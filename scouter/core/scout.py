"""Scouter: the feedback loop. Render, measure, report. No model calls."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

from darktable_mcp.darktable.cli_wrapper import CLIWrapper
from darktable_mcp.utils.errors import DarktableNotFoundError, ExportError
from scouter import metrics, paths
from scouter.core import dtbridge, ppm, xmp
from scouter.errors import CliWroteNothing, DbLocked, ScouterError
from scouter.registry import ArgSpec, Verb, register
from scouter.types import CompareResult, PreviewResult, StatsResult

#: Size Scouter drops to when the loop blows its budget (spec 7).
FALLBACK_PX = 512

#: `--width`/`--height` bounds Scouter will hand darktable-cli. Anything
#: smaller is not a useful preview; anything larger is well past what the
#: loop budget (spec 7) can afford.
PX_MIN, PX_MAX = 64, 4096

#: How long a persistent preview stays cached before `_prune_preview_cache`
#: age-culls it, and the total budget the cache is trimmed to afterwards
#: (oldest first). Fix round 1, point 6.
PREVIEW_MAX_AGE_S = 7 * 24 * 3600
PREVIEW_MAX_BYTES = 500 * 1024 * 1024

#: Total budget `stats_many` trims `paths.stats_dir()` to (fix round: task 5).
#: Age itself comes from the config (`stats.cache_max_age_days`), not a
#: constant, because it is exactly the knob task 5's cache is meant to expose.
STATS_CACHE_MAX_BYTES = 200 * 1024 * 1024

#: darktable-cli's own stderr wording for a library lock contention (ruling 7:
#: rendering goes through `CLIWrapper`, which raises a generic `ExportError`
#: for any non-zero exit -- this is what separates a lock from any other
#: failure, the same text `core.dtbridge` matches for the Lua bridge's own
#: "database is locked" errors).
_DB_LOCKED_RE = re.compile(r"database is locked|could not get lock|another instance", re.I)

#: Overridable in tests so `cli_binary`'s "not found" path can be exercised
#: without touching whatever darktable install is actually on this host.
_MAC_CLI_PATH = Path("/Applications/darktable.app/Contents/MacOS/darktable-cli")


class CliMissing(ScouterError):
    """darktable-cli could not be found anywhere Scouter looks.

    Deliberately not added to `scouter.errors.ERROR_CLASSES`/`EXIT_CODES`
    (fix round 1, point 7): a missing binary is an environment problem, not
    one of the per-class outcomes those tables enumerate, so its CLI exit
    code falls back to the generic 1 rather than claiming a reserved one.
    """

    code = "cli_missing"
    fix = "install darktable, or set SCOUTER_DARKTABLE_CLI to its darktable-cli"


def cli_binary() -> str:
    explicit = os.environ.get("SCOUTER_DARKTABLE_CLI")
    if explicit:
        return explicit
    if _MAC_CLI_PATH.is_file():
        return str(_MAC_CLI_PATH)
    found = shutil.which("darktable-cli")
    if found:
        return found
    raise CliMissing(
        "darktable-cli not found: looked at $SCOUTER_DARKTABLE_CLI, " f"{_MAC_CLI_PATH}, and $PATH"
    )


def _validate_px(px: int) -> None:
    if not (PX_MIN <= px <= PX_MAX):
        raise ValueError(f"px must be {PX_MIN}..{PX_MAX}, got {px}")


def _render_configdir() -> Path:
    """A private `--configdir` for exactly one render call.

    `CLIWrapper`'s own worker-slot pool (fix round 1, point 2) isolates
    concurrent darktable-cli runs *by thread*, reusing a slot once its owner
    exits -- fine for `batch_export`'s thread pool, but Scouter's own render
    calls are not always issued from a pool CLIWrapper manages, and two
    single-shot renders on the same thread (one after another, or from two
    separate `CLIWrapper` instances) would otherwise share `self.configdir`
    and its `library.db`. A fresh, uniquely-named subdirectory per call sidesteps
    that regardless of threading: `render` builds a new `CLIWrapper` and hands
    it this directory, then removes it once the call returns.
    """
    token = f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}"
    return paths.cli_configdir() / f"call-{token}"


def render(
    raw_path: str, out_path: Path, px: int, ext: str, *, extra_args: list[str] | None = None
) -> int:
    """One darktable-cli render via `CLIWrapper`. Returns elapsed ms.

    `extra_args` (task 10) is threaded straight through to
    `CLIWrapper.export_image` -- Scouter's own preset flags (`--icc-type`,
    `--style`, `--style-overwrite`, format `--conf`), built once in
    `core/export.py::preset_args`. `render` itself stays preset-ignorant.

    Ruling 7: no hand-rolled subprocess -- `CLIWrapper.export_image` already
    owns the exit-code-lies-about-success check task 4 proved. `ExportError`
    is darktable-cli-shaped, not one of Scouter's typed errors, so its
    message is classified here into `db_locked` or `cli_wrote_nothing` the
    same way `core.dtbridge` classifies the Lua bridge's own failures.

    The render goes to a hidden sibling temp file and is moved into place with
    `os.replace` (final review, should-fix 9). This used to unlink `out_path`
    first, which destroyed a perfectly good previous render before knowing
    whether the new one would even be produced -- a failed render left the
    caller with neither file. `os.replace` within one directory is atomic, so
    `out_path` is always either the old render or the complete new one.

    The temp name keeps `out_path`'s own suffix on purpose:
    `CLIWrapper.export_image` normalises the extension to the one the format
    really writes and returns the path it actually wrote, so a `.part` suffix
    would come back as a different path. That returned path is what is moved.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}"
    tmp_out = out_path.with_name(f".{out_path.stem}.{token}.part{out_path.suffix}")
    written = tmp_out
    published = False
    configdir = _render_configdir()
    configdir.mkdir(parents=True, exist_ok=True)
    cli = CLIWrapper(darktable_cli_path=cli_binary(), configdir=configdir)
    started = time.monotonic()
    try:
        written = cli.export_image(
            Path(raw_path),
            tmp_out,
            format_type=ext,
            max_width=px,
            max_height=px,
            timeout=180,
            extra_args=extra_args,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if not written.exists() or written.stat().st_size == 0:
            raise CliWroteNothing(
                f"darktable-cli reported success but wrote no usable file for {out_path}",
                detail={
                    "configdir": str(configdir),
                    "raw_path": raw_path,
                    "temp_path": str(written),
                },
            )
        os.replace(written, out_path)
        published = True
        return elapsed_ms
    except DarktableNotFoundError as e:
        raise CliMissing(str(e)) from e
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
    finally:
        shutil.rmtree(configdir, ignore_errors=True)
        # Anything left of a render that never got published is scratch, and
        # `out_path` itself is never touched here -- only the temp names.
        if not published:
            tmp_out.unlink(missing_ok=True)
            if written != tmp_out:
                written.unlink(missing_ok=True)


def _stats_scratch_path(px: int, ext: str) -> Path:
    """A private output path for one `scout.stats` render.

    Unlike `scout.preview`'s cache, this file is read once and deleted before
    the caller ever sees it (`_raster_for`) -- it needs no stable name at all,
    only one that cannot collide with a concurrent `scout.stats` call on the
    same image, so it gets the same per-call token `_render_configdir` uses.
    """
    token = f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}"
    return paths.preview_dir() / f"stats-{px}-{token}.{ext}"


