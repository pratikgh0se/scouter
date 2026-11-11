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
from scouter import config as scouter_config
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


class ImportIncomplete(RuntimeError):
    """`library_import`'s own envelope did not account for every source
    frame the harness staged (a partial import). The destination for the
    missing frame(s) is genuinely unknown -- guessing the card path would
    only surface downstream as a false `usage`/`not_in_darkroom` refusal on
    a frame that was never the harness's fault. Raised, never swallowed:
    callers turn it into `obs.blocked`, so the case scores `blocked`,
    honestly naming what's missing."""


def _imported_frames(
    session, envelope: dict | None, source_frames: list[Path]
) -> list[Path] | None:
    """Map `source_frames` (the staged copies -- the card, or a workflow
    case's `job_frames`) to the absolute paths `library_import` actually
    wrote them to, using ITS OWN envelope (`data.files[].source_name` ->
    `.dest`, relative to the job directory -- spec section 6,
    `scouter.types.ImportResult`). Returns `None` when the envelope carries
    nothing usable at all (the import did not run or errored -- callers keep
    the original paths, since a downstream step will honestly fail on its
    own terms). Raises `ImportIncomplete` when the import DID run but its
    own `files[]` is missing one or more of the frames the harness staged (a
    partial import): the destination for those frames is unknown, not "the
    staged path", and must never be guessed. Every later step (`{job_frame:N}`,
    `{job_frames}`) must address these destination files."""
    data = (envelope or {}).get("data") or {}
    files = data.get("files") or []
    job_slug, date = data.get("job_slug"), data.get("date")
    if not files or not job_slug or not date:
        return None
    dest_by_source = {f.get("source_name"): f.get("dest") for f in files if f.get("source_name")}
    missing = [src.name for src in source_frames if src.name not in dest_by_source]
    if missing:
        raise ImportIncomplete(
            f"import_incomplete: library_import's files[] names none of {missing!r} "
            f"({len(missing)} of {len(source_frames)} staged frames)"
        )
    cfg = scouter_config.load(str(session.staged.config))
    job_path = scouter_config.job_dir(cfg, date, job_slug)
    return [job_path / dest_by_source[src.name] for src in source_frames]


def resolve_import_job_frames(
    obs: Observation, session, rec: CallRecord, source_frames: list[Path]
) -> list[Path] | None:
    """After a `library_import` step, rewrite `session.staged.job_frames` to
    the paths it actually wrote (shared by `run_tool_case` and
    `exec_bulk.run_bulk_case` -- the same rewrite, one place). A no-op
    unless `rec` is an errorless `library_import` call; sets `obs.blocked`
    on a partial import instead of guessing a destination."""
    if rec.tool != "library_import" or rec.error:
        return None
    try:
        resolved = _imported_frames(session, rec.envelope, source_frames)
    except ImportIncomplete as e:
        obs.blocked = f"harness_error: {e}"
        obs.hang_evidence.append(str(e))
        return None
    if resolved is not None:
        session.staged.job_frames = resolved
    return resolved


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
            cur = calls[int(idx)].envelope or {}
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

        mods = (
            {s.module for s in vocab.PARAMS} if "*" in session.touched else session.touched - {"*"}
        )
        for m in sorted(mods):
            session.call("darkroom_reset", {"module": m, "confirm": True}, 12.0, write=True)
        session.touched.clear()
    now = observe.snapshot(frame)
    base = session.baselines.setdefault(str(frame), now["params"])
    units = now.get("units", {})
    defaults = now.get("defaults", {})
    now_params = now["params"]
    moved = [
        k
        for k in set(base) | set(now_params)
        if not metrics.param_equal(units.get(k, "ratio"), now_params.get(k), base.get(k))
        and not evaluate._absent_equiv(base, now_params, k, defaults)
    ]
    if moved:
        session.refresh_frame(role)
        return True
    return False


def _mark_walkthrough_broken(session, case: Case, obs: Observation, walkthrough: bool) -> None:
    """Review round 2, finding 9 (GA-1): record the FIRST walkthrough case
    that ends the script's own intended state -- blocked, or a write step
    that landed !ok when the case's own expectation was not a typed_error
    (a refusal it was not scripted to produce). Never overwrites an
    earlier break: the root cause, not the latest symptom."""
    if not walkthrough or getattr(session, "walkthrough_broken", None) is not None:
        return
    broke = bool(obs.blocked)
    if not broke and case.expect.kind != "typed_error":
        broke = any(c.write and not (c.envelope or {}).get("ok") for c in obs.calls)
    if broke:
        session.walkthrough_broken = case.id


