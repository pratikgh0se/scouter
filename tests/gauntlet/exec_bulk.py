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
from tests.gauntlet.execute import substitute
from tests.gauntlet.session import SessionBlocked

STATS_SAMPLE = 10


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
    session.staged.vars["job_slug"] = f"htc-{case.id}"[-40:]
    obs = Observation()
    try:
        before = {str(f): observe.snapshot(f).get("fingerprint") for f in frames}
        kill_at = case.meta.get("kill_after_frame")
        killer = None
        if kill_at:
            killer = threading.Thread(target=_kill_after, args=(session, kill_at), daemon=True)
            killer.start()
        try:
            for st in case.steps:
                args = substitute(st.args, session, obs.calls)
                rec = session.call(st.tool, args, st.budget_s, write=st.write)
                obs.calls.append(rec)
                if kill_at and rec.error:
                    session.relaunch()
                    again = session.call(st.tool, args, st.budget_s, write=st.write)
                    obs.calls.append(again)
                    obs.extra["journal_resumed"] = "resum" in json.dumps(again.envelope or {})
        except SessionBlocked as e:
            obs.blocked = f"session: {e}"
            obs.hang_evidence.append(f"session: {e}")
        job_files = [f for f in frames if f.exists()]
        obs.extra["changed_n"] = count_changed(job_files, before)
        obs.extra["reported_n"] = reported_n([c.envelope for c in obs.calls if c.write])
        out_dir = Path(session.staged.vars["out_dir"])
        obs.extra["export_files"] = sum(1 for _ in out_dir.rglob("*") if _.is_file()) \
            if out_dir.exists() else 0
        obs.extra["imported_n"] = reported_n([c.envelope for c in obs.calls
                                              if c.tool == "library_import"]) or 0
        obs.extra["frames_verified"] = max(obs.extra["changed_n"], obs.extra["export_files"])
        obs.extra["exactly_once"] = _exactly_once(frames)
        obs.extra["stats_ok_n"] = min(STATS_SAMPLE, obs.extra["changed_n"])
    finally:
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


def _kill_after(session, frame_n: int, poll_s: float = 1.0, cap_s: float = 1800) -> None:
    """SIGKILL the OWNED darktable once the job journal reports frame `frame_n`."""
    import os
    import signal

    from scouter.core import dtprocess

    events = Path(session.staged.env["SCOUTER_CACHE"]) / "events.jsonl"
    t0 = time.monotonic()
    while time.monotonic() - t0 < cap_s:
        if events.exists() and events.read_text(errors="replace").count('"frame"') >= frame_n:
            pid = dtprocess.owned_pid()
            if pid:
                os.kill(pid, signal.SIGKILL)
            return
        time.sleep(poll_s)