def _raster_for(raw_path: str, px: int) -> tuple[ppm.Raster, int]:
    out = _stats_scratch_path(px, "ppm")
    render_ms = render(raw_path, out, px, "ppm")
    raster = ppm.parse_p6(out.read_bytes())
    out.unlink(missing_ok=True)  # stats do not need the pixels kept
    return raster, render_ms


@metrics.instrumented("scout.stats")
def stats(
    image: str | int, *, px: int = 1024, budget_s: float = 3.0, job: str | None = None
) -> StatsResult:
    """Histogram, clipping, luminance percentiles, WB ratios and sharpness."""
    _validate_px(px)
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
        image_id=resolved.image_id,
        path=resolved.path,
        px_requested=px,
        px_used=px_used,
        render_ms=render_ms,
        analyse_ms=analyse_ms,
        stats=asdict(image_stats),
        histogram=hist,
        warnings=warnings,
    )


def _stats_cache_path(raw_path: str, px: int) -> Path:
    """Key: absolute path + the XMP's mtime_ns + px + Scouter's version.

    The XMP mtime is in the key on purpose: after `style.apply_set` the frames
    whose history changed get fresh numbers and the rest are read from cache,
    which is what makes "stats before the style, stats after the style"
    affordable over 300 frames.
    """
    # Local import (see `stats_many`): `scouter/__init__.py` is cheap, but
    # importing it at module scope here is the same cycle risk, and keeping
    # both local imports in this module consistent is worth more than one
    # saved line.
    from scouter import __version__

    mtime = xmp.sidecar_path(raw_path)
    stamp = mtime.stat().st_mtime_ns if mtime.exists() else 0
    digest_input = f"{Path(raw_path).resolve()}|{stamp}|{px}|{__version__}"
    key = hashlib.sha1(digest_input.encode()).hexdigest()
    return paths.stats_dir() / f"{key}.json"


