"""The generic runner (spec 2.3): sessions, hard caps, resume, crash guard,
park-and-resume. Product behaviour lives in the adapter's Session."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from gauntlet.adapter import Adapter, Observation, Park
from gauntlet.case import Case
from gauntlet.evaluate import FAIL, Verdict, evaluate
from gauntlet.progress import Progress
from gauntlet.store import RunDir

SESSION_MAX_CASES = 50
SESSION_MAX_S = 20 * 60
PARK_MAX_WAIT_S = 6 * 3600
NON_PASS = ("partial", "fail")


@dataclass
class RunSummary:
    ran: int = 0
    skipped: int = 0
    parked: list[str] = field(default_factory=list)
    stopped_layers: list[str] = field(default_factory=list)
    status: str = "done"


class _Timeout(Exception):
    pass


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
                 session_max_s: float = SESSION_MAX_S) -> None:
        self.adapter = adapter
        self.run = run.ensure()
        self.clock = clock
        self.sleep = sleep
        self.wall = wall
        self.progress = Progress(run, adapter.name, commit, clock)
        self.policy = adapter.policy()
        self.session_max_cases = session_max_cases
        self.session_max_s = session_max_s

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
        for spec in specs:
            all_layer = [c for c in cases if c.layer == spec.name]
            todo = self._select(cases, spec.name, resume=resume, only_failed=only_failed,
                                case_ids=case_ids, tiers=tiers, prefixes=prefixes,
                                where=where)
            self.progress.layer(spec.name, len(all_layer),
                                sum(c.tier == "P0" for c in all_layer), spec.live)
            if not todo:
                continue
            self.progress.start_layer(spec.name)
            status = self._run_layer(spec.name, todo, summary, wait_on_park)
            self.progress.end_layer(spec.name, status)
            if status == "stopped_crash":
                summary.stopped_layers.append(spec.name)
        summary.status = "parked" if summary.parked else "done"
        self.progress.finish(summary.status)
        return summary

    def _run_layer(self, layer: str, todo: list[Case], summary: RunSummary,
                   wait_on_park: bool) -> str:
        session = None
        started = 0.0
        count = 0
        i = 0
        try:
            while i < len(todo):
                case = todo[i]
                if session is None or count >= self.session_max_cases or \
                        self.clock() - started > self.session_max_s:
                    if session is not None:
                        session.close()
                    session = self.adapter.open_session(layer, self.run)
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
                        self.progress.unpark(case.id)
                        continue
                    summary.parked.extend(c.id for c in todo[i:])
                    return "parked"
                except _Timeout as e:
                    session.abort()
                    session = None
                    obs = Observation(blocked="hard_cap", hang_evidence=[str(e)])
                except Exception as e:  # noqa: BLE001 - a harness error is a case result
                    obs = Observation(blocked="harness_error",
                                      hang_evidence=[f"{type(e).__name__}: {e}"])
                obs.wall_s = obs.wall_s or (self.clock() - t0)
                if self.adapter.crash_count() > crashes_before:
                    obs.crash_reports = self.adapter.crash_reports_since(t_wall)
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
                session.close()

    def _write(self, case: Case, obs: Observation, verdict: Verdict, t_wall: float) -> None:
        from dataclasses import asdict

        doc = {
            "id": case.id, "layer": case.layer, "tier": case.tier, "mode": case.mode,
            "body": case.body, "frame_role": case.frame_role, "source": case.source,
            "meta": case.meta, "repro": case.repro.replace("<stamp>", self.run.stamp),
            "started_at": t_wall, "wall_s": round(obs.wall_s, 3),
            "verdict": verdict.to_dict(),
            "observation": asdict(obs),
        }
        self.run.write_result(case.id, doc)
        self.progress.record(case.layer, case.id, case.tier, verdict.to_dict(), obs.wall_s)


def failed_verdict(note: str) -> Verdict:
    return Verdict(FAIL, "failed", notes=[note])
