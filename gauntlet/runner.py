"""The generic runner (spec 2.3): sessions, hard caps, resume, crash guard,
park-and-resume. Product behaviour lives in the adapter's Session."""

from __future__ import annotations

import inspect
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from gauntlet.adapter import Adapter, Observation, Park, RefuseRun
from gauntlet.case import Case
from gauntlet.evaluate import FAIL, Verdict, evaluate
from gauntlet.progress import Progress
from gauntlet.store import RunDir

SESSION_MAX_CASES = 50
SESSION_MAX_S = 20 * 60
PARK_MAX_WAIT_S = 6 * 3600
NON_PASS = ("partial", "fail")
#: (fix round 1, I1): open_session can hang (a stuck product launch) or raise
#: (a launch failure); it must never do either untyped, so it goes through
#: the same hard-cap machinery as a case. (pre-night-1 N1): an adapter may
#: raise it with an optional `session_open_cap_s` attribute.
SESSION_OPEN_CAP_S = 180.0
#: (fix round 1, I2): abort()/close() are best-effort cleanup, not part of
#: the case's own budget; capped and swallowed so a broken cleanup path
#: cannot kill the run.
SESSION_CLOSE_CAP_S = 30.0
#: (fix round 1, I1): after this many consecutive open failures the layer
#: stops (status "blocked") instead of retrying forever.
MAX_CONSECUTIVE_OPEN_FAILURES = 3
#: (t_90e75c20): the run-level disk guard -- below this many decimal GB free
#: (as `df -H` reports: 1000-based, not 1024-based) the runner refuses to
#: start the next case and parks the rest of the layer, reason "disk", the
#: same shape as the existing usage-meter parker (Park raised before any
#: session work for the case begins).
DISK_MIN_GB = 25.0


def free_disk_gb(path: Path) -> float:
    """Decimal GB free on `path`'s filesystem, matching `df -H`. The real
    default for Runner's `free_disk_gb` seam; tests inject a fake callable
    instead of mocking `shutil.disk_usage` itself."""
    return shutil.disk_usage(path).free / 1_000_000_000


@dataclass
class RunSummary:
    ran: int = 0
    skipped: int = 0
    parked: list[str] = field(default_factory=list)
    stopped_layers: list[str] = field(default_factory=list)
    status: str = "done"


class _Timeout(Exception):
    pass