def _stats_many_one(path: str, px: int) -> dict:
    """One frame's stats entry for `stats_many`: cache hit, cache miss, or error.

    Ruling 23: cache misses call `stats.__wrapped__` directly -- no nested
    metrics event, only the one `stats_many` itself does not even emit (it is
    not a registered top-level verb; the verb that calls it, task 9/12, emits
    the single event for the whole set).
    """
    cache_path = _stats_cache_path(path, px)
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass  # corrupt/partial cache entry: fall through to a fresh render
        else:
            data["cached"] = True
            return data
    try:
        result = stats.__wrapped__(path, px=px)
    except ScouterError as e:
        return {"path": path, "error": {"code": e.code, "message": e.message}}
    except Exception as e:  # one bad frame must not fail the whole set
        return {"path": path, "error": {"code": "error", "message": str(e)}}
    data = result.to_dict()
    data["cached"] = False
    paths.ensure(cache_path)
    token = f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}"
    tmp = cache_path.with_name(f".{cache_path.stem}.{token}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, cache_path)
    return data


def stats_many(
    paths_in: list[str], *, px: int, workers: int, job_dir: Path | None = None
) -> list[dict]:
    """Stats for a set, cached on disk and rendered by a small thread pool.

    Each worker calls `render`, which already builds its OWN isolated configdir
    per call (`_render_configdir` uses a per-call token) and deletes it after,
    so K concurrent darktable-cli processes cannot contend for a library lock.
    One frame that fails does not fail the set: it comes back as
    {"path": ..., "error": {"code", "message"}} and the caller reports `failed`.
    """
    workers = max(1, min(workers, os.cpu_count() or 2))
    # Local import, deliberately (carried finding 8, task 5's nit: the same
    # import here and in `_cache_path` had no comment explaining itself, so
    # it read like an oversight): `scouter.config` imports `scouter.paths`
    # and is imported by `scouter.core.library`, which imports this module.
    # A module-level `from scouter.config import load` would close that
    # cycle at import time. Aliased `_load_config` so the name cannot be
    # mistaken for one of this module's own helpers.
    from scouter.config import load as _load_config

    cfg = _load_config()
    _prune_cache(paths.stats_dir(), cfg.stats.cache_max_age_days * 24 * 3600, STATS_CACHE_MAX_BYTES)

    slots: list[dict] = [{} for _ in paths_in]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_stats_many_one, p, px): i for i, p in enumerate(paths_in)}
        for future in as_completed(futures):
            slots[futures[future]] = future.result()

    ordered: list[dict] = slots
    if job_dir is not None:
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / f"stats-{px}.json").write_text(json.dumps(ordered, indent=2), encoding="utf-8")
    return ordered


