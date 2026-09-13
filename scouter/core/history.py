"""History: read it from the XMP, and make darktable write the XMP.

Phase 0 finding 3: darktable commits history and the sidecar when the darkroom
is LEFT, not per slider move. So `commit` is a first-class verb and every
writer calls it by default. The strategy order is set by task 1's measurement
(docs/measurements.md); `view_bounce` is the proven floor, so commit cannot
fail for lack of a mechanism -- only for lack of time.
"""

from __future__ import annotations

import time

from scouter import metrics
from scouter.core import dtbridge, xmp
from scouter.errors import CommitTimeout, NotInDarkroom
from scouter.registry import ArgSpec, Verb, register
from scouter.types import CommitResult, HistoryResult

POLL_INTERVAL_S = 0.05


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
    job: str | None = None,
) -> CommitResult:
    """Make darktable write this image's history to its sidecar, then verify."""
    target = image if image is not None else _darkroom_image()
    # A writer, so an unknown path may be imported (that is `darkroom.open`'s
    # documented job); reads below never pass `may_import`.
    resolved = dtbridge.resolve_image(target, may_import=True)
    sidecar = xmp.sidecar_path(resolved.path)
    before_ns, before_size = _sidecar_signature(sidecar)

    started = time.monotonic()
    result = dtbridge.call("history.commit", {"strategy": strategy})
    strategy_used = str(result.get("strategy_used", strategy))
    ok = bool(result.get("ok", True))
    view_after = str(result.get("view_after", "unknown"))

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
            view_after=view_after,
            ms=int((time.monotonic() - started) * 1000),
            xmp_mtime=sidecar.stat().st_mtime,
            warnings=warnings,
        )

    deadline = started + timeout_s
    while time.monotonic() < deadline:
        found = _poll()
        if found is not None:
            return found
        time.sleep(POLL_INTERVAL_S)

    # One last look: the deadline can pass between the final `sleep` waking up
    # and the loop re-checking `time.monotonic()`, which would otherwise
    # discard a commit that landed in exactly that window.
    found = _poll()
    if found is not None:
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
        ),
        writes=True,
        gated=False,
        summary="Make darktable write history to the XMP, and verify it.",
    ),
):
    register(verb)
