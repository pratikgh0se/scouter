"""Scouter: the feedback loop. Render, measure, report. No model calls."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from dataclasses import asdict
from pathlib import Path

from darktable_mcp.darktable.cli_wrapper import CLIWrapper
from darktable_mcp.utils.errors import DarktableNotFoundError, ExportError

from scouter import metrics, paths
from scouter.core import dtbridge, ppm, xmp
from scouter.errors import CliWroteNothing, DbLocked
from scouter.registry import ArgSpec, Verb, register
from scouter.types import CompareResult, PreviewResult, StatsResult

#: Size Scouter drops to when the loop blows its budget (spec 7).
FALLBACK_PX = 512

#: darktable-cli's own stderr wording for a library lock contention (ruling 7:
#: rendering goes through `CLIWrapper`, which raises a generic `ExportError`
#: for any non-zero exit -- this is what separates a lock from any other
#: failure, the same text `core.dtbridge` matches for the Lua bridge's own
#: "database is locked" errors).
_DB_LOCKED_RE = re.compile(r"database is locked|could not get lock|another instance", re.I)


def cli_binary() -> str:
    explicit = os.environ.get("SCOUTER_DARKTABLE_CLI")
    if explicit:
        return explicit
    mac = "/Applications/darktable.app/Contents/MacOS/darktable-cli"
    if Path(mac).is_file():
        return mac
    found = shutil.which("darktable-cli")
    if found:
        return found
    raise CliWroteNothing(
        "darktable-cli not found: looked at $SCOUTER_DARKTABLE_CLI, "
        f"{mac}, and $PATH",
        fix="install darktable, or set SCOUTER_DARKTABLE_CLI to its darktable-cli",
    )


def render(raw_path: str, out_path: Path, px: int, ext: str) -> int:
    """One darktable-cli render via `CLIWrapper`. Returns elapsed ms.

    Ruling 7: no hand-rolled subprocess -- `CLIWrapper.export_image` already
    owns the isolated `--configdir` (so darktable-cli never contends for the
    GUI's library.db lock) and the exit-code-lies-about-success check task 4
    proved. `ExportError` is darktable-cli-shaped, not one of Scouter's typed
    errors, so its message is classified here into `db_locked` or
    `cli_wrote_nothing` the same way `core.dtbridge` classifies the Lua
    bridge's own failures.
    """
    configdir = paths.cli_configdir()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    cli = CLIWrapper(darktable_cli_path=cli_binary(), configdir=configdir)
    started = time.monotonic()
    try:
        cli.export_image(
            Path(raw_path), out_path, format_type=ext,
            max_width=px, max_height=px, timeout=180,
        )
    except DarktableNotFoundError as e:
        raise CliWroteNothing(
            str(e),
            fix="install darktable, or set SCOUTER_DARKTABLE_CLI to its darktable-cli",
        ) from e
    except ExportError as e:
        message = str(e)
        if _DB_LOCKED_RE.search(message):
            raise DbLocked(
                f"darktable-cli could not get a library lock: {message[:200]}",
                detail={"configdir": str(configdir), "raw_path": raw_path},
            ) from e
        raise CliWroteNothing(
            f"darktable-cli wrote no usable file at {out_path}: {message[:500]}",
            detail={"configdir": str(configdir), "raw_path": raw_path},
        ) from e
    return int((time.monotonic() - started) * 1000)


def _cache_key(raw_path: str, px: int, ext: str) -> str:
    sidecar = xmp.sidecar_path(raw_path)
    mtime = sidecar.stat().st_mtime_ns if sidecar.exists() else 0
    digest = hashlib.sha1(f"{raw_path}|{mtime}|{px}|{ext}".encode()).hexdigest()[:16]
    return f"{Path(raw_path).stem}-{digest}.{ext}"


def _raster_for(raw_path: str, px: int) -> tuple[ppm.Raster, int]:
    out = paths.preview_dir() / _cache_key(raw_path, px, "ppm")
    render_ms = render(raw_path, out, px, "ppm")
    raster = ppm.parse_p6(out.read_bytes())
    out.unlink(missing_ok=True)          # stats do not need the pixels kept
    return raster, render_ms


@metrics.instrumented("scout.stats")
def stats(image: str | int, *, px: int = 1024, budget_s: float = 3.0,
          job: str | None = None) -> StatsResult:
    """Histogram, clipping, luminance percentiles, WB ratios and sharpness."""
    resolved = dtbridge.resolve_image(image)
    warnings: list[str] = []
    started = time.monotonic()
    raster, render_ms = _raster_for(resolved.path, px)
    px_used = px
    if (time.monotonic() - started) > budget_s and px > FALLBACK_PX:
        warnings.append(
            f"loop took {time.monotonic() - started:.2f}s at {px}px, over the "
            f"{budget_s:g}s budget: re-rendered at {FALLBACK_PX}px (spec 7)"
        )
        raster, render_ms = _raster_for(resolved.path, FALLBACK_PX)
        px_used = FALLBACK_PX

    analyse_started = time.monotonic()
    hist = ppm.histogram(raster)
    image_stats = ppm.stats(raster)
    analyse_ms = int((time.monotonic() - analyse_started) * 1000)
    warnings.append(
        "statistics are 8-bit (high byte of darktable's 16-bit render) and the "
        "luminance histogram is an estimate from the channel histograms (design 6.2)"
    )
    return StatsResult(
        image_id=resolved.image_id, path=resolved.path, px_requested=px,
        px_used=px_used, render_ms=render_ms, analyse_ms=analyse_ms,
        stats=asdict(image_stats), histogram=hist, warnings=warnings,
    )


@metrics.instrumented("scout.preview")
def preview(image: str | int, *, px: int = 1024, fmt: str = "png",
            job: str | None = None) -> PreviewResult:
    """Render a viewable preview and return its PATH, never its bytes."""
    if fmt not in ("png", "ppm"):
        raise ValueError(f"fmt must be png or ppm, got {fmt!r}")
    resolved = dtbridge.resolve_image(image)
    out = paths.preview_dir() / _cache_key(resolved.path, px, fmt)
    if out.exists() and out.stat().st_size > 0:
        return PreviewResult(
            image_id=resolved.image_id, path=resolved.path, preview_path=str(out),
            fmt=fmt, bytes=out.stat().st_size, px_requested=px, cached=True,
            warnings=["preview served from cache (keyed on the sidecar mtime)"],
        )
    render_ms = render(resolved.path, out, px, fmt)
    width = height = 0
    if fmt == "ppm":
        raster = ppm.parse_p6(out.read_bytes())
        width, height = raster.width, raster.height
    return PreviewResult(
        image_id=resolved.image_id, path=resolved.path, preview_path=str(out),
        fmt=fmt, width=width, height=height, bytes=out.stat().st_size,
        px_requested=px, render_ms=render_ms, cached=False,
        warnings=["read the file at `preview_path`; Scouter never inlines pixels "
                  "(design 6.3)"],
    )


@metrics.instrumented("scout.compare")
def compare(a: str | int, b: str | int, *, px: int = 1024,
            job: str | None = None) -> CompareResult:
    """Numeric distance between two images' current renders."""
    left = stats.__wrapped__(a, px=px, job=job)
    right = stats.__wrapped__(b, px=px, job=job)
    ls, rs = left.stats, right.stats
    delta = {
        "luminance_mean": round(rs["luminance_mean"] - ls["luminance_mean"], 3),
        "luminance_p50": rs["luminance_p50"] - ls["luminance_p50"],
        "clipped_high_pct": round(rs["clipped_high_pct"] - ls["clipped_high_pct"], 4),
        "clipped_low_pct": round(rs["clipped_low_pct"] - ls["clipped_low_pct"], 4),
        "wb_ratio_rg": round(rs["wb_ratio_rg"] - ls["wb_ratio_rg"], 4),
        "wb_ratio_bg": round(rs["wb_ratio_bg"] - ls["wb_ratio_bg"], 4),
        "sharpness": round(rs["sharpness"] - ls["sharpness"], 6),
    }
    return CompareResult(
        a={"image_id": left.image_id, "path": left.path, "stats": ls},
        b={"image_id": right.image_id, "path": right.path, "stats": rs},
        delta=delta, image_id=left.image_id, path=left.path,
        warnings=["deltas are b minus a; sharpness is comparable only between "
                  "renders of the same frame (design 6.2)"],
    )


PX_ARG = ArgSpec("px", "int", default=1024, help="longest edge of the render, 64..4096")

for verb in (
    Verb(id="scout.stats", group="scout", fn=stats,
         args=(ArgSpec("image", "image", required=True, positional=True,
                       help="path or image id"), PX_ARG,
               ArgSpec("budget_s", "float", default=3.0,
                       help="loop budget; over it, re-render at 512px")),
         writes=False, gated=False,
         summary="Histogram, clipping, luminance percentiles, WB ratios, sharpness."),
    Verb(id="scout.preview", group="scout", fn=preview,
         args=(ArgSpec("image", "image", required=True, positional=True,
                       help="path or image id"), PX_ARG,
               ArgSpec("fmt", "str", default="png", choices=("png", "ppm"),
                       help="preview file format")),
         writes=False, gated=False,
         summary="Render a preview to a file and return its path."),
    Verb(id="scout.compare", group="scout", fn=compare,
         args=(ArgSpec("a", "image", required=True, positional=True, help="first image"),
               ArgSpec("b", "image", required=True, positional=True, help="second image"),
               PX_ARG),
         writes=False, gated=False, summary="Numeric distance between two renders."),
):
    register(verb)
