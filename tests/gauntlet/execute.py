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


def _imported_frames(session, envelope: dict | None,
                      source_frames: list[Path]) -> list[Path] | None:
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


def resolve_import_job_frames(obs: Observation, session, rec: CallRecord,
                              source_frames: list[Path]) -> list[Path] | None:
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
    role = case.frame_role or "hero"
    try:
        frame = session.ensure_frame(role)
        if isolate(session, frame, role):
            frame = session.ensure_frame(role)
            obs.extra["contaminated_restaged"] = True
    except SessionBlocked as e:
        obs.blocked = f"session: {e}"
        obs.hang_evidence.append(f"session: {e}")
        obs.wall_s = clock() - t0
        return obs
    #: Per-case undo journal isolation -- see session.reset_journal's own
    #: docstring. Must run AFTER isolate() (whose own darkroom_reset calls, for a
    #: module the PREVIOUS case touched, write their own journal entries)
    #: and any re-stage above, but before this case's own before-snapshot/
    #: steps below, so no earlier entry -- isolate's own or an earlier
    #: case's -- can leak into THIS case's darkroom_undo_last. The count is
    #: kept in obs.extra so a leak stays visible rather than disappearing.
    if session.staged is not None:
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
        #: a workflow case's later `{job_frames}` step must see where
        #: library_import actually wrote its files, not the staged copies.
        resolve_import_job_frames(obs, session, rec, session.staged.job_frames)
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
