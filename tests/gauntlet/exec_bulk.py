"""Layer 3 executor (spec 3.3): stage the card by duplication, run the flow
through MCP, then count what really changed (the silent-failure (d) facts)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from gauntlet.adapter import Observation
from gauntlet.case import Case
from scouter import config as scouter_config
from tests.gauntlet import observe
from tests.gauntlet.corpus import build_card, delete_card
from tests.gauntlet.execute import substitute
from tests.gauntlet.jobslug import job_slug_for
from tests.gauntlet.session import SessionBlocked

STATS_SAMPLE = 10

#: Re-exported for callers that used to import `job_slug_for` from this
#: module (now the shared `tests.gauntlet.jobslug`, since
#: `tests/gauntlet/session.py` needs it too).
__all__ = ["job_slug_for", "ImportIncomplete", "run_bulk_case", "reported_n", "count_changed"]


class ImportIncomplete(RuntimeError):
    """`library_import`'s own envelope did not account for every source
    frame the harness staged (a partial import). The destination for the
    missing frame(s) is genuinely unknown -- guessing the card path would
    only surface downstream as a false `usage`/`not_in_darkroom` refusal on
    a frame that was never the harness's fault. Raised, never swallowed:
    `run_bulk_case` turns it into `obs.blocked`, so the case scores
    `blocked`, honestly naming what's missing."""


def _imported_frames(session, envelope: dict | None,
                      source_frames: list[Path]) -> list[Path] | None:
    """Map `source_frames` (the staged card copies) to the absolute paths
    `library_import` actually wrote them to, using ITS OWN envelope
    (`data.files[].source_name` -> `.dest`, relative to the job directory --
    spec section 6, `scouter.types.ImportResult`). Returns `None` when the
    envelope carries nothing usable at all (the import did not run or
    errored -- callers keep the card paths, since a downstream step will
    honestly fail on its own terms). Raises `ImportIncomplete` when the
    import DID run but its own `files[]` is missing one or more of the
    frames the harness staged (a partial import): the destination for
    those frames is unknown, not "the card path", and must never be
    guessed. Every later step (`{job_frame:N}`, `{job_frames}`) must
    address these destination files: the card copies are deleted at the
    end of the case and were never what darktable's library, style/preset
    verbs or exports operate on."""
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
            for st in case.steps:
                args = substitute(st.args, session, obs.calls)
                rec = session.call(st.tool, args, st.budget_s, write=st.write)
                obs.calls.append(rec)
                if kill_at and rec.error:
                    session.relaunch()
                    rec = session.call(st.tool, args, st.budget_s, write=st.write)
                    obs.calls.append(rec)
                    obs.extra["journal_resumed"] = "resum" in json.dumps(rec.envelope or {})
                if st.tool == "library_import" and not rec.error:
                    try:
                        resolved = _imported_frames(session, rec.envelope, frames)
                    except ImportIncomplete as e:
                        obs.blocked = f"harness_error: {e}"
                        obs.hang_evidence.append(str(e))
                        break
                    if resolved is not None:
                        job_files = resolved
                        session.staged.job_frames = resolved
                        before = {str(f): observe.snapshot(f).get("fingerprint")
                                  for f in resolved}
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
