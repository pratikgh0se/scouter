"""The generic runner (spec 2.3): sessions, hard caps, resume, crash guard,
park-and-resume. Product behaviour lives in the adapter's Session."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

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
#: the same hard-cap machinery as a case.
SESSION_OPEN_CAP_S = 180.0
#: (fix round 1, I2): abort()/close() are best-effort cleanup, not part of
#: the case's own budget; capped and swallowed so a broken cleanup path
#: cannot kill the run.
SESSION_CLOSE_CAP_S = 30.0
#: (fix round 1, I1): after this many consecutive open failures the layer
#: stops (status "blocked") instead of retrying forever.
MAX_CONSECUTIVE_OPEN_FAILURES = 3


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


class Runner:
    def __init__(self, adapter: Adapter, run: RunDir, *, commit: str = "",
                 clock=time.monotonic, sleep=time.sleep, wall=time.time,
                 session_max_cases: int = SESSION_MAX_CASES,
                 session_max_s: float = SESSION_MAX_S,
                 session_open_cap_s: float = SESSION_OPEN_CAP_S,
                 max_consecutive_open_failures: int = MAX_CONSECUTIVE_OPEN_FAILURES) -> None:
        self.adapter = adapter
        self.run = run.ensure()
        self.clock = clock
        self.sleep = sleep
        self.wall = wall
        self.progress = Progress(run, adapter.name, commit, clock)
        self.policy = adapter.policy()
        self.session_max_cases = session_max_cases
        self.session_max_s = session_max_s
        self.session_open_cap_s = session_open_cap_s
        self.max_consecutive_open_failures = max_consecutive_open_failures

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
            self.progress.end_layer(spec.name, status)
            if status == "stopped_crash":
                summary.stopped_layers.append(spec.name)
        if refused is not None:
            summary.status = f"blocked: {refused}"
        else:
            summary.status = "parked" if summary.parked else "done"
        self.progress.finish(summary.status)
        return summary

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

    def _open_session(self, layer: str):
        """Open a session under the same hard-cap machinery as a case (fix
        round 1, I1). Returns (session, None) or (None, reason)."""
        try:
            session = _call_with_cap(lambda: self.adapter.open_session(layer, self.run),
                                     self.session_open_cap_s)
            return session, None
        except RefuseRun as e:
            raise _Refused(e.reason) from e
        except _Timeout as e:
            return None, str(e)
        except Exception as e:  # noqa: BLE001 - a launch failure is a case result
            return None, f"{type(e).__name__}: {e}"

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
                if session is None or count >= self.session_max_cases or \
                        self.clock() - started > self.session_max_s:
                    if session is not None:
                        old, session = session, None
                        self._safe_close(old)
                    session, open_err = self._open_session(layer)
                    if session is None:
                        consecutive_open_failures += 1
                        obs = Observation(blocked=f"session_open: {open_err}",
                                          hang_evidence=[open_err or ""])
                        t_wall = self.wall()
                        verdict = evaluate(case, obs, self.policy)
                        self._write(case, obs, verdict, t_wall)
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
                                         case.budget_cap_s())
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

    def _write(self, case: Case, obs: Observation, verdict: Verdict, t_wall: float) -> None:
        from dataclasses import asdict

        prev = self.run.read_result(case.id)
        doc = {
            "id": case.id, "layer": case.layer, "tier": case.tier, "mode": case.mode,
            "body": case.body, "frame_role": case.frame_role, "source": case.source,
            "meta": case.meta, "repro": case.repro.replace("<stamp>", self.run.stamp),
            "started_at": t_wall, "wall_s": round(obs.wall_s, 3),
            "verdict": verdict.to_dict(),
            "observation": asdict(obs),
        }
        self.run.write_result(case.id, doc)
        prev_verdict = prev["verdict"] if prev else None
        self.progress.record(case.layer, case.id, case.tier, verdict.to_dict(), obs.wall_s,
                             prev_verdict=prev_verdict)


def failed_verdict(note: str) -> Verdict:
    return Verdict(FAIL, "failed", notes=[note])