def export_files_count(obs: Observation) -> tuple[int, dict] | None:
    """HB3: how many files a case's `export_run` calls produced, with two
    oracles (Zenkai Z2). Oracle 1 is the envelope's own `files[]` entries that
    answer `ok`. Oracle 2 is the disk: how many of those ok outputs are
    actually present as non-empty files in the call's `out_dir`. The two must
    agree; on any disagreement the count is 0 (so a `gte 1` check fails) and
    the detail dict names both numbers. None when the case made no export_run
    call (nothing to count)."""
    calls = [c for c in obs.calls if c.tool == "export_run"]
    if not calls:
        return None
    reported = on_disk = 0
    for c in calls:
        data = (c.envelope or {}).get("data") or {}
        out_dir = Path(str(data.get("out_dir") or (c.args or {}).get("out_dir") or ""))
        for f in data.get("files") or ():
            if not (isinstance(f, dict) and f.get("ok")):
                continue
            reported += 1
            out = Path(str(f.get("output") or ""))
            if out.is_file() and out.stat().st_size > 0 and out.parent == out_dir:
                on_disk += 1
    detail = {"reported": reported, "on_disk": on_disk}
    return (reported if reported == on_disk else 0), detail


def run_tool_case(
    session, case: Case, *, clock=time.monotonic, pre_ping: bool = True
) -> Observation:
    obs = Observation()
    t0 = clock()
    #: Fix A (night-1 triage rank 6, GD-walkthrough-state); review round 2
    #: finding 14 (GA-3): the user-guide walkthrough (layers/guide.py
    #: WALKTHROUGH, meta["session"] == "walkthrough") is one stateful
    #: session in document order -- gd-02's "set contrast" runs on top of
    #: gd-01's "set exposure", gd-04's undo expects gd-02/03's history to
    #: still be there, gd-09's preset save needs the frame the earlier
    #: steps actually left. Isolating every case (the ordinary rule) or
    #: resetting its undo journal would erase exactly the state the next
    #: scripted step depends on. An ordinary case (any other
    #: meta["session"], or none) keeps the ordinary per-case isolation.
    #: `getattr` (not a direct attribute read) for consistency with other
    #: minimal session doubles elsewhere in this function.
    #: Z1 intent example: tests/intent/walkthrough_state.json.
    walkthrough = case.meta.get("session") == "walkthrough"
    continuing = walkthrough and getattr(session, "walkthrough_started", False)
    #: Review round 2, finding 9 (GA-2, P2): a targeted rerun (--only-failed,
    #: case_ids, a resume mid-layer) that lands on a walkthrough case with
    #: order > 0 as the FIRST walkthrough case THIS session ever runs would
    #: otherwise get the ordinary "first case" isolate/fresh-journal
    #: treatment -- gd-04 or gd-08 run alone then fails for harness reasons
    #: (an empty journal), not a real product answer. Block it honestly
    #: instead of letting it run isolated and fail dishonestly.
    if walkthrough and not continuing and case.meta.get("order", 0) > 0:
        obs.blocked = "walkthrough_prefix_not_run"
        obs.wall_s = clock() - t0
        return obs
    #: Review round 2, finding 6 (GB-1, P1): a case whose own steps
    #: reference the guide job placeholders must never run (and fail on the
    #: product's own terms) when the harness's own job import failed --
    #: block it honestly instead. Merge note (guide x fix/htc-hang-
    #: recovery, re-review round 3 GR-4): the only real conflict is this
    #: function's own signature line (hang-recovery adds `pre_ping: bool =
    #: True`); the body auto-merges, walkthrough/stage_error/prefix blocks
    #: first, pre_ping's own inherited_wedge early return after them. THAT
    #: early return does not exist yet in this branch, so it cannot call
    #: `_mark_walkthrough_broken` here today -- but it must, once merged:
    #: an inherited wedge mid-walkthrough is exactly the kind of break
    #: finding 9 (GA-1) exists to tag, and an untagged one would leave
    #: every later walkthrough case scored as if the script's state were
    #: intact. Whoever resolves that merge: add
    #: `_mark_walkthrough_broken(session, case, obs, walkthrough)` right
    #: before the inherited_wedge block's own `return obs`, the same way
    #: the SessionBlocked except-branch below and the final return already do.
    stage_error = getattr(session, "guide_job_stage_error", None)
    if stage_error and _refs_job(case):
        obs.blocked = f"fixture: guide job import failed: {stage_error}"
        obs.wall_s = clock() - t0
        return obs
    #: Review round 2, finding 9 (GA-1, P2): once a walkthrough case fails
    #: for real (blocked, or a write step landed !ok when the case's own
    #: expectation was NOT a typed_error -- i.e. it was not scripted to
    #: refuse), every LATER case in the same walkthrough is scored against
    #: state the script never intended. Tag every case after the break so
    #: triage can tell a cascade from the root cause; never re-tag the
    #: breaking case itself with its own id.
    if walkthrough and getattr(session, "walkthrough_broken", None) is not None:
        obs.extra["walkthrough_prefix_broken"] = session.walkthrough_broken
    role = case.frame_role or "hero"
    #: (night1-robustness-triage.md item 2; fix round 1 review M2): ping
    #: BEFORE ensure_frame/isolate, not after. `isolate` alone can send one
    #: `darkroom_reset` per module the previous case touched, each its own
    #: 12+30s budget -- against an inherited wedge that alone can burn the
    #: hard cap (the rob-r3 shape) before a post-isolate ping ever ran.
    #: `pre_ping=False` (fix round 1 review B1): `run_fault_case` passes this
    #: for a fault that just froze darktable ON PURPOSE (`_FREEZE_FAULTS`) --
    #: that unresponsiveness is the fault working as intended, not an
    #: inherited wedge, and `run_fault_case`'s own pre-arm `isolate` already
    #: ran, responsively, before the freeze. `dt` may be absent on a minimal
    #: test double; real `ScouterSession` always has it.
    if pre_ping:
        dt = getattr(session, "dt", None)
        if dt is not None and dt.health() == "unresponsive":
            obs.blocked = "inherited_wedge"
            obs.hang_evidence.append(
                "darktable was already unresponsive before this case's "
                "own steps ran (inherited from an earlier case)"
            )
            _mark_walkthrough_broken(session, case, obs, walkthrough)
            obs.wall_s = clock() - t0
            return obs
    try:
        frame = session.ensure_frame(role)
        if not continuing and isolate(session, frame, role):
            frame = session.ensure_frame(role)
            obs.extra["contaminated_restaged"] = True
    except SessionBlocked as e:
        obs.blocked = f"session: {e}"
        obs.hang_evidence.append(f"session: {e}")
        _mark_walkthrough_broken(session, case, obs, walkthrough)
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
        previews["before"] = observe.render_preview(
            frame, session.previews_dir / case.id / "before.png"
        )
    for st in case.steps:
        args = substitute(st.args, session, obs.calls)
        if st.tool in VIRTUAL:
            try:
                rec = VIRTUAL[st.tool](session, case, st, obs)
            except Park:
                raise
            except subprocess.TimeoutExpired:
                obs.blocked = f"hang: {st.tool} exceeded budget"
                obs.hang_evidence.append(
                    f"{st.tool}: no answer in {st.budget_s + 30:.0f}s " "(subprocess timeout)"
                )
                break
            except Exception as e:  # noqa: BLE001 - a virtual tool crash must not crash the case
                obs.blocked = f"harness_error: {st.tool}: {type(e).__name__}: {e}"
                break
        else:
            rec = session.call(st.tool, args, st.budget_s, write=st.write)
        obs.calls.append(rec)
        #: a workflow case's later `{job_frames}` step must see where
        #: library_import actually wrote its files, not the staged copies.
        resolve_import_job_frames(obs, session, rec, session.staged.job_frames)
        if obs.blocked:  # (final fix M3): a virtual tool blocked the case itself
            break
        if rec.error == "timeout" and rec.health == "unresponsive":
            obs.blocked = "hang"
            obs.hang_evidence.append(
                f"{st.tool}: no answer in {st.budget_s + 30:.0f}s, " "ping unresponsive"
            )
            break
        if rec.envelope and (
            rec.envelope.get("healed") or (rec.envelope.get("data") or {}).get("healed")
        ):
            obs.healed = True
        snaps.append(observe.snapshot(frame, kelvin)["params"] if st.write else snaps[-1])
    obs.after = observe.snapshot(frame, kelvin)
    obs.extra["snapshots"] = snaps
    exported = export_files_count(obs)
    if exported is not None:
        obs.extra["export_files"], obs.extra["export_files_oracles"] = exported
    if stats and not obs.blocked:
        raster = observe.render_raster(frame, session.root / "render" / "after.ppm")
        obs.stats_after = chart.patch_stats(raster) if is_chart else observe.stats(raster, masks)[0]
        previews["after"] = observe.render_preview(
            frame, session.previews_dir / case.id / "after.png"
        )
        obs.extra["previews"] = {k: session.relative(v) for k, v in previews.items()}
    _mark_walkthrough_broken(session, case, obs, walkthrough)
    obs.wall_s = clock() - t0
    return obs