def _cache_key(raw_path: str, px: int, ext: str) -> str:
    """A NAME, not a fingerprint (fix round 1, point 6).

    The mtime used to be baked into this key, so every commit left the
    previous preview orphaned on disk forever. Dropping it makes the name
    stable per (image, px, fmt): `preview` now decides staleness itself by
    comparing the cached file's own mtime against the sidecar's, and
    overwrites in place rather than growing a new file per edit. What
    accumulates instead -- one file per (image, px, fmt) pair ever
    requested -- is what `_prune_preview_cache` bounds by age and size.
    """
    digest = hashlib.sha1(f"{raw_path}|{px}|{ext}".encode()).hexdigest()[:16]
    return f"{Path(raw_path).stem}-{digest}.{ext}"


def _sidecar_mtime(raw_path: str) -> float | None:
    sidecar = xmp.sidecar_path(raw_path)
    return sidecar.stat().st_mtime if sidecar.exists() else None


def _prune_cache(directory: Path, max_age_s: float, max_bytes: int) -> None:
    """Age out anything older than `max_age_s`, then trim to a `max_bytes` budget.

    Generalised out of phase 1's `_prune_preview_cache` (task 5) so the same
    age+size policy also bounds `paths.stats_dir()`, whose entries accumulate
    exactly like previews do -- one file per (image, px, xmp mtime) key ever
    requested. Age culling runs first so a stale file never counts against
    the size budget; the size trim then removes the oldest surviving files
    until the total fits, so a burst of fresh entries is what stays.
    """
    if not directory.is_dir():
        return
    now = time.time()
    entries: list[tuple[float, int, Path]] = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if now - stat.st_mtime > max_age_s:
            path.unlink(missing_ok=True)
            continue
        entries.append((stat.st_mtime, stat.st_size, path))

    total = sum(size for _, size, _ in entries)
    if total <= max_bytes:
        return
    for _, size, path in sorted(entries, key=lambda e: e[0]):  # oldest first
        if total <= max_bytes:
            break
        path.unlink(missing_ok=True)
        total -= size


def _prune_preview_cache() -> None:
    """Thin wrapper kept so nothing else changes: age+size policy for previews."""
    _prune_cache(paths.preview_dir(), PREVIEW_MAX_AGE_S, PREVIEW_MAX_BYTES)


@metrics.instrumented("scout.preview")
def preview(
    image: str | int, *, px: int = 1024, fmt: str = "png", job: str | None = None
) -> PreviewResult:
    """Render a viewable preview and return its PATH, never its bytes."""
    if fmt not in ("png", "ppm"):
        raise ValueError(f"fmt must be png or ppm, got {fmt!r}")
    _validate_px(px)
    _prune_preview_cache()
    resolved = dtbridge.resolve_image(image)
    out = paths.preview_dir() / _cache_key(resolved.path, px, fmt)
    sidecar_mtime = _sidecar_mtime(resolved.path)
    fresh = (
        out.exists()
        and out.stat().st_size > 0
        and (sidecar_mtime is None or out.stat().st_mtime >= sidecar_mtime)
    )
    if fresh:
        return PreviewResult(
            image_id=resolved.image_id,
            path=resolved.path,
            preview_path=str(out),
            fmt=fmt,
            bytes=out.stat().st_size,
            px_requested=px,
            cached=True,
            warnings=[
                "preview served from cache (stable name, re-rendered only "
                "when the sidecar is newer than the cached file)"
            ],
        )
    render_ms = render(resolved.path, out, px, fmt)
    width = height = 0
    if fmt == "ppm":
        raster = ppm.parse_p6(out.read_bytes())
        width, height = raster.width, raster.height
    return PreviewResult(
        image_id=resolved.image_id,
        path=resolved.path,
        preview_path=str(out),
        fmt=fmt,
        width=width,
        height=height,
        bytes=out.stat().st_size,
        px_requested=px,
        render_ms=render_ms,
        cached=False,
        warnings=["read the file at `preview_path`; Scouter never inlines pixels " "(design 6.3)"],
    )


