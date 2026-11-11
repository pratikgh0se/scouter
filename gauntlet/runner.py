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
#: (N15): session-close pruning. Within this many GB of DISK_MIN_GB the runner
#: prunes kept (non-pass) closed sessions, oldest first, before it would park.
DISK_PRUNE_MARGIN_GB = 5.0
#: (N15): closed sessions holding a non-pass case keep their full folders up
#: to this many (newest win); older ones are pruned too.
SESSION_KEEP_NONPASS_MAX = 10
SESSION_PRUNE_MODES = ("passing", "all", "off")


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
    #: parallel mode: a sibling worker parked, so this one stopped between cases
    stopped_early: bool = False
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


def with_walkthrough_prefix(chosen: list[Case], layer_cases: list[Case]) -> list[Case]:
    """Group D (canary-colorcal-20261108): a walkthrough case (meta session
    "walkthrough") is one stateful session in document order, so a subset
    holding step N also needs steps 0..N-1 or it runs on state the script never
    built (execute.py blocks it, which scored a harness gap as a product
    fail). Add the missing earlier steps, in document order, so the selected
    case is really tested rather than excluded. Runs after every other filter
    (only_failed, resume): a prefix step that passed earlier still has to run
    again in this session to rebuild the state."""
    def order(c: Case):
        return c.meta.get("order", 0) if c.meta.get("session") == "walkthrough" else None

    top: dict = {}
    for c in chosen:
        o = order(c)
        if o is not None and o > 0:
            top[c.layer] = max(top.get(c.layer, 0), o)
    if not top:
        return chosen
    have = {c.id for c in chosen}
    need = {c.id for c in layer_cases
            if order(c) is not None and c.layer in top and order(c) < top[c.layer]}
    if not need - have:
        return chosen
    return [c for c in layer_cases if c.id in have | need]


