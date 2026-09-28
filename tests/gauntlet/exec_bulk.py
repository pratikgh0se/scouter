"""Layer 3 executor (spec 3.3): stage the card by duplication, run the flow
through MCP, then count what really changed (the silent-failure (d) facts)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from gauntlet.adapter import Observation
from gauntlet.case import Case
from tests.gauntlet import observe
from tests.gauntlet.corpus import build_card, delete_card
from tests.gauntlet.execute import resolve_import_job_frames, substitute
from tests.gauntlet.jobslug import job_slug_for
from tests.gauntlet.session import SessionBlocked

STATS_SAMPLE = 10

#: Re-exported for callers that used to import `job_slug_for` from this
#: module (now the shared `tests.gauntlet.jobslug`, since
#: `tests/gauntlet/session.py` needs it too).
__all__ = ["job_slug_for", "run_bulk_case", "reported_n", "count_changed"]


def reported_n(envelopes: list[dict]) -> int | None:
    """The largest per-frame count any bulk envelope claims."""
    best = None
    for env in envelopes:
        data = (env or {}).get("data") or {}
        for key in ("applied", "done", "exported", "imported", "frames", "results", "images"):
            v = data.get(key)
            n = len(v) if isinstance(v, list) else v if isinstance(v, int) else None
            if n is not None:
                best = max(best or 0, n)
    return best


def count_changed(frames: list[Path], before: dict[str, str]) -> int:
    return sum(1 for f in frames
               if observe.snapshot(f).get("fingerprint") != before.get(str(f)))


def run_bulk_case(session, case: Case) -> Observation:
    t0 = time.monotonic()
    n = case.meta["frames"]
    card = session.root / "bulk" / case.id / "card"
    frames = build_card(session.corpus, case.body, n, card)
    session.staged.job_frames = frames
    session.staged.vars["card"] = str(card)
    session.staged.vars["job_slug"] = job_slug_for(case.id)
    # (n1-bulk fix c): `library_relocate` refuses a `new_root` that does not
    # already exist -- that is the product's own contract (an operator must
    # prepare the destination), so the harness creates it up front rather
    # than asking the product to relax a deliberate safety check.
    Path(session.staged.vars["relocate_root"]).mkdir(parents=True, exist_ok=True)
    obs = Observation()
    # (final fix M1): the kill timer must never outlive its case (it could
    # SIGKILL darktable in a later case); set in `finally`.
    stop_killer = threading.Event()
    # job_files/before start on the card copies (the `time_lapse_deflicker`
    # refusal case never imports, so these stay the honest baseline); the
    # first successful `library_import` step below replaces both with the
    # destination files it actually wrote.
    job_files = frames
    try:
        before = {str(f): observe.snapshot(f).get("fingerprint") for f in frames}
        kill_at = case.meta.get("kill_after_frame")
        killer = None
        if kill_at:
            killer = threading.Thread(target=_kill_after, args=(session, kill_at),
                                      kwargs={"stop": stop_killer}, daemon=True)
            killer.start()
        try:
            bridge_relaunched = False
            last_image = None
            for st in case.steps:
                args = substitute(st.args, session, obs.calls)
                if "image" in args:
                    last_image = args["image"]
                rec = session.call(st.tool, args, st.budget_s, write=st.write)
                obs.calls.append(rec)
                if kill_at and rec.error:
                    session.relaunch()
                    session.current_frame = None  # relaunch is launched, not current
                    rec = session.call(st.tool, args, st.budget_s, write=st.write)
                    obs.calls.append(rec)
                    obs.extra["journal_resumed"] = "resum" in json.dumps(rec.envelope or {})
                elif rec.error in ("bridge_timeout", "commit_timeout", "not_in_darkroom"):
                    # One relaunch per case; a write step's commit_timeout is
                    # never blind-retried since its edit may have landed.
                    if bridge_relaunched:
                        obs.blocked = (f"bridge_relaunch_failed: {st.tool} {rec.error} again "
                                      "after the one relaunch this case already used")
                        obs.hang_evidence.append(obs.blocked)
                        break
                    bridge_relaunched = True
                    session.relaunch(Path(last_image) if last_image else None)
                    session.current_frame = None  # relaunch is launched, not current
                    if st.write and rec.error == "commit_timeout":
                        obs.blocked = (f"bridge_relaunch_failed: {st.tool} commit_timeout on a "
                                      "write step is not retried after a relaunch (it may have "
                                      "already landed)")
                        obs.hang_evidence.append(obs.blocked)
                        break
                    rec = session.call(st.tool, args, st.budget_s, write=st.write)
                    obs.calls.append(rec)
                    if rec.error in ("bridge_timeout", "commit_timeout", "not_in_darkroom"):
                        obs.blocked = (f"bridge_relaunch_failed: {st.tool} still {rec.error} "
                                      "after a relaunch")
                        obs.hang_evidence.append(obs.blocked)
                        break
                resolved = resolve_import_job_frames(obs, session, rec, frames)
                if obs.blocked:
                    break
                if resolved is not None:
                    job_files = resolved
                    before = {str(f): observe.snapshot(f).get("fingerprint") for f in resolved}
        except SessionBlocked as e:
            obs.blocked = f"session: {e}"
            obs.hang_evidence.append(f"session: {e}")
        existing = [f for f in job_files if f.exists()]
        obs.extra["changed_n"] = count_changed(existing, before)
        # (n1-bulk fix e): `library_import`'s own envelope claims `imported`
        # files every time it runs -- folding it into the max over every
        # write envelope made `reported_n` (the harness's "did anything
        # claim it wrote N frames" signal) true on an import-only case even
        # when every real bulk verb after it errored, scoring a genuine
        # failure as `silent_failure` instead. `imported_n` below already
        # tracks the import's own count on its own terms.
        obs.extra["reported_n"] = reported_n([c.envelope for c in obs.calls
                                              if c.write and c.tool != "library_import"])
        out_dir = Path(session.staged.vars["out_dir"])
        obs.extra["export_files"] = sum(1 for _ in out_dir.rglob("*") if _.is_file()) \
            if out_dir.exists() else 0
        obs.extra["imported_n"] = reported_n([c.envelope for c in obs.calls
                                              if c.tool == "library_import"]) or 0
        obs.extra["frames_verified"] = max(obs.extra["changed_n"], obs.extra["export_files"])
        obs.extra["exactly_once"] = _exactly_once(job_files)
        obs.extra["stats_ok_n"] = min(STATS_SAMPLE, obs.extra["changed_n"])
    finally:
        stop_killer.set()
        delete_card(card)
    obs.wall_s = time.monotonic() - t0
    return obs


def _exactly_once(frames: list[Path]) -> bool:
    """No sidecar carries the same operation+params twice in a row (a double apply)."""
    from scouter.core import xmp

    for f in frames:
        side = xmp.sidecar_path(f)
        if not side.exists():
            continue
        entries = xmp.parse(side.read_text(encoding="utf-8", errors="replace")).entries
        for a, b in zip(entries, entries[1:]):
            if a.operation == b.operation and a.params_hex == b.params_hex:
                return False
    return True


def _kill_after(session, frame_n: int, poll_s: float = 1.0, cap_s: float = 1800,
                stop: threading.Event | None = None) -> None:
    """SIGKILL the OWNED darktable once the job journal reports frame `frame_n`;
    returns without signalling once `stop` is set (final fix M1)."""
    import os
    import signal

    from scouter.core import dtprocess

    events = Path(session.staged.env["SCOUTER_CACHE"]) / "events.jsonl"
    t0 = time.monotonic()
    stop = stop or threading.Event()
    while time.monotonic() - t0 < cap_s and not stop.is_set():
        if events.exists() and events.read_text(errors="replace").count('"frame"') >= frame_n:
            if stop.is_set():
                return
            pid = dtprocess.owned_pid()
            if pid:
                os.kill(pid, signal.SIGKILL)
            return
        stop.wait(poll_s)
