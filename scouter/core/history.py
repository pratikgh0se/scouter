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
from scouter.errors import CommitTimeout
from scouter.paths import current_job
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
            image_id=resolved.image_id, path=resolved.path, source="xmp",
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
        image_id=resolved.image_id, path=resolved.path,
        history_end=parsed.history_end, xmp_version=parsed.xmp_version,
        iop_order_version=parsed.iop_order_version, current_hash=parsed.current_hash,
        xmp_mtime=mtime, entries=parsed.entries, source="xmp",
        confirmed=parsed.well_formed, warnings=warnings,
    )


@metrics.instrumented("history.commit")
def commit(image: str | int | None = None, *, timeout_s: float = 5.0,
           strategy: str = "auto", job: str | None = None) -> CommitResult:
    """Make darktable write this image's history to its sidecar, then verify."""
    job = job or current_job()
    target = image if image is not None else _darkroom_image()
    resolved = dtbridge.resolve_image(target)
    sidecar = xmp.sidecar_path(resolved.path)
    before_ns = sidecar.stat().st_mtime_ns if sidecar.exists() else 0

    started = time.monotonic()
    result = dtbridge.call("history.commit", {"strategy": strategy})
    strategy_used = str(result.get("strategy_used", strategy))

    deadline = started + timeout_s
    while time.monotonic() < deadline:
        if sidecar.exists() and sidecar.stat().st_mtime_ns > before_ns:
            text = sidecar.read_text(encoding="utf-8", errors="replace")
            if xmp.parse(text).well_formed:
                return CommitResult(
                    image_id=resolved.image_id, path=resolved.path,
                    strategy_used=strategy_used, committed=True,
                    ms=int((time.monotonic() - started) * 1000),
                    xmp_mtime=sidecar.stat().st_mtime,
                )
        time.sleep(POLL_INTERVAL_S)

    raise CommitTimeout(
        f"darktable did not write {sidecar} within {timeout_s:g}s using the "
        f"{strategy_used} strategy",
        detail={"strategy_used": strategy_used, "image_id": resolved.image_id,
                "xmp": str(sidecar),
                "ms": int((time.monotonic() - started) * 1000),
                "bridge_detail": result.get("detail")},
    )


def _darkroom_image() -> int:
    return int(dtbridge.call("darkroom.open", {})["image_id"])


for verb in (
    Verb(id="history.read", group="history", fn=read,
         args=(ArgSpec("image", "image", required=True, positional=True,
                       help="path or image id"),),
         writes=False, gated=False, summary="Read the image's committed history."),
    Verb(id="history.commit", group="history", fn=commit,
         args=(ArgSpec("image", "image", positional=True,
                       help="path or image id; defaults to the darkroom image"),
               ArgSpec("timeout_s", "float", default=5.0, help="seconds to wait"),
               ArgSpec("strategy", "str", default="auto",
                       choices=("auto", "sidecar_action", "view_bounce"),
                       help="commit trigger; auto picks the cheapest that works")),
         writes=True, gated=False,
         summary="Make darktable write history to the XMP, and verify it."),
):
    register(verb)
