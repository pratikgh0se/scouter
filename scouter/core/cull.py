"""`Ki Sense`: stats-only cull assist. Groups a job into bursts, proposes a
rating for every frame with the numbers behind it, and applies a proposal's
ratings in batches, verified from the XMP.

**No frame is ever proposed rating 0 or reject** (design section 8.2, spec
4.8/13.4): stats cannot see eyes, expression or a deliberate motion blur, and
a stats-only culler that auto-rejected would throw frames away for reasons it
cannot see. Flagged frames land at `cull.keep_rating` with their numbers, and
Pratik decides. `cull.propose` never writes a rating and never calls the
bridge for anything but the read-only `image.lookup` behind
`dtbridge.resolve_image`; only `cull.apply` -- always gated -- calls
`image.rate`.

`cull.propose` persists exactly what it computed to
`<job>/.scouter/cull-<ts>.json`, `xmp_mtimes` included, so `cull.apply`
consumes the table Pratik actually read: no recomputation, no drift, and a
sidecar that changed since the proposal was written is a hard refusal, not a
silent re-derivation.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from scouter import config, metrics
from scouter.core import dtbridge, exif, scout, xmp
from scouter.errors import ScouterError, UsageError
from scouter.registry import ArgSpec, Verb, register
from scouter.types import CullApplyResult, CullResult

#: `image.rate` per call (design 8.3): a 300-frame job is ~6 calls, not one
#: giant payload the bridge's IPC spool has to marshal in one go.
BATCH_SIZE = 50

#: How long, and how often, `cull.apply` polls a changed frame's sidecar for
#: `xmp:Rating` to catch up before it is reported `unverified` rather than
#: `verified` (design 8.3). Module-level, not a default argument, so a test
#: can shrink them by assignment instead of waiting out the real window.
VERIFY_TIMEOUT_S = 2.0
VERIFY_INTERVAL_S = 0.05


def _capture_ts(captured_at: str | None, subsec: int) -> tuple[float | None, int]:
    """Epoch seconds with the fractional part folded in, plus the divisor used
    to fold it in (`subsec_scale`).

    SubSecTimeOriginal is hundredths on Pratik's body, but its digit count is
    not a universal constant across cameras, so the divisor is derived from
    how many digits `subsec` itself has (`10 ** len(str(subsec))`) rather than
    assumed to be 100 -- unit-agnostic, and the scale actually used is handed
    back rather than silently assumed.
    """
    if not captured_at:
        return None, 0
    dt = datetime.strptime(captured_at, "%Y-%m-%d %H:%M:%S")
    if subsec:
        scale = 10 ** len(str(subsec))
        frac = subsec / scale
    else:
        scale = 1
        frac = 0.0
    return dt.timestamp() + frac, scale


def _group_bursts(frames: list[dict], gap_s: float) -> list[dict]:
    """Consecutive frames within `gap_s` of each other, same camera, one burst.

    Frames with no capture time cannot be ordered against the rest, so they
    form one `no-time` group and are never proposed for a rating change --
    their reason says exactly that. Inventing a time to group them would be a
    guess about when they were shot.

    Each frame dict needs `seq`, `captured_at`, `subsec` and `camera`. Returns
    burst dicts `{burst_id, frames: [seq...], span_s, camera, tie_broken_by}`
    in encounter order (`b1`, `b2`, ...; `no-time` last, when present).
    `tie_broken_by` starts `None` here -- only the proposal pass (which knows
    sharpness) can detect a tie, and sets it on the same dict afterwards.
    """
    timed: list[tuple[float, dict]] = []
    no_time: list[dict] = []
    for f in frames:
        ts, _scale = _capture_ts(f.get("captured_at"), f.get("subsec") or 0)
        if ts is None:
            no_time.append(f)
        else:
            timed.append((ts, f))
    timed.sort(key=lambda pair: (pair[0], pair[1]["seq"]))

    groups: list[dict] = []
    current: dict | None = None
    burst_index = 0
    for ts, f in timed:
        if (
            current is not None
            and f.get("camera") == current["camera"]
            and (ts - current["_last_ts"]) <= gap_s
        ):
            current["frames"].append(f["seq"])
            current["_last_ts"] = ts
        else:
            burst_index += 1
            current = {
                "burst_id": f"b{burst_index}",
                "frames": [f["seq"]],
                "span_s": 0.0,
                "camera": f.get("camera"),
                "tie_broken_by": None,
                "_first_ts": ts,
                "_last_ts": ts,
            }
            groups.append(current)
        current["span_s"] = round(current["_last_ts"] - current["_first_ts"], 3)

    for g in groups:
        del g["_first_ts"]
        del g["_last_ts"]

    if no_time:
        groups.append(
            {
                "burst_id": "no-time",
                "frames": sorted(f["seq"] for f in no_time),
                "span_s": None,
                "camera": None,
                "tie_broken_by": None,
            }
        )
    return groups


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile, `q` in `[0, 1]` (same convention as
    `numpy.percentile`'s default `linear` method) -- stdlib only, no numpy
    dependency for one number."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = q * (len(ordered) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (idx - lo)


def _job_sharpness_threshold(frames: list[dict], cfg) -> float:
    """The job-relative sharpness gate for a singleton frame (E12 D1, row 407
    call 2): the value at the `(1 - sharpness_keep_top_pct)` percentile of
    THIS job's own sharpness numbers, so a singleton clears it exactly when
    it is in the sharpest `sharpness_keep_top_pct` fraction of the job --
    never an absolute number tuned on a different set of frames.
    """
    values = [f["sharpness"] for f in frames if "sharpness" in f]
    keep_top_pct = min(max(cfg.cull.sharpness_keep_top_pct, 0.0), 1.0)
    return _percentile(values, 1.0 - keep_top_pct)


def _propose_one(
    frame: dict, burst: dict, best_seq: int, cfg, sharpness_threshold: float
) -> tuple[int, list[str]]:
    """One frame's proposed rating and the reasons behind it, per design 8.2's
    table. `burst["members"]` (this frame's whole burst, itself included) is
    what lets the sharpness reasons name the runner-up's number."""
    reasons: list[str] = []

    # Carried finding 4 (task 6): a REJECTED frame is never promoted.
    # darktable stores a reject as a negative `xmp:Rating` (-1). The clamp
    # below is `min(candidate, keep_rating)`, and `min(2, 1)` is 1 -- so a
    # rejected frame that happened to be the sharpest of its burst came out
    # of here proposed for rating 1 or 2, and `cull.apply` would have
    # un-rejected a frame Pratik had deliberately thrown away. A reject is a
    # human decision about content this stats-only culler cannot see (design
    # 8.2's whole premise), so it is returned untouched, before any burst or
    # flag arithmetic runs.
    if frame["rating_now"] < 0:
        return frame["rating_now"], [
            f"rejected in darktable (xmp:Rating {frame['rating_now']}): a reject is "
            "your decision about content this stats-only culler cannot see, so it is "
            "never raised -- clear the reject in darktable to have it proposed again"
        ]

    flagged = False
    if frame["clipped_high_pct"] > cfg.cull.clipped_flag_pct:
        reasons.append(
            f"clipped {frame['clipped_high_pct']:.1f} % > " f"{cfg.cull.clipped_flag_pct:.1f} %"
        )
        flagged = True
    lum = frame["luminance_mean"]
    if lum < cfg.cull.luminance_low:
        reasons.append(f"mean luminance {lum:.0f} < {cfg.cull.luminance_low}")
        flagged = True
    elif lum > cfg.cull.luminance_high:
        reasons.append(f"mean luminance {lum:.0f} > {cfg.cull.luminance_high}")
        flagged = True

    members: list[dict] = burst["members"]
    if len(members) >= 2:
        ordered = sorted(members, key=lambda m: (-m["sharpness"], m["seq"]))
        best, second = ordered[0], ordered[1]
        if frame["seq"] == best_seq:
            candidate = cfg.cull.pick_rating
            reasons.append(
                f"sharpest of {len(members)} in burst {burst['burst_id']} "
                f"({frame['sharpness']:.2f} vs {second['sharpness']:.2f} next)"
            )
        else:
            candidate = cfg.cull.keep_rating
            reasons.append(
                f"softer than the pick of burst {burst['burst_id']} "
                f"({frame['sharpness']:.2f} vs {best['sharpness']:.2f})"
            )
    else:
        pct = cfg.cull.sharpness_keep_top_pct
        if frame["sharpness"] >= sharpness_threshold:
            candidate = cfg.cull.pick_rating
            reasons.append(
                f"singleton, sharpness {frame['sharpness']:.4f} >= this job's top "
                f"{pct:.0%} threshold {sharpness_threshold:.4f}"
            )
        else:
            candidate = cfg.cull.keep_rating
            reasons.append(
                f"singleton, sharpness {frame['sharpness']:.4f} < this job's top "
                f"{pct:.0%} threshold {sharpness_threshold:.4f} "
                f"(absolute floor {cfg.cull.sharpness_floor:.2f} is a diagnostic only)"
            )

    # No frame is ever proposed rating 0 or reject (design 8.2, spec 4.8/13.4):
    # a flagged frame is capped here, never lowered below `keep_rating` --
    # there is nothing below it to fall to.
    if flagged:
        candidate = min(candidate, cfg.cull.keep_rating)

    if frame["rating_now"] > candidate:
        candidate = frame["rating_now"]
        reasons.append(f"keeping your existing rating {frame['rating_now']}")

    return candidate, reasons


def _build_proposals(frames: list[dict], cfg) -> tuple[list[dict], list[dict], float]:
    """Group `frames` into bursts and propose a rating for every one of them.

    Each frame dict needs `seq`, `path`, `image_id`, `captured_at`, `subsec`,
    `camera`, `sharpness`, `clipped_high_pct`, `luminance_mean`, `rating_now`.
    Returns `(proposals, groups, sharpness_threshold)`, the first two
    sorted/ordered deterministically; `sharpness_threshold` is the job-
    relative singleton gate `_propose_one` used (row 407 call 2).
    """
    groups = _group_bursts(frames, cfg.cull.burst_gap_s)
    by_seq = {f["seq"]: f for f in frames}
    sharpness_threshold = _job_sharpness_threshold(frames, cfg)
    proposals: list[dict] = []

    for group in groups:
        members = [by_seq[s] for s in group["frames"]]
        if group["burst_id"] == "no-time":
            for f in members:
                proposals.append(
                    {
                        "image_id": f.get("image_id"),
                        "path": f.get("path"),
                        "seq": f["seq"],
                        "burst_id": group["burst_id"],
                        "rating_now": f["rating_now"],
                        "rating_proposed": f["rating_now"],
                        "sharpness": f.get("sharpness"),
                        "clipped_high_pct": f.get("clipped_high_pct"),
                        "luminance_mean": f.get("luminance_mean"),
                        "reasons": [
                            "no capture time: cannot be ordered against the rest of the "
                            f"set within the {cfg.cull.burst_gap_s:.1f} s burst window, "
                            "so it is never proposed for a rating change"
                        ],
                    }
                )
            continue

        burst = {"burst_id": group["burst_id"], "members": members}
        if len(members) >= 2:
            ordered = sorted(members, key=lambda m: (-m["sharpness"], m["seq"]))
            best_seq = ordered[0]["seq"]
            if ordered[0]["sharpness"] == ordered[1]["sharpness"]:
                group["tie_broken_by"] = "seq"
        else:
            best_seq = members[0]["seq"]

        for f in members:
            proposed, reasons = _propose_one(f, burst, best_seq, cfg, sharpness_threshold)
            proposals.append(
                {
                    "image_id": f.get("image_id"),
                    "path": f.get("path"),
                    "seq": f["seq"],
                    "burst_id": group["burst_id"],
                    "rating_now": f["rating_now"],
                    "rating_proposed": proposed,
                    "sharpness": f.get("sharpness"),
                    "clipped_high_pct": f.get("clipped_high_pct"),
                    "luminance_mean": f.get("luminance_mean"),
                    "reasons": reasons,
                }
            )

    proposals.sort(key=lambda p: p["seq"])
    return proposals, groups, sharpness_threshold


def _counts(proposals: list[dict], groups: list[dict], cfg) -> dict[str, int]:
    no_time_seqs = {s for g in groups if g["burst_id"] == "no-time" for s in g["frames"]}
    counts = {"pick": 0, "keep": 0, "unchanged": 0, "no_time": 0}
    for p in proposals:
        if p["seq"] in no_time_seqs:
            counts["no_time"] += 1
        elif p["rating_proposed"] == p["rating_now"]:
            counts["unchanged"] += 1
        elif p["rating_proposed"] == cfg.cull.pick_rating:
            counts["pick"] += 1
        else:
            counts["keep"] += 1
    return counts


@metrics.instrumented("cull.propose")
def propose(
    *,
    job_slug: str | None = None,
    images: list[str | int] | None = None,
    px: int | None = None,
    workers: int | None = None,
    job: str | None = None,
) -> CullResult:
    """Propose a rating for every frame in a job (or an explicit image list),
    grouped into bursts. Never writes a rating and never calls the bridge for
    anything but the read-only `image.lookup` behind `resolve_image`."""
    cfg = config.load()
    px = px if px is not None else cfg.stats.set_px
    workers = workers if workers is not None else cfg.stats.workers

    if not job_slug and not images:
        raise UsageError(
            "cull.propose needs --job-slug or --images",
            detail={"job_slug": job_slug, "images": images},
        )

    job_path: Path | None = None
    raw_frames: list[dict] = []
    if images:
        # Ruling R15: one `image.lookup` for the whole set. A miss still
        # raises (a cull over a set with a dead reference in it would be
        # culling something other than what was asked for), but it now
        # costs one round trip for the set instead of one per frame.
        for idx, (image, outcome) in enumerate(
            zip(images, dtbridge.resolve_images(list(images))), start=1
        ):
            if isinstance(outcome, ScouterError):
                raise outcome
            resolved = outcome
            tags = exif.read_tags(resolved.path)
            raw_frames.append(
                {
                    "seq": idx,
                    "path": resolved.path,
                    "image_id": resolved.image_id,
                    "captured_at": tags.captured_at,
                    "subsec": tags.subsec,
                    "camera": tags.model or "",
                }
            )
        if job_slug:
            job_path = config.find_job(cfg, job_slug)
    else:
        job_path = config.find_job(cfg, job_slug)
        import_path = config.scouter_dir(job_path) / "import.json"
        if not import_path.is_file():
            raise UsageError(
                f"no {import_path} for job {job_slug!r}: run `library import` first",
                detail={"job_slug": job_slug, "import_json": str(import_path)},
            )
        manifest = json.loads(import_path.read_text(encoding="utf-8"))
        manifest_files = list(manifest.get("files", []))
        manifest_paths = [str(job_path / f["dest"]) for f in manifest_files]
        for f, path, outcome in zip(
            manifest_files, manifest_paths, dtbridge.resolve_images(list(manifest_paths))
        ):
            if isinstance(outcome, ScouterError):
                raise outcome
            resolved = outcome
            raw_frames.append(
                {
                    "seq": f["seq"],
                    "path": path,
                    "image_id": resolved.image_id,
                    "captured_at": f.get("captured_at"),
                    "subsec": f.get("subsec") or 0,
                    "camera": manifest.get("camera", ""),
                }
            )

    stats_started = time.monotonic()
    paths_in = [f["path"] for f in raw_frames]
    stats_job_dir = config.scouter_dir(job_path) if job_path is not None else None
    # `unedited=True` (E12 D1): the cull must score the frame as imported,
    # never its current edited history -- otherwise editing the hero moves
    # the burst's pick out from under the photographer, as it did in the
    # live E12 run.
    stats_results = scout.stats_many(
        paths_in, px=px, workers=workers, job_dir=stats_job_dir, unedited=True
    )
    stats_ms = int((time.monotonic() - stats_started) * 1000)

    cached = computed = failed = 0
    frames: list[dict] = []
    for raw, stat in zip(raw_frames, stats_results):
        merged = dict(raw)
        merged["rating_now"] = xmp.rating(raw["path"]) or 0
        if "error" in stat:
            failed += 1
            # Neutral stand-ins, never a guess dressed up as a measurement:
            # a frame whose stats Scouter could not compute must not be
            # flagged or picked on numbers it does not have.
            merged["sharpness"] = 0.0
            merged["clipped_high_pct"] = 0.0
            merged["luminance_mean"] = (cfg.cull.luminance_low + cfg.cull.luminance_high) / 2
            merged["stats_error"] = stat["error"]
        else:
            frame_stats = stat.get("stats", {})
            merged["sharpness"] = frame_stats.get("sharpness", 0.0)
            merged["clipped_high_pct"] = frame_stats.get("clipped_high_pct", 0.0)
            merged["luminance_mean"] = frame_stats.get("luminance_mean", 0.0)
            if stat.get("cached"):
                cached += 1
            else:
                computed += 1
        frames.append(merged)

    proposals, groups, sharpness_threshold = _build_proposals(frames, cfg)
    counts = _counts(proposals, groups, cfg)

    warnings: list[str] = []
    failed_paths = [f["path"] for f in frames if "stats_error" in f]
    if failed_paths:
        warnings.append(
            f"stats failed for {len(failed_paths)} frame(s); they were not flagged or "
            "picked on numbers Scouter could not compute -- see each frame's own reason"
        )

    proposal_path = ""
    if job_path is not None:
        scouter_d = config.scouter_dir(job_path)
        scouter_d.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        out_path = scouter_d / f"cull-{ts}.json"
        xmp_mtimes: dict[str, int | None] = {}
        for f in frames:
            sidecar = xmp.sidecar_path(f["path"])
            xmp_mtimes[f["path"]] = sidecar.stat().st_mtime_ns if sidecar.exists() else None
        document = {
            "version": 1,
            "job_slug": job_slug or "",
            "created": datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            "px": px,
            "tolerances_used": asdict(cfg.cull),
            # The job-relative gate `_propose_one` actually used, computed
            # from THIS job's sharpness numbers (row 407 call 2).
            # `tolerances_used.sharpness_floor` is the old absolute number,
            # kept only as a diagnostic -- it gates nothing here.
            "sharpness_threshold": sharpness_threshold,
            "frames": proposals,
            "groups": groups,
            "xmp_mtimes": xmp_mtimes,
        }
        tmp = out_path.with_name(f".{out_path.name}.tmp")
        tmp.write_text(json.dumps(document, indent=2), encoding="utf-8")
        os.replace(tmp, out_path)
        proposal_path = str(out_path)
    else:
        warnings.append(
            "no --job-slug given: this proposal was not persisted, so `cull.apply` "
            "has nothing on disk to consume for it"
        )

    return CullResult(
        proposal_path=proposal_path,
        job_slug=job_slug or "",
        groups=groups,
        proposals=proposals,
        counts=counts,
        stats={
            "cached": cached,
            "computed": computed,
            "failed": failed,
            "ms": stats_ms,
            "sharpness_threshold": sharpness_threshold,
            "sharpness_floor_diagnostic": cfg.cull.sharpness_floor,
        },
        path=str(job_path) if job_path is not None else None,
        warnings=warnings,
    )


def _poll_verified_set(expected: dict[str, int]) -> set[str]:
    """Poll every sidecar in `expected` together, under ONE shared deadline.

    Returns the paths whose `xmp:Rating` caught up. darktable owns when it
    flushes to disk, so this never claims a write it cannot see -- it only
    reports what it read.

    Carried finding 4 (task 6): this used to be a per-frame serial poll, so
    the worst case was `len(frames) * VERIFY_TIMEOUT_S` -- 10 minutes for a
    300-frame job whose sidecars never caught up, on a verb whose real work
    is ~6 bridge calls. One deadline for the whole set bounds it at
    `VERIFY_TIMEOUT_S` no matter how many frames there are, and it is also
    *more* accurate: darktable flushes sidecars in its own batches, so the
    frames waited for concurrently are the frames that land concurrently.
    Each sweep re-reads only the frames still outstanding.
    """
    pending = dict(expected)
    verified: set[str] = set()
    deadline = time.monotonic() + VERIFY_TIMEOUT_S
    while True:
        for path in list(pending):
            if xmp.rating(path) == pending[path]:
                verified.add(path)
                del pending[path]
        if not pending or time.monotonic() >= deadline:
            return verified
        time.sleep(VERIFY_INTERVAL_S)


@metrics.instrumented("cull.apply")
def apply(
    *,
    job_slug: str | None = None,
    proposal: str | None = None,
    confirm: bool = False,
    dry_run: bool = False,
    job: str | None = None,
) -> CullApplyResult:
    """Apply a `cull.propose` proposal's ratings, in batches of `BATCH_SIZE`,
    verified from the XMP. Always gated: a cull touches many images."""
    cfg = config.load()
    if proposal:
        proposal_path = Path(proposal)
        if not proposal_path.is_file():
            raise UsageError(
                f"no proposal file at {proposal_path}", detail={"proposal": str(proposal_path)}
            )
    elif job_slug:
        job_path = config.find_job(cfg, job_slug)
        scouter_d = config.scouter_dir(job_path)
        candidates = sorted(scouter_d.glob("cull-*.json"))
        if not candidates:
            raise UsageError(
                f"no cull-*.json proposal under {scouter_d}: run `cull propose "
                f"--job-slug {job_slug}` first",
                detail={"job_slug": job_slug, "scouter_dir": str(scouter_d)},
            )
        proposal_path = candidates[-1]
    else:
        raise UsageError(
            "cull.apply needs --job-slug or --proposal",
            detail={"job_slug": job_slug, "proposal": proposal},
        )

    data = json.loads(proposal_path.read_text(encoding="utf-8"))
    resolved_job_slug = job_slug or data.get("job_slug") or ""
    frames = data.get("frames", [])
    recorded_mtimes = data.get("xmp_mtimes", {})

    # The proposal is stale the moment any recorded frame's sidecar mtime has
    # moved: the numbers Pratik read are no longer the numbers on disk, and
    # `cull.apply` must not silently re-derive new ones (design 8.3).
    stale: list[str] = []
    for f in frames:
        path = f["path"]
        recorded = recorded_mtimes.get(path)
        sidecar = xmp.sidecar_path(path)
        current = sidecar.stat().st_mtime_ns if sidecar.exists() else None
        if recorded != current:
            stale.append(path)
    if stale:
        raise UsageError(
            f"{len(stale)} frame(s) changed since this proposal was written "
            f"({', '.join(Path(p).name for p in stale[:5])}"
            f"{', ...' if len(stale) > 5 else ''}); re-run `cull propose` for fresh numbers",
            detail={"proposal": str(proposal_path), "stale_frames": stale},
        )

    to_change = [f for f in frames if f["rating_proposed"] != f["rating_now"]]
    unchanged = len(frames) - len(to_change)
    by_rating: dict[int, list[dict]] = {}
    for f in to_change:
        by_rating.setdefault(f["rating_proposed"], []).append(f)

    plan = [
        {"action": "cull.apply", "rating": rating, "count": len(fs)}
        for rating, fs in sorted(by_rating.items())
    ]
    if dry_run or not confirm:
        return CullApplyResult(
            outcome="dry_run",
            job_slug=resolved_job_slug,
            proposal_path=str(proposal_path),
            would_change=len(to_change),
            unchanged=unchanged,
            plan=plan,
            reason=(
                "dry run: no ratings were written"
                if dry_run
                else f"{len(to_change)} frame(s) would change; re-run with --yes / confirm=true"
            ),
        )

    missed_ids: list[int] = []
    per_bridge: dict[int, dict] = {}
    for rating_value, group in sorted(by_rating.items()):
        ids = [int(f["image_id"]) for f in group]
        for i in range(0, len(ids), BATCH_SIZE):
            chunk = ids[i : i + BATCH_SIZE]
            result = dtbridge.call("image.rate", {"image_ids": chunk, "rating": rating_value})
            for missed in result.get("missed", []):
                missed_ids.append(int(missed))
            for entry in result.get("per_image", []):
                per_bridge[int(entry["image_id"])] = entry

    # One poll pass over every frame the bridge did not miss, before the
    # per-image rows are built: the whole set shares one deadline.
    verified_paths = _poll_verified_set(
        {
            f["path"]: f["rating_proposed"]
            for f in to_change
            if int(f["image_id"]) not in set(missed_ids)
        }
    )

    per_image: list[dict] = []
    verified_count = 0
    unverified_count = 0
    for f in to_change:
        image_id = int(f["image_id"])
        if image_id in missed_ids:
            per_image.append(
                {
                    "image_id": image_id,
                    "rating_before": f["rating_now"],
                    "rating_after": f["rating_now"],
                    "verified": False,
                }
            )
            continue
        entry = per_bridge.get(image_id, {})
        rating_before = entry.get("rating_before", f["rating_now"])
        rating_after = entry.get("rating_after", f["rating_proposed"])
        verified = f["path"] in verified_paths
        if verified:
            verified_count += 1
        else:
            unverified_count += 1
        per_image.append(
            {
                "image_id": image_id,
                "rating_before": rating_before,
                "rating_after": rating_after,
                "verified": verified,
            }
        )

    applied = len(to_change) - len(missed_ids)
    warnings: list[str] = []
    if unverified_count:
        warnings.append(
            f"{unverified_count} rating(s) landed in darktable's database but the XMP "
            "sidecar had not caught up within the poll window; the write is real and "
            "the sidecar will follow -- this does not claim a write Scouter cannot see"
        )

    rows = [
        ("applied", str(applied)),
        ("unchanged", str(unchanged)),
        ("missed", str(len(missed_ids))),
        ("verified", str(verified_count)),
        ("unverified", str(unverified_count)),
    ]
    try:
        from scouter.core import vault

        vault.append_stage(cfg, resolved_job_slug, "Cull", rows, "cull proposed")
    except Exception as e:  # noqa: BLE001 - vault.py hasn't landed yet (task 4)
        # Until vault.py lands this is always an ImportError; caught the same
        # way any later vault failure is: a warning, never a rollback of
        # ratings that already landed.
        warnings.append(f"vault stage not appended: {e}")

    return CullApplyResult(
        job_slug=resolved_job_slug,
        proposal_path=str(proposal_path),
        applied=applied,
        unchanged=unchanged,
        would_change=len(to_change),
        missed=missed_ids,
        verified=verified_count,
        unverified=unverified_count,
        per_image=per_image,
        warnings=warnings,
    )


for _verb in (
    Verb(
        id="cull.propose",
        group="cull",
        fn=propose,
        args=(
            ArgSpec("job_slug", "str", help="job slug; frames come from its import.json"),
            ArgSpec(
                "images",
                "list[str]",
                help="explicit image list, instead of (or alongside) --job-slug",
            ),
            ArgSpec("px", "int", help="stats render size (default: stats.set_px)"),
            ArgSpec("workers", "int", help="thread pool size (default: stats.workers)"),
        ),
        writes=False,
        gated=False,
        summary="Propose cull ratings for a job's frames, grouped into bursts.",
    ),
    Verb(
        id="cull.apply",
        group="cull",
        fn=apply,
        args=(
            ArgSpec(
                "job_slug",
                "str",
                help="job slug; loads the newest cull-*.json unless --proposal is given",
            ),
            ArgSpec("proposal", "str", help="path to a specific cull-<ts>.json"),
            ArgSpec("dry_run", "bool", default=False, help="plan only"),
            ArgSpec(
                "confirm", "bool", default=False, help="required: writes ratings via image.rate"
            ),
        ),
        writes=True,
        gated=True,
        summary="Apply a cull proposal's ratings, verified from the XMP sidecars.",
    ),
):
    register(_verb)
