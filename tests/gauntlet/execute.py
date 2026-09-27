"""Tool-mode case execution (spec 2.3 'Per case'): isolation, before
snapshot/stats, the steps with placeholders, after snapshot/stats."""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from gauntlet import chart
from gauntlet.adapter import CallRecord, Observation, Park
from gauntlet.case import Case
from tests.gauntlet import observe
from tests.gauntlet.corpus import guard
from tests.gauntlet.session import SessionBlocked, reset_journal

_PH = re.compile(r"\{([a-z_0-9]+)(?::([^}]+))?\}")
#: Virtual tools, registered by the layer executors (tests/gauntlet/exec_*.py):
#: name -> fn(session, case, step, obs) -> CallRecord.
VIRTUAL: dict = {}
STAT_METRICS = {"M2", "M3", "M4", "M5", "M6", "M7", "M8"}
#: body -> WB table camera id (config `cameras` map).
CAMERA = {"olympus": "em10iii", "sony_a7": "a7", "sony_nex5n": "nex5n"}


def substitute(value, session, calls: list[CallRecord]):
    """Fill {frame:role}, {job_frame:N}, {job_frames}, {step:N:path} and session
    vars. Every resulting path is checked against the originals guard."""
    st = session.staged

    def one(m: re.Match) -> str:
        name, arg = m.group(1), m.group(2)
        if name == "frame":
            return str(st.frames[arg])
        if name == "job_frame":
            return str(st.job_frames[int(arg) - 1])
        if name == "step":
            idx, path = arg.split(":", 1)
            cur = (calls[int(idx)].envelope or {})
            for part in path.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            return "" if cur is None else str(cur)
        return str(st.vars[name])

    if isinstance(value, str):
        if value == "{job_frames}":
            return [str(p) for p in st.job_frames]
        out = _PH.sub(one, value)
        if out.startswith("/") or out.startswith("~"):
            guard(out)
        return out
    if isinstance(value, list):
        flat = []
        for v in value:
            r = substitute(v, session, calls)
            flat.extend(r if isinstance(r, list) and v == "{job_frames}" else [r])
        return flat
    if isinstance(value, dict):
        return {k: substitute(v, session, calls) for k, v in value.items()}
    return value


def needs_stats(case: Case) -> bool:
    return any(c.metric in STAT_METRICS for c in case.expect.checks)


_JOB_PLACEHOLDERS = {"job_slug", "job_frame", "job_frames"}


def _refs_job(case: Case) -> bool:
    """True when any step's args reference {job_slug}/{job_frame:N}/
    {job_frames} -- review round 2, finding 6 (GB-1)."""
    def walk(v) -> bool:
        if isinstance(v, str):
            return any(m.group(1) in _JOB_PLACEHOLDERS for m in _PH.finditer(v))
        if isinstance(v, list):
            return any(walk(x) for x in v)
        if isinstance(v, dict):
            return any(walk(x) for x in v.values())
        return False

    return any(walk(st.args) for st in case.steps)


def isolate(session, frame: Path, role: str) -> bool:
    """Spec 2.3 case isolation: reset what the previous case touched; then the
    snapshot must equal this frame's session baseline, else the session is
    contaminated and the frame is re-staged from a fresh copy (relaunch).
    Returns True when it had to re-stage.

    (night-1 fix, class LK-refresh-no-open): resetting a module the previous
    case touched acts on whatever frame is current, which on a chart<->hero
    switch is often a module this frame's own history never had (for
    example, the PFM chart has no temperature module). `darkroom_reset` then
    writes it in at darktable's default -- absent becomes present-at-default,
    which is not a real change -- but a plain `param_equal` reads "None" vs
    "the default value" as moved and re-stages a perfectly clean frame. Use
    the evaluator's own absent<->default equivalence (bug 3) so isolate and
    the evaluator agree on what counts as a diff."""
    from gauntlet import evaluate, metrics

    if session.touched:
        from scouter.core import vocab

        mods = {s.module for s in vocab.PARAMS} if "*" in session.touched else \
            session.touched - {"*"}
        for m in sorted(mods):
            session.call("darkroom_reset", {"module": m, "confirm": True}, 12.0, write=True)
        session.touched.clear()
    now = observe.snapshot(frame)
    base = session.baselines.setdefault(str(frame), now["params"])
    units = now.get("units", {})
    defaults = now.get("defaults", {})
    now_params = now["params"]
    moved = [k for k in set(base) | set(now_params)
             if not metrics.param_equal(units.get(k, "ratio"), now_params.get(k), base.get(k))
             and not evaluate._absent_equiv(base, now_params, k, defaults)]
    if moved:
        session.refresh_frame(role)
        return True
    return False


