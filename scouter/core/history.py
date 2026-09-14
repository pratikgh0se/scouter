"""History: read it from the XMP, and make darktable write the XMP.

Phase 0 finding 3: darktable commits history and the sidecar when the darkroom
is LEFT, not per slider move. So `commit` is a first-class verb and every
writer calls it by default. The strategy order is set by task 1's measurement
(docs/measurements.md); `view_bounce` is the proven floor, so commit cannot
fail for lack of a mechanism -- only for lack of time.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from scouter import metrics, paths
from scouter.core import dtbridge, xmp
from scouter.errors import CommitTimeout, NotInDarkroom
from scouter.registry import ArgSpec, Verb, register
from scouter.types import ChangedResult, CommitResult, HistoryResult

POLL_INTERVAL_S = 0.05

#: Mirrors darktable_mcp.lua's COMMIT_BOUNCE_ATTEMPTS -- the default number of
#: view-bounce attempts a commit spends when the caller does not say
#: otherwise. A caller who already knows the write may be a no-op (the
#: `darkroom.set_param` pre-check, task 12) asks for `attempts=1` and
#: `expect_change=False` instead, and pays one bounce, not two.
DEFAULT_COMMIT_ATTEMPTS = 2


@metrics.instrumented("history.read")
def read(image: str | int, *, job: str | None = None) -> HistoryResult:
    """Parse the image's sidecar. `confirmed=False` when it is mid-write.

    `image` is required (controller ruling 10): unlike `commit`, there is no
    darkroom fallback here, because a caller reading history almost always
    means a specific image, and `darkroom.describe_history` already resolves
    the darkroom's current image itself before delegating to this function.
    Every entry keeps its own `enabled` flag from the XMP (ruling 20), so a
    disabled module in the history is never mistaken for a live one.
    """
    resolved = dtbridge.resolve_image(image)
    try:
        text, mtime = xmp.read_sidecar(resolved.path)
    except FileNotFoundError:
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


def _job_state_read(job: str) -> dict:
    """Read the per-job state file (ruling 30's file, shared with darkroom.py's
    `opened_image_id`/`opened_path`). A private duplicate of darkroom.py's
    helper of the same name and shape: history.py must not import darkroom.py
    at module scope (darkroom.py already imports history.py lazily, and this
    keeps the dependency one-directional), and this is the whole logic --
    "read a small JSON dict, or {} when there is none yet."""
    path = paths.job_state_path(job)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _job_state_write(job: str, state: dict) -> None:
    """Replace this job's state file atomically. Best-effort by design, same
    as darkroom.py's identical helper."""
    path = paths.ensure(paths.job_state_path(job))
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, sort_keys=True)
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _signature(xmp_path: str) -> dict | None:
    p = Path(xmp_path)
    if not p.exists():
        return None
    st = p.stat()
    return {"mtime_ns": st.st_mtime_ns, "size": st.st_size}


def watermark_write(job: str, image_id: int, xmp_path: str) -> dict:
    """Record the sidecar's signature after a CONFIRMED commit.

    This is what makes the no-op pre-check safe. The XMP can be stale: an
    uncommitted darkroom change means the file on disk is older than
    darktable's in-memory history, and skipping a write on a stale read would
    silently drop a real edit. The watermark says "the last time Scouter
    committed this image, the sidecar looked exactly like this" -- so a
    sidecar that still looks like that is known-fresh, and anything else is
    not trusted.

    A read-modify-write on the shared per-job state file (not a blind
    overwrite): `_record_opened_image` in darkroom.py writes the same file,
    and each write must preserve the other's keys.
    """
    sig = _signature(xmp_path)
    state = _job_state_read(job)
    watermarks = dict(state.get("watermarks") or {})
    key = str(int(image_id))
    if sig is None:
        watermarks.pop(key, None)
    else:
        watermarks[key] = sig
    state["watermarks"] = watermarks
    _job_state_write(job, state)
    return sig or {}


def watermark_fresh(job: str, image_id: int, xmp_path: str) -> tuple[bool, str | None]:
    """(fresh, reason_when_not). No watermark -> (False, "no watermark for this
    image in job <job>"): a fresh session, or an edit Pratik made by hand, both
    correctly fall through to the safe path."""
    state = _job_state_read(job)
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


@metrics.instrumented("history.commit")
def commit(
    image: str | int | None = None,
    *,
    timeout_s: float = 5.0,
    strategy: str = "auto",
    attempts: int = DEFAULT_COMMIT_ATTEMPTS,
    expect_change: bool = True,
    job: str | None = None,
) -> CommitResult:
    """Make darktable write this image's history to its sidecar, then verify.

    `attempts`/`expect_change` (task 12): a caller who already knows this
    write may be a no-op -- `darkroom.set_param`'s pre-check, when the
    watermark is stale but the requested value already matches the XMP --
    asks for `attempts=1, expect_change=False` and pays one bounce (~1.0s)
    instead of the two-bounce, ~5.5s worst case a real "nothing to write"
    discovery costs. With `expect_change=False`, a bounce that confirms
    nothing changed is reported as an honest `changed=False` success, never
    raised as `commit_timeout`.
    """
    job = job or paths.current_job()
    target = image if image is not None else _darkroom_image()
    # A writer, so an unknown path may be imported (that is `darkroom.open`'s
    # documented job); reads below never pass `may_import`.
    resolved = dtbridge.resolve_image(target, may_import=True)
    sidecar = xmp.sidecar_path(resolved.path)
    before_ns, before_size = _sidecar_signature(sidecar)

    started = time.monotonic()
    result = dtbridge.call(
        "history.commit",
        {"strategy": strategy, "attempts": attempts, "expect_change": expect_change},
    )
    strategy_used = str(result.get("strategy_used", strategy))
    ok = bool(result.get("ok", True))
    view_after = str(result.get("view_after", "unknown"))
    sidecar_written = result.get("sidecar_written")

    def _watermark() -> None:
        watermark_write(job, resolved.image_id, resolved.xmp_path)

    # `expect_change=False`: the bridge already spent its one bounce and its
    # own sidecar wait (Lua's `wait_for_sidecar`, up to SIDECAR_WRITE_BUDGET_MS)
    # confirming nothing changed. Polling again here for the rest of
    # `timeout_s` would just re-pay that wait for no new information -- the
    # bridge's own `sidecar_written=False` is already the answer.
    if not expect_change and ok and sidecar_written is False:
        _watermark()
        return CommitResult(
            image_id=resolved.image_id,
            path=resolved.path,
            strategy_used=strategy_used,
            committed=True,
            changed=False,
            view_after=view_after,
            ms=int((time.monotonic() - started) * 1000),
            xmp_mtime=sidecar.stat().st_mtime if sidecar.exists() else None,
            warnings=[
                f"darktable confirmed no history change for {sidecar}: the caller "
                "said a change was not expected, and none was found"
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

    deadline = started + timeout_s
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
    state = _job_state_read(job)
    watermark = (state.get("watermarks") or {}).get(str(resolved.image_id))
    sidecar = _signature(resolved.xmp_path)
    return ChangedResult(
        image_id=resolved.image_id,
        path=resolved.path,
        fresh=fresh,
        watermark=watermark,
        sidecar=sidecar,
        reason=reason,
    )


for verb in (
    Verb(
        id="history.read",
        group="history",
        fn=read,
        args=(ArgSpec("image", "image", required=True, positional=True, help="path or image id"),),
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
                choices=("auto", "sidecar_action", "view_bounce"),
                help="commit trigger; auto picks the cheapest that works",
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
                default=True,
                help="False forces attempts=1 and reports a confirmed no-op as "
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