@metrics.instrumented("scout.compare")
def compare(
    a: str | int, b: str | int, *, px: int = 1024, budget_s: float = 3.0, job: str | None = None
) -> CompareResult:
    """Numeric distance between two images' current renders.

    `delta` is `stats(b) - stats(a)`, field by field: a positive
    `luminance_mean` means `b` is brighter than `a`, a positive
    `clipped_high_pct` means `b` clips more highlights, and so on -- a plain
    signed difference per field, not an aggregate score or a perceptual
    distance.

    Deviation from design 6.2: 6.2 describes reusing an already-cached render,
    but `compare` always renders both `a` and `b` fresh through
    `stats.__wrapped__` (only the wrapped verb's own metrics event is
    skipped, not its render) -- there is no cross-image render cache, so
    comparing costs two full render+analyse passes, one per image.
    """
    left = stats.__wrapped__(a, px=px, budget_s=budget_s, job=job)
    right = stats.__wrapped__(b, px=px, budget_s=budget_s, job=job)
    ls, rs = left.stats, right.stats
    warnings = [
        "deltas are b minus a; sharpness is comparable only between "
        "renders of the same frame (design 6.2)"
    ]
    if left.px_used != right.px_used:
        warnings.append(
            f"a rendered at {left.px_used}px but b at {right.px_used}px (one of "
            f"them hit the {budget_s:g}s loop budget and fell back to "
            f"{FALLBACK_PX}px): resolution-dependent fields such as sharpness "
            "are not directly comparable"
        )
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
        a={"image_id": left.image_id, "path": left.path, "stats": ls, "px_used": left.px_used},
        b={"image_id": right.image_id, "path": right.path, "stats": rs, "px_used": right.px_used},
        delta=delta,
        image_id=left.image_id,
        path=left.path,
        warnings=warnings,
    )


PX_ARG = ArgSpec("px", "int", default=1024, help="longest edge of the render, 64..4096")

for verb in (
    Verb(
        id="scout.stats",
        group="scout",
        fn=stats,
        args=(
            ArgSpec("image", "image", required=True, positional=True, help="path or image id"),
            PX_ARG,
            ArgSpec(
                "budget_s", "float", default=3.0, help="loop budget; over it, re-render at 512px"
            ),
        ),
        writes=False,
        gated=False,
        summary="Histogram, clipping, luminance percentiles, WB ratios, sharpness.",
    ),
    Verb(
        id="scout.preview",
        group="scout",
        fn=preview,
        args=(
            ArgSpec("image", "image", required=True, positional=True, help="path or image id"),
            PX_ARG,
            ArgSpec(
                "fmt", "str", default="png", choices=("png", "ppm"), help="preview file format"
            ),
        ),
        writes=False,
        gated=False,
        summary="Render a preview to a file and return its path.",
    ),
    Verb(
        id="scout.compare",
        group="scout",
        fn=compare,
        args=(
            ArgSpec("a", "image", required=True, positional=True, help="first image"),
            ArgSpec("b", "image", required=True, positional=True, help="second image"),
            PX_ARG,
            ArgSpec(
                "budget_s", "float", default=3.0, help="loop budget; over it, re-render at 512px"
            ),
        ),
        writes=False,
        gated=False,
        summary="Numeric distance between two renders.",
    ),
):
    register(verb)