def run_tool_case(session, case: Case, *, clock=time.monotonic) -> Observation:
    obs = Observation()
    t0 = clock()
    #: Review round 2, finding 6 (GB-1, P1): a case whose own steps
    #: reference the guide job placeholders must never run (and fail on the
    #: product's own terms) when the harness's own job import failed --
    #: block it honestly instead. Merge note (guide x fix/htc-hang-
    #: recovery): this and any future pre_ping/inherited_wedge block both
    #: belong here, at the very top, before the isolate/ensure_frame try;
    #: whichever lands second in a merge keeps both, this one first.
    stage_error = getattr(session, "guide_job_stage_error", None)
    if stage_error and _refs_job(case):
        obs.blocked = f"fixture: guide job import failed: {stage_error}"
        obs.wall_s = clock() - t0
        return obs
    role = case.frame_role or "hero"
    #: Fix A (night-1 triage rank 6, GD-walkthrough-state): the user-guide
    #: walkthrough (layers/guide.py WALKTHROUGH, meta["session"] ==
    #: "walkthrough") is one stateful session in document order -- gd-02's
    #: "set contrast" runs on top of gd-01's "set exposure", gd-04's undo
    #: expects gd-02/03's history to still be there, gd-09's preset save
    #: needs the frame the earlier steps actually left. Isolating every case
    #: (the ordinary rule) or resetting its undo journal would erase exactly
    #: the state the next scripted step depends on. An ordinary case (any
    #: other meta["session"], or none) keeps the ordinary per-case isolation.
    #: Z1 intent example: tests/intent/walkthrough_state.json.
    walkthrough = case.meta.get("session") == "walkthrough"
    continuing = walkthrough and session.walkthrough_started
    try:
        frame = session.ensure_frame(role)
        if not continuing and isolate(session, frame, role):
            frame = session.ensure_frame(role)
            obs.extra["contaminated_restaged"] = True
    except SessionBlocked as e:
        obs.blocked = f"session: {e}"
        obs.hang_evidence.append(f"session: {e}")
        obs.wall_s = clock() - t0
        return obs
    if walkthrough:
        session.walkthrough_started = True
    #: Fix round 2 (review R1); fix round 3 (re-review R1, ordering): per-case
    #: undo journal isolation -- see session.reset_journal's own docstring.
    #: Must run AFTER isolate() (whose own darkroom_reset calls, for a
    #: module the PREVIOUS case touched, write their own journal entries)
    #: and any re-stage above, but before this case's own before-snapshot/
    #: steps below, so no earlier entry -- isolate's own or an earlier
    #: case's -- can leak into THIS case's darkroom_undo_last. The count is
    #: kept in obs.extra so a leak stays visible rather than disappearing.
    #: A continuing walkthrough case (fix A above) skips this too: gd-04's
    #: and gd-08's own undo steps must see the REAL journal the walkthrough
    #: built up, not an empty one.
    if session.staged is not None and not continuing:
        obs.extra["journal_reset_entries"] = reset_journal(session.staged)
    kelvin = observe.kelvin_for(CAMERA.get(case.body or ""))
    obs.before = observe.snapshot(frame, kelvin)
    snaps = [obs.before["params"]]
    masks = None
    stats = needs_stats(case)
    previews = {}
    is_chart = case.frame_role == "chart"
    if stats:
        raster = observe.render_raster(frame, session.root / "render" / "before.ppm")
        if is_chart:
            obs.stats_before = chart.patch_stats(raster)
        else:
            obs.stats_before, masks = observe.stats(raster)
        obs.floors = dict(session.floors)
        previews["before"] = observe.render_preview(frame, session.previews_dir / case.id /
                                                    "before.png")
    for st in case.steps:
        args = substitute(st.args, session, obs.calls)
        if st.tool in VIRTUAL:
            try:
                rec = VIRTUAL[st.tool](session, case, st, obs)
            except Park:
                raise
            except subprocess.TimeoutExpired:
                obs.blocked = f"hang: {st.tool} exceeded budget"
                obs.hang_evidence.append(f"{st.tool}: no answer in {st.budget_s + 30:.0f}s "
                                         "(subprocess timeout)")
                break
            except Exception as e:  # noqa: BLE001 - a virtual tool crash must not crash the case
                obs.blocked = f"harness_error: {st.tool}: {type(e).__name__}: {e}"
                break
        else:
            rec = session.call(st.tool, args, st.budget_s, write=st.write)
        obs.calls.append(rec)
        if obs.blocked:  # (final fix M3): a virtual tool blocked the case itself
            break
        if rec.error == "timeout" and rec.health == "unresponsive":
            obs.blocked = "hang"
            obs.hang_evidence.append(f"{st.tool}: no answer in {st.budget_s + 30:.0f}s, "
                                     "ping unresponsive")
            break
        if rec.envelope and (rec.envelope.get("healed") or
                             (rec.envelope.get("data") or {}).get("healed")):
            obs.healed = True
        snaps.append(observe.snapshot(frame, kelvin)["params"] if st.write else snaps[-1])
    obs.after = observe.snapshot(frame, kelvin)
    obs.extra["snapshots"] = snaps
    if stats and not obs.blocked:
        raster = observe.render_raster(frame, session.root / "render" / "after.ppm")
        obs.stats_after = chart.patch_stats(raster) if is_chart else \
            observe.stats(raster, masks)[0]
        previews["after"] = observe.render_preview(frame, session.previews_dir / case.id /
                                                   "after.png")
        obs.extra["previews"] = {k: session.relative(v) for k, v in previews.items()}
    obs.wall_s = clock() - t0
    return obs