class Runner:
    def __init__(self, adapter: Adapter, run: RunDir, *, commit: str = "",
                 clock=time.monotonic, sleep=time.sleep, wall=time.time,
                 session_max_cases: int = SESSION_MAX_CASES,
                 session_max_s: float = SESSION_MAX_S,
                 session_open_cap_s: float | None = None,
                 max_consecutive_open_failures: int = MAX_CONSECUTIVE_OPEN_FAILURES,
                 free_disk_gb: Callable[[Path], float] | None = None,
                 disk_min_gb: float = DISK_MIN_GB,
                 should_stop: Callable[[], bool] | None = None,
                 session_prune: str = "passing",
                 session_keep_nonpass_max: int = SESSION_KEEP_NONPASS_MAX,
                 disk_prune_margin_gb: float = DISK_PRUNE_MARGIN_GB) -> None:
        self.should_stop = should_stop
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
        assert session_prune in SESSION_PRUNE_MODES, session_prune
        self.session_prune = session_prune
        self.session_keep_nonpass_max = session_keep_nonpass_max
        self.disk_prune_margin_gb = disk_prune_margin_gb
        #: session folder -> scores of the cases that ran in it (insertion
        #: order = oldest first); `_pruned` = folders already pruned.
        self._ledger: dict[Path, list[str]] = {}
        self._pruned: set[Path] = set()

    def _select(self, cases: list[Case], layer: str, *, resume: bool, only_failed: bool,
                case_ids: set[str] | None, tiers: set[str] | None = None,
                prefixes: tuple[str, ...] = (), where: dict | None = None) -> list[Case]:
        chosen = self._select_filtered(cases, layer, resume=resume, only_failed=only_failed,
                                       case_ids=case_ids, tiers=tiers, prefixes=prefixes,
                                       where=where)
        return with_walkthrough_prefix(chosen, [c for c in cases if c.layer == layer])

    def _select_filtered(self, cases: list[Case], layer: str, *, resume: bool,
                         only_failed: bool, case_ids: set[str] | None,
                         tiers: set[str] | None = None, prefixes: tuple[str, ...] = (),
                         where: dict | None = None) -> list[Case]:
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
        if resume:  # an explicit --case list narrows the set; it never re-runs done ids
            done = self.run.done_ids()
            chosen = [c for c in chosen if c.id not in done]
        return chosen

    def selection(self, layers: list[str] | None = None, *, resume: bool = True,
                  only_failed: bool = False, case_ids: set[str] | None = None,
                  tiers: set[str] | None = None, prefixes: tuple[str, ...] = (),
                  where: dict | None = None) -> list[Case]:
        """The cases run_layers would run, layer by layer in layer order (the
        parallel parent shards this)."""
        cases = self.run.read_cases()
        specs = sorted(self.adapter.layers(), key=lambda s: s.order)
        if layers:
            specs = [s for s in specs if s.name in layers]
        out: list[Case] = []
        for spec in specs:
            out.extend(self._select(cases, spec.name, resume=resume, only_failed=only_failed,
                                    case_ids=case_ids, tiers=tiers, prefixes=prefixes,
                                    where=where))
        return out

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
            self.progress.end_layer(spec.name, "parked" if status in (
                "parked_disk", "stopped_early") else status)
            if status == "stopped_crash":
                summary.stopped_layers.append(spec.name)
            if status in ("parked_disk", "stopped_early"):
                break
        if refused is not None:
            summary.status = f"blocked: {refused}"
        else:
            summary.status = "parked" if summary.parked or summary.stopped_early else "done"
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
                if self.should_stop is not None and self.should_stop():
                    summary.stopped_early = True
                    summary.skipped += len(todo) - i
                    self.progress.log(f"- layer {layer}: stopped before {case.id} "
                                      "(a sibling worker parked)")
                    return "stopped_early"
                free_fn = self.free_disk_gb or free_disk_gb
                free_gb = free_fn(self.run.root)
                if free_gb < self.disk_min_gb + self.disk_prune_margin_gb:
                    # (N15): prune kept closed sessions oldest first before parking
                    self._prune_for_disk(session, free_fn)
                    free_gb = free_fn(self.run.root)
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
                        self._sweep(None)
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
                self._note_session(session, verdict)
                self._sweep(self._session_root(session))
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
                self._sweep(None)

    # -- N15: session-close pruning ---------------------------------------

    @staticmethod
    def _session_root(session) -> Path | None:
        """The folder of the session that is open right now (the adapter's
        session exposes `session_dir()`); None when none is open or the
        adapter has no per-session folders."""
        fn = getattr(session, "session_dir", None) if session is not None else None
        try:
            r = fn() if fn else None
        except Exception:  # noqa: BLE001
            return None
        return Path(r) if r else None

    def _note_session(self, session, verdict) -> None:
        root = self._session_root(session)
        if root is not None:
            self._ledger.setdefault(root, []).append(verdict.score)

    def _prune_one(self, root: Path) -> None:
        names = getattr(self.adapter, "session_prune_names", None)
        keep = tuple(getattr(self.adapter, "session_prune_keep", lambda: ())())
        if names is None or root in self._pruned:
            return
        try:
            freed = self.run.prune_session(root, tuple(names()), keep)
        except Exception as e:  # noqa: BLE001 - never let pruning kill the run
            self.progress.log(f"- session prune {root.name} failed: {type(e).__name__}: {e}")
            return
        self._pruned.add(root)
        if freed:
            self.progress.log(f"- pruned closed session {root.parent.name}/{root.name}: "
                              f"freed {freed} bytes")

    def _sweep(self, open_root: Path | None) -> None:
        """Prune every CLOSED session THIS runner opened and recorded (the
        ledger is per Runner/process: with parallel workers over one run dir,
        a folder another worker opened is never in it, so never touched).
        Closed = in the ledger but not the open one: all-pass ones always (mode `passing`/`all`), every one in mode
        `all`, and non-pass ones beyond the newest `session_keep_nonpass_max`.
        Verdicts come from the cases the runner itself scored in each folder."""
        if self.session_prune == "off":
            return
        kept: list[Path] = []
        for root, scores in list(self._ledger.items()):
            if root == open_root or root in self._pruned:
                continue
            if self.session_prune == "all" or all(sc == "pass" for sc in scores):
                self._prune_one(root)
            else:
                kept.append(root)
        for root in kept[:max(0, len(kept) - self.session_keep_nonpass_max)]:
            self._prune_one(root)

    def _prune_for_disk(self, session, free_fn) -> None:
        """Low disk: prune the remaining closed (kept) sessions, oldest
        first, until free space clears the margin. Never the open one."""
        if self.session_prune == "off":
            return
        open_root = self._session_root(session)
        target = self.disk_min_gb + self.disk_prune_margin_gb
        for root in list(self._ledger):
            if free_fn(self.run.root) >= target:
                return
            if root != open_root and root not in self._pruned:
                self._prune_one(root)

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