class _Refused(Exception):
    """Internal: a RefuseRun reached the layer loop (final fix I2)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _call_with_cap(fn, cap_s: float):
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - re-raised in the caller
            box["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(cap_s)
    if t.is_alive():
        raise _Timeout(f"case exceeded its hard cap of {cap_s:.0f}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _accepts(fn, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


class Runner:
    def __init__(self, adapter: Adapter, run: RunDir, *, commit: str = "",
                 clock=time.monotonic, sleep=time.sleep, wall=time.time,
                 session_max_cases: int = SESSION_MAX_CASES,
                 session_max_s: float = SESSION_MAX_S,
                 session_open_cap_s: float | None = None,
                 max_consecutive_open_failures: int = MAX_CONSECUTIVE_OPEN_FAILURES,
                 free_disk_gb: Callable[[Path], float] | None = None,
                 disk_min_gb: float = DISK_MIN_GB) -> None:
        self.adapter = adapter
        self.run = run.ensure()
        self.commit = commit
        self.clock = clock
        self.sleep = sleep
        self.wall = wall
        self.progress = Progress(run, adapter.name, commit, clock)
        self.policy = adapter.policy()
        self.session_max_cases = session_max_cases
        self.session_max_s = session_max_s
        if session_open_cap_s is None:
            session_open_cap_s = getattr(adapter, "session_open_cap_s", SESSION_OPEN_CAP_S)
        self.session_open_cap_s = float(session_open_cap_s)
        self.max_consecutive_open_failures = max_consecutive_open_failures
        #: (fix round 1, I3): stored possibly `None` -- resolved against the
        #: MODULE's own `free_disk_gb` at call time (`_run_layer`), never
        #: bound here, so a test that monkeypatches `gauntlet.runner.
        #: free_disk_gb` (never the real host disk) reaches every Runner,
        #: not just ones constructed after the patch.
        self.free_disk_gb = free_disk_gb
        self.disk_min_gb = disk_min_gb

    def _select(self, cases: list[Case], layer: str, *, resume: bool, only_failed: bool,
                case_ids: set[str] | None, tiers: set[str] | None = None,
                prefixes: tuple[str, ...] = (), where: dict | None = None) -> list[Case]:
        chosen = [c for c in cases if c.layer == layer]
        if tiers:
            chosen = [c for c in chosen if c.tier in tiers]
        if prefixes:
            chosen = [c for c in chosen if c.id.startswith(prefixes)]
        if where:
            chosen = [c for c in chosen
                      if all(str(c.meta.get(k)) in vs for k, vs in where.items())]
        if case_ids:
            chosen = [c for c in chosen if c.id in case_ids]
        if only_failed:
            keep = []
            for c in chosen:
                r = self.run.read_result(c.id)
                if r is None or r["verdict"]["score"] in NON_PASS:
                    keep.append(c)
            return keep
        if resume and not case_ids:
            done = self.run.done_ids()
            chosen = [c for c in chosen if c.id not in done]
        return chosen

    def run_layers(self, layers: list[str] | None = None, *, resume: bool = True,
                   only_failed: bool = False, case_ids: set[str] | None = None,
                   wait_on_park: bool = False, tiers: set[str] | None = None,
                   prefixes: tuple[str, ...] = (), where: dict | None = None) -> RunSummary:
        cases = self.run.read_cases()
        specs = sorted(self.adapter.layers(), key=lambda s: s.order)
        if layers:
            specs = [s for s in specs if s.name in layers]
        summary = RunSummary()
        refused: str | None = None
        for spec in specs:
            all_layer = [c for c in cases if c.layer == spec.name]
            todo = self._select(cases, spec.name, resume=resume, only_failed=only_failed,
                                case_ids=case_ids, tiers=tiers, prefixes=prefixes,
                                where=where)
            self.progress.layer(spec.name, len(all_layer),
                                sum(c.tier == "P0" for c in all_layer), spec.live)
            if not todo:
                continue
            refused = self._preflight(spec.name)
            if refused is None:
                self.progress.start_layer(spec.name)
                try:
                    status = self._run_layer(spec.name, todo, summary, wait_on_park)
                except _Refused as r:
                    refused = r.reason
            if refused is not None:
                #: (final fix I2): no case result is written; the run stops.
                summary.stopped_layers.append(spec.name)
                self.progress.log(f"- layer {spec.name}: run refused -- {refused}")
                self.progress.end_layer(spec.name, "blocked")
                break
            #: (fix round 1, M3): "parked_disk" is "parked" for progress/report
            #: purposes -- the distinct string only tells THIS loop to stop
            #: opening later layers, each of which would otherwise immediately
            #: re-park its own first case on the same disk floor.
            self.progress.end_layer(spec.name, "parked" if status == "parked_disk" else status)
            if status == "stopped_crash":
                summary.stopped_layers.append(spec.name)
            if status == "parked_disk":
                break
        if refused is not None:
            summary.status = f"blocked: {refused}"
        else:
            summary.status = "parked" if summary.parked else "done"
        self.progress.finish(summary.status)
        return summary

    def case_cap_s(self, case: Case) -> float:
        """(final fix I7): the product's budget cap (Case.budget_cap_s) plus
        the adapter's optional harness allowance -- time the harness itself
        spends inside a case (isolation resets, relaunches, renders) must not
        turn into a product hang. Adapter method `harness_allowance_s(case)`,
        default 0."""
        fn = getattr(self.adapter, "harness_allowance_s", None)
        extra = float(fn(case)) if fn is not None else 0.0
        return case.budget_cap_s() + max(0.0, extra)

    def _preflight(self, layer: str) -> str | None:
        """The adapter's optional run-level check before a layer (final fix I2)."""
        pre = getattr(self.adapter, "preflight", None)
        if pre is None:
            return None
        try:
            reason = pre(self.run, layer)
        except RefuseRun as e:
            return e.reason
        return str(reason) if reason else None

    def _open_session(self, layer: str, case: Case):
        """Open a session under the same hard-cap machinery as a case (fix
        round 1, I1). Returns (session, None) or (None, reason).

        (pre-night-1 N2): an adapter whose open_session takes `next_case` is
        told the case the session opens for. (N1): an open past the cap calls
        the adapter's optional `abort_open()` so its in-flight launch is
        killed, not orphaned."""
        fn = self.adapter.open_session
        kw = {"next_case": case} if _accepts(fn, "next_case") else {}
        try:
            session = _call_with_cap(lambda: fn(layer, self.run, **kw),
                                     self.session_open_cap_s)
            return session, None
        except RefuseRun as e:
            raise _Refused(e.reason) from e
        except _Timeout as e:
            return None, "; ".join([str(e), *self._safe_abort_open()])
        except Exception as e:  # noqa: BLE001 - a launch failure is a case result
            return None, f"{type(e).__name__}: {e}"

    def _safe_abort_open(self) -> list[str]:
        """(pre-night-1 N1): best-effort, capped, never raises."""
        fn = getattr(self.adapter, "abort_open", None)
        if fn is None:
            return ["abort_open: not provided by the adapter"]
        try:
            return [f"abort_open: {_call_with_cap(fn, SESSION_CLOSE_CAP_S)}"]
        except _Timeout as e:
            return [f"abort_open timed out: {e}"]
        except Exception as e:  # noqa: BLE001 - never let cleanup kill the run
            return [f"abort_open failed: {type(e).__name__}: {e}"]

    def _safe_close(self, session) -> None:
        """close() is best-effort: capped, and a failure only reaches the log
        (fix round 1, I2)."""
        try:
            _call_with_cap(session.close, SESSION_CLOSE_CAP_S)
        except _Timeout as e:
            self.progress.log(f"- session close timed out: {e}")
        except Exception as e:  # noqa: BLE001 - never let cleanup kill the run
            self.progress.log(f"- session close failed: {type(e).__name__}: {e}")

    def _safe_abort(self, session) -> list[str]:
        """abort() is best-effort: capped, and a failure is folded into the
        case's own hang_evidence rather than raised (fix round 1, I2)."""
        try:
            _call_with_cap(session.abort, SESSION_CLOSE_CAP_S)
            return []
        except _Timeout as e:
            return [f"abort timed out: {e}"]
        except Exception as e:  # noqa: BLE001 - never let cleanup kill the run
            return [f"abort failed: {type(e).__name__}: {e}"]

    def _run_layer(self, layer: str, todo: list[Case], summary: RunSummary,
                   wait_on_park: bool) -> str:
        session = None
        started = 0.0
        count = 0
        i = 0
        consecutive_open_failures = 0
        try:
            while i < len(todo):
                case = todo[i]
                free_gb = (self.free_disk_gb or free_disk_gb)(self.run.root)
                if free_gb < self.disk_min_gb:
                    reason = f"disk: {free_gb:.1f} GB free < {self.disk_min_gb:.0f} GB"
                    self.progress.park(layer, case.id, reason)
                    summary.parked.extend(c.id for c in todo[i:])
                    return "parked_disk"
                if session is None or count >= self.session_max_cases or \
                        self.clock() - started > self.session_max_s:
                    if session is not None:
                        old, session = session, None
                        self._safe_close(old)
                    session, open_err = self._open_session(layer, case)
                    if session is None:
                        consecutive_open_failures += 1
                        obs = Observation(blocked=f"session_open: {open_err}",
                                          hang_evidence=[open_err or ""])
                        t_wall = self.wall()
                        verdict = evaluate(case, obs, self.policy)
                        self._write(case, obs, verdict, t_wall)
                        self._retain(case)
                        summary.ran += 1
                        i += 1
                        if consecutive_open_failures >= self.max_consecutive_open_failures:
                            summary.skipped += len(todo) - i
                            self.progress.log(
                                f"- layer {layer}: stopped after "
                                f"{consecutive_open_failures} consecutive session-open "
                                f"failures ({open_err})")
                            return "blocked"
                        continue
                    consecutive_open_failures = 0
                    started, count = self.clock(), 0
                crashes_before = self.adapter.crash_count()
                t_wall = self.wall()
                t0 = self.clock()
                try:
                    obs = _call_with_cap(lambda s=session, c=case: s.run_case(c),
                                         self.case_cap_s(case))
                except Park as p:
                    self.progress.park(layer, case.id, p.reason)
                    if wait_on_park and p.until and p.until - self.wall() < PARK_MAX_WAIT_S:
                        self.sleep(max(0.0, p.until - self.wall()) + 60)
                        self.progress.unpark(case.id, layer)
                        continue
                    summary.parked.extend(c.id for c in todo[i:])
                    return "parked"
                except RefuseRun as e:
                    # (final fix I2): the machine refuses the run; this case
                    # gets no result (resume reruns it) and the run stops.
                    summary.skipped += len(todo) - i
                    raise _Refused(e.reason) from e
                except _Timeout as e:
                    old, session = session, None
                    evidence = [str(e), *self._safe_abort(old)]
                    obs = Observation(blocked="hard_cap", hang_evidence=evidence)
                except Exception as e:  # noqa: BLE001 - a harness error is a case result
                    obs = Observation(blocked="harness_error",
                                      hang_evidence=[f"{type(e).__name__}: {e}"])
                obs.wall_s = obs.wall_s or (self.clock() - t0)
                if self.adapter.crash_count() > crashes_before:
                    # (final fix I3): merge -- a session-recorded crash (e.g. an
                    # MCP-server death) must survive crash files appearing too.
                    obs.crash_reports = list(dict.fromkeys(
                        [*obs.crash_reports, *self.adapter.crash_reports_since(t_wall)]))
                verdict = evaluate(case, obs, self.policy)
                self._write(case, obs, verdict, t_wall)
                self._retain(case)
                summary.ran += 1
                count += 1
                i += 1
                if verdict.outcome == "crash":
                    summary.skipped += len(todo) - i
                    return "stopped_crash"
            return "done"
        finally:
            if session is not None:
                old, session = session, None
                self._safe_close(old)

    def _retain(self, case: Case) -> None:
        """Per-case retention after scoring: an adapter that opts in with
        `retain_keep(layer)` gets its case workdir pruned to that keep list.
        Best-effort like `_safe_close` (fix round 1, I2): a retention
        failure only reaches the log, never the run -- this card exists to
        keep a night ALIVE, so it must never be the thing that kills one."""
        retain_keep = getattr(self.adapter, "retain_keep", None)
        if retain_keep is None:
            return
        try:
            freed = self.run.retain(case.id, tuple(retain_keep(case.layer)))
        except Exception as e:  # noqa: BLE001 - never let retention kill the run
            self.progress.log(f"- retain {case.id} failed: {type(e).__name__}: {e}")
            return
        if freed:
            self.progress.log(f"- retained {case.id}: freed {freed} bytes")

    def _write(self, case: Case, obs: Observation, verdict: Verdict, t_wall: float) -> None:
        from dataclasses import asdict

        prev = self.run.read_result(case.id)
        doc = {
            "id": case.id, "layer": case.layer, "tier": case.tier, "mode": case.mode,
            "body": case.body, "frame_role": case.frame_role, "source": case.source,
            "meta": case.meta, "repro": case.repro.replace("<stamp>", self.run.stamp),
            "started_at": t_wall, "wall_s": round(obs.wall_s, 3),
            #: (final fix I5): release bar 6 is "one full run of one commit".
            "commit": self.commit,
            "verdict": verdict.to_dict(),
            "observation": asdict(obs),
        }
        self.run.write_result(case.id, doc)
        prev_verdict = prev["verdict"] if prev else None
        prev_blocked = ((prev or {}).get("observation") or {}).get("blocked")
        self.progress.record(case.layer, case.id, case.tier, verdict.to_dict(), obs.wall_s,
                             prev_verdict=prev_verdict, blocked=obs.blocked,
                             prev_blocked=prev_blocked)


def failed_verdict(note: str) -> Verdict:
    return Verdict(FAIL, "failed", notes=[note])
