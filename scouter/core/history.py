"""History: read it from the XMP, and make darktable write the XMP.

Phase 0 finding 3: darktable commits history and the sidecar when the darkroom
is LEFT, not per slider move. So `commit` is a first-class verb and every
writer calls it by default. The strategy order is set by task 1's measurement
(docs/measurements.md); `view_bounce` is the proven floor, so commit cannot
fail for lack of a mechanism -- only for lack of time.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from darktable_mcp.bridge.client import BridgeTimeoutError
from scouter import metrics, paths
from scouter.core import dtbridge, xmp
from scouter.errors import (
    BridgeTimeout,
    CommitTimeout,
    DarktableUnresponsive,
    DbLocked,
    NoSidecar,
    NotInDarkroom,
    UsageError,
)
from scouter.registry import ArgSpec, Verb, register
from scouter.types import ChangedResult, CommitResult, HistoryEntry, HistoryResult

POLL_INTERVAL_S = 0.05

#: Mirrors darktable_mcp.lua's COMMIT_BOUNCE_ATTEMPTS -- the default number of
#: view-bounce attempts a commit spends when the caller does not say
#: otherwise. A caller who already knows the write may be a no-op (the
#: `darkroom.set_param` pre-check, task 12) asks for `attempts=1` and
#: `expect_change=False` instead, and pays one bounce, not two.
DEFAULT_COMMIT_ATTEMPTS = 2
#: Row 431: how much sooner than the caller's `timeout_s` the plugin's own
#: `budget_ms` runs out, so its answer (and the sidecar poll after it) still
#: lands inside the caller's wait.
_BRIDGE_MARGIN_S = 0.5

#: Every strategy the plugin's `history.commit` accepts (row 422 added
#: `view_bounce_action` and `no_bounce`; see darktable_mcp.lua).
COMMIT_STRATEGIES = ("auto", "sidecar_action", "view_bounce", "view_bounce_action", "no_bounce")

#: Where `history.read` takes the history from.
READ_SOURCES = ("xmp", "db", "auto")

#: Attached to every library.db read. Source-verified (darktable 5.4.1,
#: src/develop/develop.c and src/views/darkroom.c): the darkroom writes its
#: in-memory history to library.db at the SAME moments it writes the XMP --
#: autosave (a history change at least `autosave_interval`, default 10s,
#: after the previous autosave), leaving the darkroom, switching image, or
#: "synchronize selection". So the DB is not fresher than the sidecar for an
#: image open in the darkroom; what it removes is the dependence on the
#: sidecar FILE (missing, mid-write, or "write sidecar files" turned off).
DB_LAG_WARNING = (
    "read from darktable's library.db: it lags the darkroom's in-memory history exactly "
    "like the XMP does (both are written on autosave, on leaving the darkroom, or on "
    "synchronize selection), so an edit made seconds ago may not be here yet"
)


def _running_darktable_library() -> Path | None:
    """`--library <path>` from a running darktable GUI's command line, or None.

    The host test harness launches darktable with an explicit `--library`, and
    the plugin's Lua API exposes no library path, so the process table is the
    one place that says which library.db the live session is using. `ps`
    flattens quoting, so a library path containing spaces is not recovered
    here -- pass `library=` (or $SCOUTER_DARKTABLE_LIBRARY_DB) for that.
    """
    try:
        out = subprocess.run(
            ["ps", "-Ao", "args="], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        tokens = line.split()
        if not tokens or "darktable" not in tokens[0] or "darktable-" in tokens[0]:
            continue
        for i, token in enumerate(tokens):
            if token == "--library" and i + 1 < len(tokens):
                return Path(tokens[i + 1]).expanduser()
            if token.startswith("--library="):
                return Path(token.split("=", 1)[1]).expanduser()
    return None


def darktable_library_db(library: str | Path | None = None) -> tuple[Path, str]:
    """(path, how_it_was_found) for darktable's library.db, or `UsageError`.

    Order: an explicit `library`, then `$SCOUTER_DARKTABLE_LIBRARY_DB`, then
    the `--library` of a running darktable, then the default locations
    (`backup._darktable_library_db_candidates`, which already knows this
    Mac's real path).
    """
    from scouter.core.backup import _darktable_library_db_candidates

    tried: list[str] = []
    candidates: list[tuple[str, Path]] = []
    if library:
        candidates.append(("library argument", Path(library).expanduser()))
    env = os.environ.get("SCOUTER_DARKTABLE_LIBRARY_DB")
    if env:
        candidates.append(("$SCOUTER_DARKTABLE_LIBRARY_DB", Path(env).expanduser()))
    running = _running_darktable_library()
    if running is not None:
        candidates.append(("running darktable --library", running))
    candidates.extend(
        (label, path)
        for label, path in _darktable_library_db_candidates()
        if label != "$SCOUTER_DARKTABLE_LIBRARY_DB"
    )
    for label, path in candidates:
        if path.is_file():
            return path, label
        tried.append(f"{label}: {path}")
    raise UsageError(
        "no darktable library.db found (tried " + "; ".join(tried) + ")",
        fix="pass --library <path to library.db> or set SCOUTER_DARKTABLE_LIBRARY_DB",
        detail={"tried": tried},
    )


#: darktable stores `images.change_timestamp` as a GTimeSpan: microseconds
#: since 0001-01-01 00:00 UTC (src/common/datetime.h, DT_DATETIME_ORIGIN).
#: Checked against a real 5.4.1 library.db (row 429): change_timestamp
#: 63926003002096029 -> unix 1790406202.1, write_timestamp 1790406204.
GTIMESPAN_UNIX_OFFSET_S = 62135596800

#: `source="auto"` reads the DB instead of the XMP when library.db's last
#: history change is more than this many seconds newer than the sidecar's
#: mtime. Slack for darktable writing the DB first and the XMP right after.
STALE_SLACK_S = 1.0


def db_changed_at(image_id: int, library: str | Path | None = None) -> float | None:
    """Unix time of library.db's last history change for this image
    (`images.change_timestamp`), or None when it cannot be read -- no
    library found, locked, an old schema, or a never-changed image (-1).
    Read-only; never raises."""
    try:
        db_path, _how = darktable_library_db(library)
    except UsageError:
        return None
    try:
        conn = sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT change_timestamp FROM main.images WHERE id = ?", (int(image_id),)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None or row[0] is None:
        return None
    try:
        raw = int(row[0])
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    unix = raw / 1_000_000 - GTIMESPAN_UNIX_OFFSET_S
    # Anything outside 2000-01-01 .. far future is not a timestamp we understand.
    if not 946684800 <= unix <= 4102444800:
        return None
    return unix


def _entry_from_db_row(row: tuple, history_end: int) -> HistoryEntry:
    (
        num,
        modversion,
        operation,
        op_params,
        enabled,
        blendop_params,
        blendop_version,
        multi_priority,
        multi_name,
        multi_name_hand_edited,
    ) = row
    blob = bytes(op_params or b"")
    decoded, reason = xmp.decode_module(operation or "", int(modversion or 0), blob)
    return HistoryEntry(
        num=int(num),
        operation=operation or "",
        enabled=bool(enabled),
        modversion=int(modversion or 0),
        multi_name=multi_name or "",
        multi_priority=int(multi_priority or 0),
        active=int(num) < history_end,
        # Plain lowercase hex -- one of the two encodings `xmp.decode_params`
        # (and darktable's own XMP reader) accepts.
        params_hex=blob.hex(),
        decoded=decoded,
        reason=reason,
        blendop_params_hex=bytes(blendop_params or b"").hex(),
        blendop_version=int(blendop_version or 0),
        multi_name_hand_edited=bool(multi_name_hand_edited),
    )


@metrics.instrumented("history.read_db")
def read_db(
    image: str | int, *, library: str | Path | None = None, job: str | None = None
) -> HistoryResult:
    """The image's history straight from darktable's library.db (read-only).

    Row 422: removes the dependence on the XMP file. It does NOT see
    history the darkroom has not flushed yet -- see `DB_LAG_WARNING`.
    Opened `mode=ro`; darktable keeps the file open and may be writing, so a
    lock is reported as `db_locked`, never waited out forever.
    """
    resolved = dtbridge.resolve_image(image)
    db_path, found_via = darktable_library_db(library)
    uri = f"file:{quote(str(db_path))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as e:
        raise DbLocked(
            f"could not open {db_path} read-only: {e}", detail={"db": str(db_path)}
        ) from e
    try:
        image_row = conn.execute(
            "SELECT history_end FROM main.images WHERE id = ?", (resolved.image_id,)
        ).fetchone()
        if image_row is None:
            raise UsageError(
                f"image {resolved.image_id} is not in {db_path} ({found_via}) -- that is not "
                "the library the running darktable is using",
                fix="pass --library <the library.db darktable was started with>",
                detail={"db": str(db_path), "image_id": resolved.image_id},
            )
        history_end = int(image_row[0] or 0)
        rows = conn.execute(
            "SELECT num, module, operation, op_params, enabled, blendop_params, "
            "blendop_version, multi_priority, multi_name, multi_name_hand_edited "
            "FROM main.history WHERE imgid = ? ORDER BY num",
            (resolved.image_id,),
        ).fetchall()
        iop_order_version = 0
        try:
            order = conn.execute(
                "SELECT version FROM main.module_order WHERE imgid = ?", (resolved.image_id,)
            ).fetchone()
            if order is not None:
                iop_order_version = int(order[0] or 0)
        except sqlite3.OperationalError:
            pass
        current_hash = None
        try:
            hashed = conn.execute(
                "SELECT current_hash FROM main.history_hash WHERE imgid = ?", (resolved.image_id,)
            ).fetchone()
            if hashed is not None and hashed[0] is not None:
                current_hash = bytes(hashed[0]).hex()
        except sqlite3.OperationalError:
            pass
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            raise DbLocked(f"{db_path} is locked: {e}", detail={"db": str(db_path)}) from e
        raise UsageError(
            f"{db_path} does not look like a darktable library.db: {e}",
            detail={"db": str(db_path)},
        ) from e
    finally:
        conn.close()

    entries = [_entry_from_db_row(row, history_end) for row in rows]
    warnings = [DB_LAG_WARNING + f" ({found_via}: {db_path})"]
    undecoded = sorted({e.operation for e in entries if e.decoded is None})
    if undecoded:
        warnings.append(
            "params returned as opaque hex (no verified layout): " + ", ".join(undecoded)
        )
    return HistoryResult(
        image_id=resolved.image_id,
        path=resolved.path,
        history_end=history_end,
        iop_order_version=iop_order_version,
        current_hash=current_hash,
        entries=entries,
        source="db",
        confirmed=True,
        warnings=warnings,
    )


@metrics.instrumented("history.read")
def read(
    image: str | int,
    *,
    source: str | None = None,
    library: str | None = None,
    job: str | None = None,
) -> HistoryResult:
    """Parse the image's sidecar. `confirmed=False` when it is mid-write.

    `image` is required (controller ruling 10): unlike `commit`, there is no
    darkroom fallback here, because a caller reading history almost always
    means a specific image, and `darkroom.describe_history` already resolves
    the darkroom's current image itself before delegating to this function.
    Every entry keeps its own `enabled` flag from the XMP (ruling 20), so a
    disabled module in the history is never mistaken for a live one.

    `source` (row 422): "xmp" (the default, unchanged), "db" (`read_db`), or
    "auto" (row 429) -- the XMP unless it is stale, then library.db. Stale
    means: no sidecar, a sidecar caught mid-write, or a sidecar whose mtime
    is older than library.db's `change_timestamp` for the image (darktable
    flushed history to the DB -- autosave, synchronize selection -- without
    writing the XMP yet). When library.db cannot be read, auto is the XMP.
    """
    # rob-r5: only an EXPLICIT source="xmp" treats a missing sidecar as an
    # error; the unset default keeps the lenient "no edits yet" answer.
    strict_xmp = source == "xmp"
    if source is None:
        source = "xmp"
    if source not in READ_SOURCES:
        raise UsageError(f"unknown source {source!r} (valid: {', '.join(READ_SOURCES)})")
    if source == "db":
        return read_db(image, library=library, job=job)
    resolved = dtbridge.resolve_image(image)
    try:
        text, mtime = xmp.read_sidecar(resolved.path)
    except FileNotFoundError:
        if source == "auto":
            result = read_db(resolved.image_id, library=library, job=job)
            result.warnings.insert(0, f"no sidecar at {resolved.xmp_path}; fell back to library.db")
            return result
        if strict_xmp:
            raise NoSidecar(
                f"no sidecar at {resolved.xmp_path}: source=xmp was requested explicitly",
                detail={"path": resolved.path, "xmp_path": str(resolved.xmp_path)},
            )
        return HistoryResult(
            image_id=resolved.image_id,
            path=resolved.path,
            source="xmp",
            confirmed=False,
            warnings=[
                f"no sidecar at {resolved.xmp_path}: darktable has not committed this "
                "image yet (run `scouter history commit`), or 'write sidecar file for "
                "each image' is off in darktable's preferences"
            ],
        )
    parsed = xmp.parse(text)
    warnings: list[str] = []
    if source == "auto":
        stale_reason = None
        if not parsed.well_formed:
            stale_reason = f"the sidecar at {resolved.xmp_path} was mid-write"
        else:
            changed_at = db_changed_at(resolved.image_id, library)
            if changed_at is not None and changed_at > mtime + STALE_SLACK_S:
                stale_reason = (
                    f"the sidecar at {resolved.xmp_path} is {changed_at - mtime:.1f}s older than "
                    "library.db's last history change for this image"
                )
        if stale_reason is not None:
            try:
                result = read_db(resolved.image_id, library=library, job=job)
            except (UsageError, DbLocked) as e:
                warnings.append(f"{stale_reason}, but library.db could not be read ({e.message})")
            else:
                result.warnings.insert(0, f"{stale_reason}; read library.db instead")
                return result
    if not parsed.well_formed:
        warnings.append(
            "sidecar was mid-write: parsed with the regex fallback, retry after the "
            "mtime settles for a complete history"
        )
    undecoded = sorted({e.operation for e in parsed.entries if e.decoded is None})
    if undecoded:
        warnings.append(
            "params returned as opaque hex (no verified layout): " + ", ".join(undecoded)
        )
    return HistoryResult(
        image_id=resolved.image_id,
        path=resolved.path,
        history_end=parsed.history_end,
        xmp_version=parsed.xmp_version,
        iop_order_version=parsed.iop_order_version,
        current_hash=parsed.current_hash,
        xmp_mtime=mtime,
        entries=parsed.entries,
        source="xmp",
        confirmed=parsed.well_formed,
        warnings=warnings,
    )


def _signature(xmp_path: str) -> dict | None:
    p = Path(xmp_path)
    if not p.exists():
        return None
    st = p.stat()
    return {"mtime_ns": st.st_mtime_ns, "size": st.st_size}


def watermark_write(job: str, image_id: int, xmp_path: str) -> dict:
    """Record the sidecar's signature after a CONFIRMED commit.

    Precondition (fix round 1, should-fix 4): Scouter is the only writer
    touching this image's history between commits. The watermark is a pure
    FILE signature (mtime + size) -- it proves the sidecar's bytes have not
    changed since Scouter last committed, nothing more. It cannot see
    darktable's in-memory history: an edit made directly in the GUI (a
    slider moved by hand, never through Scouter) leaves the sidecar
    untouched until the NEXT commit, so the watermark would still read
    "fresh" even though the live history has already diverged from disk.
    Scouter's own uncommitted writes are guarded separately -- by the
    journal, not the watermark (`darkroom.py`'s `pending_uncommitted` check,
    task 12 fix round 1, blocker 1). The watermark alone only ever proves
    "this exact file", never "this is still what darktable's memory holds".

    Callers must only call this after independently confirming the sidecar
    really is in the state being recorded -- either `history.commit`'s own
    poll (a real, observed mtime/size change) or an explicit readback
    against the requested value (never from an UNCONFIRMED `expect_change=
    False` result: see `commit`'s docstring and `CommitResult.confirmed`).

    A read-modify-write on the shared per-job state file, under
    `paths.job_state_update`'s lock (fix round 1, should-fix 5):
    `darkroom.py`'s `_record_opened_image` writes the same file, possibly
    concurrently, and each write must preserve the other's keys.
    """
    sig = _signature(xmp_path)
    key = str(int(image_id))

    def _mutate(state: dict) -> None:
        watermarks = dict(state.get("watermarks") or {})
        if sig is None:
            watermarks.pop(key, None)
        else:
            watermarks[key] = sig
        state["watermarks"] = watermarks

    paths.job_state_update(job, _mutate)
    return sig or {}


def watermark_fresh(job: str, image_id: int, xmp_path: str) -> tuple[bool, str | None]:
    """(fresh, reason_when_not). No watermark -> (False, "no watermark for this
    image in job <job>"): a fresh session, or an edit Pratik made by hand, both
    correctly fall through to the safe path."""
    state = paths.job_state_read(job)
    watermarks = state.get("watermarks") or {}
    stored = watermarks.get(str(int(image_id)))
    if stored is None:
        return False, f"no watermark for this image in job {job}"
    current = _signature(xmp_path)
    if current is None:
        return False, f"no sidecar at {xmp_path} any more; the watermark no longer applies"
    if current.get("mtime_ns") == stored.get("mtime_ns") and current.get("size") == stored.get(
        "size"
    ):
        return True, None
    return False, (
        f"the sidecar for image {image_id} changed since Scouter last committed it "
        "(mtime/size no longer match the watermark)"
    )


def _sidecar_signature(sidecar) -> tuple[int, int]:
    """`(mtime_ns, size)` -- the two-part change guard fix round 1 asked for.

    A mtime advance alone (spec 8.1 step 1) is the primary signal; size is
    kept alongside it because a filesystem whose mtime resolution is coarser
    than one poll interval could report an unchanged mtime for a write that
    landed inside the same tick. Either signal moving is treated as a change;
    both signals unchanged is treated as no change yet.
    """
    if not sidecar.exists():
        return (0, -1)
    stat = sidecar.stat()
    return (stat.st_mtime_ns, stat.st_size)


_HISTORY_MARKERS = (
    re.compile(r'darktable:history_end="[^"]*"'),
    re.compile(r'darktable:history_current_hash="[^"]*"'),
    re.compile(r"<darktable:masks_history>.*?</darktable:masks_history>", re.S),
    re.compile(r"<darktable:history>.*?</darktable:history>", re.S),
)


def _history_fingerprint(sidecar: Path) -> str | None:
    """The part of a sidecar that IS the edit, as text; `None` if unreadable.

    Row 430: a metadata-only rewrite (the no_bounce rating touch re-synching
    the XMP from a library.db the darkroom never flushed to) moves
    `change_timestamp` -- so mtime and size -- while the history stays
    byte-identical. A commit is proven by the history changing, not by the
    file being touched. Mirrors the Lua plugin's `sidecar_history_signature`.
    A file with none of the markers is compared whole.
    """
    try:
        text = sidecar.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    parts = [m.group(0) for rx in _HISTORY_MARKERS if (m := rx.search(text))]
    return "\n".join(parts) if parts else text


_LI_RX = re.compile(r"<rdf:li\s.*?/>", re.S)
_NUM_RX = re.compile(r'darktable:num="(\d+)"')
_OP_RX = re.compile(r'darktable:operation="([^"]*)"')
_END_RX = re.compile(r'darktable:history_end="(\d+)"')
_HISTORY_BLOCK_RX = re.compile(r"<darktable:history>(.*?)</darktable:history>", re.S)


def _history_items(fingerprint: str | None) -> tuple[int | None, dict[int, str]]:
    if fingerprint is None:
        return None, {}
    m = _END_RX.search(fingerprint)
    block = _HISTORY_BLOCK_RX.search(fingerprint)
    items: dict[int, str] = {}
    for li in _LI_RX.findall(block.group(1) if block else ""):
        num = _NUM_RX.search(li)
        if num:
            items[int(num.group(1))] = li
    return (int(m.group(1)) if m else None), items


def _history_end_went_back(before: str | None, after: str | None) -> str | None:
    """Row 461 rule 1, on EVERY commit (with or without `expect_module`): a
    sidecar whose history_end went back was rewritten from an older
    library.db state and is never a commit. The reason, or `None`.
    Mirrors the Lua plugin's `history_end_went_back`.
    Enforcer: tests/test_history_expected.py (StaleRewriteTest)."""
    before_end, _ = _history_items(before)
    after_end, _ = _history_items(after)
    if before_end is not None and after_end is not None and after_end < before_end:
        return (
            f"history_end went back from {before_end} to {after_end}: the sidecar was "
            "rewritten from an older library.db state, not from this edit"
        )
    return None


def _restore_sidecar(sidecar: Path, text: str) -> bool:
    """Put the pre-commit sidecar back (temp file + rename, never torn)."""
    tmp = sidecar.with_name(sidecar.name + ".scouter-restore")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(sidecar)
    except OSError:
        tmp.unlink(missing_ok=True)
        return False
    return True


_HASH_RX = re.compile(r'darktable:history_current_hash="([^"]*)"')


def _restore_if_still_stale(
    sidecar: Path, snapshot: str, stale_hash: str | None, expect_module: str | None
) -> str:
    """Put the pre-commit sidecar back only while the stale rewrite is still
    on disk (row 461 fix round 2, race guard). Returns:

    - "healed": a good write landed meanwhile (darktable autosave, a
      bounce) -- history_end >= the pre-commit one, the file is no longer
      the stale rewrite, and its newest item is `expect_module` (any, when
      none is given). Left alone.
    - "restored": still the stale rewrite (same history hash as
      `stale_hash`; without one, history_end still behind the snapshot).
    - "left": neither; not ours to overwrite. "failed": the write-back failed.

    Mirrors the Lua plugin's `restore_if_still_stale`.
    Enforcer: tests/test_history_expected.py (RestoreRaceGuardTest).
    """
    try:
        current = sidecar.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "left"
    cur_hash_m = _HASH_RX.search(current)
    cur_hash = cur_hash_m.group(1) if cur_hash_m else None
    pre_end, _ = _history_items(snapshot)
    cur_end, cur_items = _history_items(current)
    went_back = _history_end_went_back(snapshot, current) is not None
    still_stale = cur_hash == stale_hash if stale_hash is not None else went_back
    if not still_stale and cur_end is not None and (pre_end is None or cur_end >= pre_end):
        newest = cur_items.get(cur_end - 1)
        op = _OP_RX.search(newest) if newest else None
        if not expect_module or (op is not None and op.group(1) == expect_module):
            return "healed"
    if not still_stale:
        return "left"
    return "restored" if _restore_sidecar(sidecar, snapshot) else "failed"


def _history_advanced_as_expected(
    before: str | None, after: str | None, expect_module: str
) -> tuple[bool, str | None]:
    """Did the sidecar history move the way THIS write expects? `(ok, why)`.

    Row 434 (gauntlet night1-20260926, 15 cases; e.g.
    cov-phrase-faded_blacks-olympus-touch, history_end 112 -> 92 with the
    params of a case three moves earlier, reported ok): no_bounce's rating
    touch rewrote the XMP from a library.db many edits behind the darkroom.
    The history DID change -- backwards -- so "changed" (row 430) accepted
    it. A rewrite is this write's commit only when history_end did not go
    back, the newest active item (num = history_end - 1) is `expect_module`,
    and that item is not identical to what the sidecar held at that num.

    Mirrors the Lua plugin's `history_advanced_as_expected`; this copy is
    the Python side's own look at the sidecar after the bridge answered.
    Enforcer: tests/test_history_expected.py (with the intent examples in
    tests/intent/history_commit_expected.json) and the "row 434" blocks in
    tests/lua/test_dispatcher.lua. The value-level oracle is the write
    verbs' read-back of the param (row 433, fix/htc-applied-verify).
    """
    after_end, after_items = _history_items(after)
    if after_end is None:
        return False, "the rewritten sidecar has no readable history_end"
    before_end, before_items = _history_items(before)
    if before_end is not None and after_end < before_end:
        return False, (
            f"history_end went back from {before_end} to {after_end}: the sidecar was "
            "rewritten from an older library.db state, not from this edit"
        )
    newest = after_items.get(after_end - 1)
    if newest is None:
        return False, f"the rewritten sidecar has no history item {after_end - 1}"
    op = _OP_RX.search(newest)
    if (op.group(1) if op else None) != expect_module:
        return False, (
            f"the newest history item is {op.group(1) if op else None}, not {expect_module}"
        )
    if before_items.get(after_end - 1) == newest:
        return False, f"the newest {expect_module} history item is unchanged"
    return True, None


@metrics.instrumented("history.commit")
def commit(
    image: str | int | None = None,
    *,
    timeout_s: float = 5.0,
    strategy: str = "auto",
    attempts: int = DEFAULT_COMMIT_ATTEMPTS,
    expect_change: bool | None = None,
    job: str | None = None,
) -> CommitResult:
    """The public `history.commit` verb: `commit_expecting` with no write to
    expect (a hand edit, or a batch), so any history change counts.
    See `commit_expecting` for the full contract."""
    return commit_expecting(
        image, timeout_s=timeout_s, strategy=strategy, attempts=attempts,
        expect_change=expect_change, job=job,
    )


def commit_expecting(
    image: str | int | None = None,
    *,
    timeout_s: float = 5.0,
    strategy: str = "auto",
    attempts: int = DEFAULT_COMMIT_ATTEMPTS,
    expect_change: bool | None = None,
    job: str | None = None,
    expect_module: str | None = None,
) -> CommitResult:
    """Make darktable write this image's history to its sidecar, then verify.

    Uninstrumented: write verbs (`darkroom._commit`) call it so one verb call
    still emits exactly one metrics event.

    `expect_module` (row 434): the darktable operation the write being
    committed touched. With it, a sidecar rewrite counts as the commit only
    when its history advanced as that write expects
    (`_history_advanced_as_expected`), both in the plugin (so auto falls
    back to the view bounce past a stale no_bounce rewrite) and in the poll
    below. Without it, any history change counts, as before. A false "not
    as expected" costs a CommitTimeout, which every darkroom writer already
    resolves by reading the value back (`_commit_with_readback`).

    `attempts`/`expect_change` (task 12): a caller who already knows this
    write may be a no-op -- `darkroom.set_param`'s pre-check, when the
    watermark is stale but the requested value already matches the XMP --
    asks for `attempts=1, expect_change=False` and pays one bounce (~1.0s)
    instead of the two-bounce, ~5.5s worst case a real "nothing to write"
    discovery costs.

    Fix round 1, blocker 2: `expect_change=False`'s early return is NEVER
    proof nothing changed -- only proof that ONE bounce found nothing to
    flush WITHIN ITS BUDGET, which is not the same thing if the caller's
    prediction was wrong. So this path never raises `commit_timeout` (an
    honest, expected outcome is not a failure), but it also never stamps a
    watermark on its own: it returns `changed=False, confirmed=False`, and
    it is the CALLER's job (`darkroom.set_param`'s mid path) to
    independently confirm the requested state via readback before trusting
    it -- only that readback earns a watermark.

    Row 431 (bench 20260926t083159z: `history_commit` on an unedited frame,
    `timeout_s=10`, never answered). `expect_change=None` (the default)
    means: expect a change only when Scouter journalled an uncommitted write
    for this image. With none pending, darktable gets one look (on 5.4.x,
    since row 461, a single view bounce; elsewhere one no_bounce look and no
    bounce); if the history did not move, the answer is `noop=True`. Pass `expect_change=True` to force the full bounce (e.g. an
    edit made by hand in darktable seconds ago). And `timeout_s` now bounds
    the WHOLE call: the plugin gets `budget_ms` a little under it, and the
    bridge wait itself is capped at what is left.
    """
    job = job or paths.current_job()
    started = time.monotonic()
    deadline = started + timeout_s
    target = image if image is not None else _darkroom_image()
    # A writer, so an unknown path may be imported (that is `darkroom.open`'s
    # documented job); reads below never pass `may_import`.
    resolved = dtbridge.resolve_image(target, may_import=True)
    probe_only = False
    if expect_change is None:
        pending = pending_uncommitted_keys(job, resolved.image_id)
        expect_change = bool(pending)
        probe_only = not pending
    if not expect_change:
        # Mirrors the Lua handler's own clamp (nit, fix round 1):
        # `expect_change=False` always means exactly one bounce, regardless
        # of what the caller passed. Enforced on both sides so a caller who
        # bypasses the bridge (a test double, or a future strategy that
        # never reaches Lua) still gets the same contract.
        attempts = 1
    sidecar = xmp.sidecar_path(resolved.path)
    before_ns, before_size = _sidecar_signature(sidecar)
    before_history = _history_fingerprint(sidecar) if sidecar.exists() else None
    # Row 461: the pre-commit copy, restored if a no_bounce rewrite is stale
    # and the plugin could not put it back itself.
    try:
        snapshot: str | None = sidecar.read_text(encoding="utf-8") if sidecar.exists() else None
    except OSError:
        snapshot = None
    metadata_only_rewrites = 0
    stale_rewrite: str | None = None

    if strategy not in COMMIT_STRATEGIES:
        raise UsageError(f"unknown strategy {strategy!r} (valid: {', '.join(COMMIT_STRATEGIES)})")
    params = {"strategy": strategy, "attempts": attempts, "expect_change": expect_change}
    if expect_module:
        params["expect_module"] = expect_module
    if strategy in ("no_bounce", "auto"):
        # no_bounce acts on a named image (selection + DB flush + XMP write)
        # rather than whatever the darkroom has loaded, so it is told which.
        # Row 429: "auto" starts with no_bounce (falling back to the
        # view_bounce_action bounce), so it needs the image too.
        params["image_id"] = resolved.image_id
    # Row 431: the plugin stops waiting a little before the caller does, and
    # the bridge wait itself never outlives `timeout_s` (it used to wait the
    # bridge's own 60s default, whatever the caller asked for).
    params["budget_ms"] = max(0, int((deadline - time.monotonic() - _BRIDGE_MARGIN_S) * 1000))
    try:
        result = dtbridge.call(
            "history.commit", params, timeout=max(deadline - time.monotonic(), 0.05)
        )
    except DarktableUnresponsive:
        # Fix round 1 (I1): a wedged darktable is its own answer, not a slow
        # commit -- surface it before the CommitTimeout conversion below.
        raise
    except BridgeTimeout as e:
        if not isinstance(e.__cause__, BridgeTimeoutError):
            raise
        raise CommitTimeout(
            f"darktable did not answer history.commit for {sidecar} within "
            f"{timeout_s:g}s (strategy {strategy})",
            fix="darktable's Lua worker is busy or stuck; check `scouter doctor`, "
            "or leave and re-enter the darkroom, then retry",
            detail={
                "strategy_used": strategy,
                "image_id": resolved.image_id,
                "xmp": str(sidecar),
                "ms": int((time.monotonic() - started) * 1000),
                "bridge_timeout": True,
            },
        ) from e
    strategy_used = str(result.get("strategy_used", strategy))
    ok = bool(result.get("ok", True))
    view_after = str(result.get("view_after", "unknown"))
    sidecar_written = result.get("sidecar_written")

    def _watermark() -> None:
        watermark_write(job, resolved.image_id, resolved.xmp_path)

    if result.get("stale_rewrite"):
        # Row 461: the plugin saw the sidecar rewritten with a history that is
        # not this edit (history_end went back, or not the expected module).
        # Refused on every path -- never a no-op, never a commit, never a
        # watermark. The second look is ours: re-read the sidecar.
        # Round 2: on any strategy (the bounce path too), and race-guarded.
        if result.get("stale_rewrite_restored"):
            outcome = "restored"
        elif result.get("stale_rewrite_healed"):
            outcome = "healed"
        elif snapshot is not None:
            outcome = _restore_if_still_stale(
                sidecar, snapshot, result.get("stale_hash"), expect_module
            )
        else:
            outcome = "left"
        restored = outcome == "restored"
        raise CommitTimeout(
            f"stale_rewrite: darktable rewrote {sidecar} with an older history, not this "
            f"edit ({result.get('detail')}); "
            + {"restored": "the pre-commit sidecar was restored",
               "healed": "a good write landed meanwhile, so the sidecar was left alone"}.get(
                outcome, "the sidecar was NOT restored"),
            fix="leave and re-enter the darkroom (the bounce writes the in-memory history), "
            "then read the history back",
            detail={
                "strategy_used": strategy_used,
                "image_id": resolved.image_id,
                "xmp": str(sidecar),
                "stale_rewrite": result.get("detail"),
                "stale_rewrite_restored": restored,
                "stale_rewrite_healed": outcome == "healed",
                "sidecar_end_now": _history_items(_history_fingerprint(sidecar))[0],
                "ms": int((time.monotonic() - started) * 1000),
            },
        )

    if probe_only and ok and sidecar_written is False:
        return CommitResult(
            image_id=resolved.image_id,
            path=resolved.path,
            strategy_used=strategy_used,
            committed=False,
            changed=False,
            confirmed=False,
            noop=True,
            view_after=view_after,
            ms=int((time.monotonic() - started) * 1000),
            xmp_mtime=sidecar.stat().st_mtime if sidecar.exists() else None,
            warnings=[
                f"nothing to commit for {sidecar}: Scouter has no uncommitted write "
                "for this image, and darktable's flush left the sidecar's history "
                "unchanged. An edit made by hand in darktable that it has not yet "
                "saved to its library is not seen by this check; pass "
                "expect_change=true to force a view bounce."
            ],
        )

    if not expect_change and ok and sidecar_written is False:
        return CommitResult(
            image_id=resolved.image_id,
            path=resolved.path,
            strategy_used=strategy_used,
            committed=False,
            changed=False,
            confirmed=False,
            view_after=view_after,
            ms=int((time.monotonic() - started) * 1000),
            xmp_mtime=sidecar.stat().st_mtime if sidecar.exists() else None,
            warnings=[
                f"darktable's one bounce found nothing to flush for {sidecar}: this is "
                "NOT proof nothing changed, only that this one bounce found nothing "
                "within its budget. No watermark was recorded; confirm the requested "
                "state via an independent readback before trusting it."
            ],
        )

    def _poll() -> CommitResult | None:
        """One look at the sidecar. `None` means: not there yet, keep polling."""
        if not sidecar.exists():
            return None
        mtime_ns, size = _sidecar_signature(sidecar)
        if mtime_ns <= before_ns and size == before_size:
            return None
        text = sidecar.read_text(encoding="utf-8", errors="replace")
        if not xmp.parse(text).well_formed:
            return None
        if before_history is not None and _history_fingerprint(sidecar) == before_history:
            # Touched, not committed (row 430): keep polling -- a real write
            # may still land -- but never report this rewrite as the commit.
            nonlocal metadata_only_rewrites
            metadata_only_rewrites += 1
            return None
        after_history = _history_fingerprint(sidecar)
        why = _history_end_went_back(before_history, after_history)
        as_expected = why is None
        if as_expected and expect_module:
            as_expected, why = _history_advanced_as_expected(
                before_history, after_history, expect_module
            )
        if not as_expected:
            # Rows 434/461: rewritten, but not with this edit -- keep polling
            # (the fallback bounce may still land) and never report it.
            nonlocal stale_rewrite
            stale_rewrite = why
            return None
        warnings: list[str] = []
        if view_after != "darkroom":
            warnings.append(
                f"the bridge reports the view ended at {view_after!r}, not darkroom, "
                "after the commit; a subsequent darkroom action may need a fresh "
                "`darkroom.open`"
            )
        return CommitResult(
            image_id=resolved.image_id,
            path=resolved.path,
            strategy_used=strategy_used,
            committed=True,
            changed=True,
            view_after=view_after,
            ms=int((time.monotonic() - started) * 1000),
            xmp_mtime=sidecar.stat().st_mtime,
            warnings=warnings,
        )

    while time.monotonic() < deadline:
        found = _poll()
        if found is not None:
            _watermark()
            return found
        time.sleep(POLL_INTERVAL_S)

    # One last look: the deadline can pass between the final `sleep` waking up
    # and the loop re-checking `time.monotonic()`, which would otherwise
    # discard a commit that landed in exactly that window.
    found = _poll()
    if found is not None:
        _watermark()
        return found

    raise CommitTimeout(
        f"darktable did not write {sidecar} within {timeout_s:g}s using the "
        f"{strategy_used} strategy",
        detail={
            "strategy_used": strategy_used,
            "image_id": resolved.image_id,
            "xmp": str(sidecar),
            "ok": ok,
            "view_after": view_after,
            "ms": int((time.monotonic() - started) * 1000),
            "bridge_detail": result.get("detail"),
            "sidecar_rewritten_history_unchanged": (
                f"the sidecar was rewritten but its history did not change "
                f"({metadata_only_rewrites} look(s)): the edit never reached it"
                if metadata_only_rewrites
                else None
            ),
            "sidecar_rewritten_not_this_edit": stale_rewrite,
            "stale_rewrite": stale_rewrite,
        },
    )


def _darkroom_image() -> int:
    """The darkroom's loaded image id, via the read-only `darkroom.current`.

    Never `darkroom.open {}` (final review, blocker 1): that method LOADS an
    image and errors outright when given nothing to load, so a no-argument
    `history.commit` was inoperable against the real plugin -- it could only
    ever raise. `darkroom.current` is the bridge's own read-only report of
    what is loaded, and is what `core.darkroom` already uses.
    """
    result = dtbridge.call("darkroom.current", {})
    image_id = result.get("image_id")
    if image_id is None:
        raise NotInDarkroom(
            "no image is loaded in the darkroom, so there is nothing to commit "
            f"(view is {result.get('view', 'unknown')}); pass an image explicitly",
            detail={"view": result.get("view")},
        )
    return int(image_id)


def pending_uncommitted_keys(job: str, image_id: int) -> list[str]:
    """`"<module>.<param>"` (plus `[instance]` when non-zero) for every key
    whose NEWEST landed journal entry for this image was written with
    `commit=False` -- i.e. every write darktable has applied in memory but
    has not been asked to flush to the sidecar.

    Carried finding 7 (task 12). The per-key decision is delegated to
    `darkroom._last_journal_write`, which already knows the rules that
    matter (an undone or failed write never landed; a landed `module.reset`
    invalidates everything journalled for that module before it) -- copying
    them here would be a second, drifting implementation of the same
    question.
    """
    from scouter.core import darkroom, journal

    keys: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str, int]] = set()
    for entry in journal.read_all(job):
        if entry.image_id != image_id or entry.module is None or entry.param is None:
            continue
        key = (entry.module, entry.param, entry.instance)
        if key not in seen:
            seen.add(key)
            keys.append(key)

    pending: list[str] = []
    for module, param, instance in keys:
        last = darkroom._last_journal_write(job, image_id, module, param, instance)
        if last is not None and last.commit is False:
            pending.append(f"{module}.{param}" + (f"[{instance}]" if instance else ""))
    return pending


@metrics.instrumented("history.changed")
def changed(image: str | int, *, job: str | None = None) -> ChangedResult:
    """Is this image's sidecar the one Scouter last committed?

    A pure read: `image.lookup` plus one `stat`. It moves nothing and costs no
    bounce, which is the whole point -- a script calls this before deciding to
    spend one.
    """
    job = job or paths.current_job()
    resolved = dtbridge.resolve_image(image)
    fresh, reason = watermark_fresh(job, resolved.image_id, resolved.xmp_path)
    state = paths.job_state_read(job)
    watermark = (state.get("watermarks") or {}).get(str(resolved.image_id))
    sidecar = _signature(resolved.xmp_path)

    # Carried finding 7 (task 12): fold `pending_uncommitted` in. The
    # watermark is a pure FILE signature, so a Scouter write journalled with
    # `commit=False` leaves the sidecar byte-identical and this verb
    # answered `fresh=True` -- while darktable's live history had already
    # diverged from disk in a way the JOURNAL knows about. `darkroom.set`
    # has always consulted the journal for exactly this (fix round 1,
    # blocker 1); a caller asking "is the sidecar what Scouter last
    # committed?" before deciding whether to spend a bounce needs the same
    # answer, or the cheap read sends it down the expensive-path-avoiding
    # branch precisely when the expensive path is the correct one.
    pending = pending_uncommitted_keys(job, resolved.image_id)
    if pending:
        fresh = False
        pending_reason = (
            f"Scouter has {len(pending)} uncommitted write(s) for this image "
            f"({', '.join(pending)}): darktable's live history differs from the "
            "sidecar, which a file signature cannot see. Run `history commit` first"
        )
        reason = f"{reason}; {pending_reason}" if reason else pending_reason
    return ChangedResult(
        image_id=resolved.image_id,
        path=resolved.path,
        fresh=fresh,
        watermark=watermark,
        sidecar=sidecar,
        pending_uncommitted=pending,
        reason=reason,
    )


for verb in (
    Verb(
        id="history.read",
        group="history",
        fn=read,
        args=(
            ArgSpec("image", "image", required=True, positional=True, help="path or image id"),
            ArgSpec(
                "source",
                "str",
                choices=READ_SOURCES,
                help="xmp (sidecar; an explicit xmp with no sidecar file is the error "
                "no_sidecar; unset is lenient: ok, empty), db (darktable's library.db, read-only), or auto "
                "(xmp unless it is stale -- missing, mid-write, or older than library.db's "
                "last history change -- then db)",
            ),
            ArgSpec(
                "library",
                "str",
                help="library.db path for source=db/auto; default: the running darktable's "
                "--library, then the usual locations",
            ),
        ),
        writes=False,
        gated=False,
        summary="Read the image's committed history.",
    ),
    Verb(
        id="history.commit",
        group="history",
        fn=commit,
        args=(
            ArgSpec(
                "image",
                "image",
                positional=True,
                help="path or image id; defaults to the darkroom image",
            ),
            ArgSpec("timeout_s", "float", default=5.0, help="seconds to wait"),
            ArgSpec(
                "strategy",
                "str",
                default="auto",
                choices=COMMIT_STRATEGIES,
                help="commit trigger; auto is no_bounce (flushes history without leaving "
                "the view), falling back to view_bounce_action (a bounce through "
                "darktable's own switch-views commands) when the sidecar did not change",
            ),
            ArgSpec(
                "attempts",
                "int",
                default=DEFAULT_COMMIT_ATTEMPTS,
                help="view-bounce attempts, clamped to 1..3; a caller sure this is a "
                "no-op should pass 1",
            ),
            ArgSpec(
                "expect_change",
                "bool",
                default=None,
                help="default: expect a change only when Scouter has an uncommitted "
                "write for this image, else answer noop=true promptly; True forces the "
                "full bounce; False forces attempts=1 and reports a confirmed no-op as "
                "ok/changed=false instead of raising commit_timeout",
            ),
        ),
        writes=True,
        gated=False,
        summary="Make darktable write history to the XMP, and verify it.",
    ),
    Verb(
        id="history.changed",
        group="history",
        fn=changed,
        args=(ArgSpec("image", "image", required=True, positional=True, help="path or image id"),),
        writes=False,
        gated=False,
        summary="Whether the image's sidecar is the one Scouter last committed (no bounce).",
    ),
):
    register(verb)
