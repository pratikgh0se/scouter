"""Export presets, `export.release`'s selection + verifiable manifest, and
`export.verify` (spec 4.7, design section 10).

Four presets: `preview` and `client-final` from phase 1, `client-proof` and
`print-tiff` new here. `PRESETS` carries a `note` per preset, which lands in
`warnings`, so the result always says what was actually rendered. Watermark
is deliberately absent (spec 4.7 calls it optional; it needs a darktable
watermark preset plus an SVG, and Pratik has not asked for one).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from scouter import __version__, config, metrics
from scouter.core import dtbridge, scout, wb, xmp
from scouter.errors import ChecksumMismatch, ExportAllFailed, ManifestUnauthenticated, UsageError
from scouter.paths import current_job
from scouter.registry import ArgSpec, Verb, register
from scouter.types import ExportedFile, ExportResult, ReleaseResult, VerifyResult

#: preset -> ext / longest edge in px (0 = full size) / quality or bit depth / ICC.
PRESETS: dict[str, dict] = {
    "preview": {"ext": "png", "px": 1024, "note": "1024 px PNG for review"},
    "client-proof": {
        "ext": "jpg",
        "px": 2048,
        "quality": 90,
        "icc": "SRGB",
        "note": "2048 px sRGB JPEG q90 -- client proof",
    },
    "client-final": {
        "ext": "jpg",
        "px": 0,
        "quality": 95,
        "icc": "SRGB",
        "note": "full-size sRGB JPEG q95",
    },
    "print-tiff": {
        "ext": "tif",
        "px": 0,
        "bpp": 16,
        "icc": "ADOBERGB",
        "note": "full-size 16-bit AdobeRGB TIFF, deflate",
    },
}

#: `export.release`'s selection rule (controller ruling R6, design 20.4):
#: proofs go by rating, finals go by the colour label Pratik set on his picks.
FINAL_PRESETS = frozenset({"client-final", "print-tiff"})

#: darktable's own colour-label vocabulary (fix round 1, should-fix 6): the
#: five booleans `image.color_labels` reads off `dt.database[id]`. A typo in
#: `export.final_label` (e.g. "gren") is refused here, not silently treated
#: as "matches nothing".
COLOR_LABEL_VOCAB = frozenset({"red", "yellow", "green", "blue", "purple"})

#: Streaming chunk size for output-file hashing (matches library.py's `CHUNK`).
_CHUNK = 1 << 20


def preset_args(
    spec: dict, *, style: str | None = None, overwrite_style: bool = False
) -> list[str]:
    """Every darktable-cli flag a preset needs, built in ONE place.

    Never emits `--core` (fix round 1, blocker 1): `CLIWrapper.export_image`
    owns the single `--core --configdir` block and splits this list at its
    first literal `"--conf"` to decide what precedes it (icc-type/style,
    darktable-cli's own pre-`--core` options) and what follows it (the
    `--conf key=value` pairs, always built last, below) -- a second `--core`
    token on the same command line would restart darktable-cli's own "rest
    is core args" parsing and strand whatever this preset put after it.
    """
    args: list[str] = []
    if spec.get("icc"):
        args += ["--icc-type", spec["icc"]]
    if style:
        args += ["--style", style]
        if overwrite_style:
            args += ["--style-overwrite"]
    if "quality" in spec:
        args += ["--conf", f"plugins/imageio/format/jpeg/quality={spec['quality']}"]
    if "bpp" in spec:
        args += [
            "--conf",
            f"plugins/imageio/format/tiff/bpp={spec['bpp']}",
            "--conf",
            "plugins/imageio/format/tiff/compress=5",
        ]
    return args


def _sha256_file(path: Path) -> str:
    """One sha256 pass over `path`, streamed -- used for OUTPUT files only.

    `export.release`'s `input_sha256` is read from `import.json`, never
    recomputed here (design 10.2): re-hashing a raw file this way would defeat
    the whole point of trusting the import's own verified checksum.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(directory: Path) -> None:
    """Carried finding 6: fsync the DIRECTORY after an atomic rename.

    `fsync` on the temp file only guarantees the bytes; on macOS/APFS and
    on ext4 the directory entry that makes them reachable under the final
    name is a separate metadata write. Without this a crash can leave a
    manifest whose contents are durable but whose name is not -- which for
    a delivery manifest is the same as losing it. Best effort: a
    filesystem that refuses `O_RDONLY` on a directory (or refuses to sync
    one) is not a reason to fail a release that already wrote its files.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_atomic(path: Path, content: str, *, fsync: bool = False) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(content)
        if fsync:
            fh.flush()
            os.fsync(fh.fileno())
    os.replace(tmp, path)
    if fsync:
        _fsync_dir(path.parent)


def _now_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _plan_outputs(items: list, path_of, out_dir: Path, ext: str) -> list[tuple[object, Path]]:
    """One output path per item, deterministic and collision-free.

    `path_of(item)` yields the item's source path, so both callers share
    this one implementation: `export.release` passes candidate dicts,
    `export.run` passes `ResolvedImage`s (carried finding 6 -- `run` built
    its own `<stem>.<ext>` inline with no collision check at all, so
    `export.run /a/x.orf /b/x.orf` rendered twice into the same file and
    reported both as exported).

    A raw and a same-stem JPEG that share a `seq` (a card holding both
    RAW+JPEG for a frame) would otherwise both plan `<stem>.<ext>`, and the
    second render would silently clobber the first (fix round 1, blocker
    2). Any stem shared by more than one selected item gets the source's
    own suffix folded in -- `<stem>-orf.jpg` / `<stem>-jpg.jpg` --
    deterministic (same input suffix always yields the same output name,
    unlike a `-2`/`-3` counter that depends on selection order). A
    collision that survives even that (two items with the same stem AND the
    same source suffix, i.e. the same filename from two directories) is
    refused rather than silently overwritten.
    """
    stem_counts: dict[str, int] = {}
    for item in items:
        stem = Path(path_of(item)).stem
        stem_counts[stem] = stem_counts.get(stem, 0) + 1

    planned: list[tuple[object, Path]] = []
    seen: dict[Path, str] = {}
    for item in items:
        source = Path(path_of(item))
        if stem_counts[source.stem] > 1:
            suffix = source.suffix.lstrip(".").lower() or "src"
            name = f"{source.stem}-{suffix}.{ext}"
        else:
            name = f"{source.stem}.{ext}"
        out = out_dir / name
        if out in seen:
            raise UsageError(
                f"two selected frames would both export to {out.name}: "
                f"{seen[out]} and {source}",
                detail={"output": str(out), "inputs": [seen[out], str(source)]},
            )
        seen[out] = str(source)
        planned.append((item, out))
    return planned


def _planned_outputs(selected: list[dict], out_dir: Path, ext: str) -> list[tuple[dict, Path]]:
    """`_plan_outputs` for `export.release`'s candidate dicts."""
    return [
        (c, out) for c, out in _plan_outputs(selected, lambda c: c["path"], out_dir, ext)
    ]  # type: ignore[misc]


def _select_by_label(
    candidates: list[dict], label: str, warnings: list[str]
) -> tuple[list[dict], dict[str, int]]:
    """Candidates carrying `label`, per one batched bridge call, plus the
    label histogram every candidate the bridge DID resolve actually carries.

    Read-only, like `image.lookup` (controller ruling R6: "both read through
    the bridge"; ruling R12, fix round 0: `image.color_labels` now exists in
    the Lua plugin, reading darktable's own `red`/`yellow`/`green`/`blue`/
    `purple` boolean fields off `dt.database[id]`). Every candidate is
    resolved to an image id first (`dtbridge.resolve_image`, itself
    read-only) and looked up in ONE call, not one per frame.

    Two distinct kinds of "the bridge doesn't know this frame" (fix round 1,
    should-fix 5), neither of which aborts the release:
    - `ImageNotFound` (the frame was never opened/imported into darktable's
      library, so it has no image id to look up at all) -- one warning
      naming every such path, the frame is simply excluded from the label
      lookup.
    - the bridge's own `missing` list (an id it looked up but does not
      recognise) -- a separate warning naming the ids.
    """
    by_image_id: dict[int, dict] = {}
    unregistered: list[str] = []
    for c in candidates:
        try:
            resolved = dtbridge.resolve_image(c["path"])
        except dtbridge.ImageNotFound:
            unregistered.append(c["path"])
            continue
        by_image_id[resolved.image_id] = c
    if unregistered:
        warnings.append(
            f"{len(unregistered)} frame(s) are not registered in darktable's library "
            f"(never opened/imported), so their colour label could not be read and "
            f"they were not label-selected: {unregistered}"
        )
    if not by_image_id:
        return [], {}

    result = dtbridge.call("image.color_labels", {"image_ids": list(by_image_id)})
    missing = result.get("missing") or []
    if missing:
        warnings.append(
            f"colour label lookup missed {len(missing)} image id(s) not in darktable's "
            f"library: {missing}"
        )
    selected: list[dict] = []
    histogram: dict[str, int] = {}
    for entry in result.get("per_image") or []:
        c = by_image_id.get(int(entry["image_id"]))
        if c is None:
            continue
        labels = entry.get("labels") or []
        for lb in labels or ["none"]:
            histogram[lb] = histogram.get(lb, 0) + 1
        if label in labels:
            selected.append(c)
    return selected, histogram


@metrics.instrumented("export.run")
def run(
    images: list[str | int],
    preset: str,
    out_dir: str,
    *,
    confirm: bool = False,
    dry_run: bool = False,
    job: str | None = None,
) -> ExportResult:
    """Export one or more images through a preset. Gated on overwrite and on n>1."""
    job = job or current_job()
    if preset not in PRESETS:
        raise ValueError(f"preset must be one of {sorted(PRESETS)}, got {preset!r}")
    spec = PRESETS[preset]
    destination = Path(out_dir)
    # Carried finding 6: an empty selection is a caller mistake, never a
    # zero-file success. `export.run` with no images used to fall straight
    # through the n>1 gate and return `exported=0, failed=0, outcome="ok"` --
    # a phantom success for work it never did. (`export.release` already
    # refuses its own empty selections; this is the same rule one layer down.)
    if not images:
        raise UsageError(
            "export.run needs at least one image",
            detail={"preset": preset, "out_dir": out_dir, "images": images},
        )
    resolved = [dtbridge.resolve_image(image) for image in images]

    # Carried finding 6: the same collision guard `export.release` uses.
    # This loop used to be `destination / f"{stem}.{ext}"` with no check, so
    # two inputs sharing a filename (`/a/x.orf` and `/b/x.orf`, or a
    # RAW+JPEG pair) rendered one on top of the other and BOTH were
    # reported exported.
    planned = _plan_outputs(resolved, lambda r: r.path, destination, spec["ext"])
    existing = [str(out) for _, out in planned if out.exists()]

    plan = [
        {
            "action": "export.run",
            "image_id": item.image_id,
            "input": item.path,
            "output": str(out),
            "exists": out.exists(),
            "preset": preset,
        }
        for item, out in planned
    ]
    needs_confirm = len(planned) > 1 or bool(existing)
    if dry_run or (needs_confirm and not confirm):
        reasons = []
        if len(planned) > 1:
            reasons.append(f"{len(planned)} images in one call")
        if existing:
            reasons.append(f"{len(existing)} output file(s) already exist")
        return ExportResult(
            outcome="dry_run",
            preset=preset,
            out_dir=str(destination),
            plan=plan,
            image_id=resolved[0].image_id if resolved else None,
            path=resolved[0].path if resolved else None,
            reason=(
                "; ".join(reasons) + "; re-run with --yes / confirm=true"
                if needs_confirm and not confirm
                else "dry run: nothing was exported"
            ),
        )

    destination.mkdir(parents=True, exist_ok=True)
    files: list[ExportedFile] = []
    for item, out in planned:
        try:
            scout.render(item.path, out, spec["px"] or 0, spec["ext"])
        except Exception as e:  # one bad file must not lose the rest
            files.append(
                ExportedFile(input=item.path, output=None, ok=False, bytes=0, error=str(e))
            )
            continue
        files.append(
            ExportedFile(
                input=item.path, output=str(out), ok=True, bytes=out.stat().st_size, error=None
            )
        )
    exported = sum(1 for f in files if f.ok)
    return ExportResult(
        preset=preset,
        out_dir=str(destination),
        files=files,
        exported=exported,
        failed=len(files) - exported,
        image_id=resolved[0].image_id if resolved else None,
        path=resolved[0].path if resolved else None,
        warnings=[spec["note"]]
        + (
            [f"{len(files) - exported} file(s) failed; see `files[].error`"]
            if exported != len(files)
            else []
        ),
    )


@metrics.instrumented("export.release")
def release(
    job_slug: str,
    preset: str,
    *,
    date: str | None = None,
    min_rating: int | None = None,
    images: list[str | int] | None = None,
    style: str | None = None,
    overwrite_style: bool = False,
    confirm: bool = False,
    dry_run: bool = False,
    job: str | None = None,
) -> ReleaseResult:
    """Export a job's selected frames through a preset, with a checksum manifest.

    Selection (design 10.2, controller ruling R6): `images` if given, else
    every raw in `import.json` whose `xmp.rating >= min_rating` for
    `client-proof`/`preview`, or that carries `cfg.export.final_label`
    (green by default -- Pratik's Q4 recommendation) as a colour label for
    `client-final`/`print-tiff`. An empty selection is a `usage` refusal
    naming the rule and the ratings histogram it found -- never a zero-file
    "success".
    """
    if preset not in PRESETS:
        raise ValueError(f"preset must be one of {sorted(PRESETS)}, got {preset!r}")
    spec = PRESETS[preset]
    cfg = config.load()
    job_path = config.find_job(cfg, job_slug, date)
    import_path = config.scouter_dir(job_path) / "import.json"
    if not import_path.is_file():
        raise UsageError(
            f"no {import_path} for job {job_slug!r}: run `library import` first",
            detail={"job_slug": job_slug, "import_json": str(import_path)},
        )
    manifest_in = json.loads(import_path.read_text(encoding="utf-8"))

    candidates: list[dict] = []
    for f in manifest_in.get("files", []):
        path = str(job_path / f["dest"])
        candidates.append(
            {
                "dest": f["dest"],
                "path": path,
                "input_sha256": f.get("sha256"),
                "rating": xmp.rating(path) or 0,
            }
        )

    resolved_min_rating = cfg.export.min_rating if min_rating is None else min_rating
    label = cfg.export.final_label or "green"
    warnings: list[str] = []

    if images:
        by_path = {c["path"]: c for c in candidates}
        selected: list[dict] = []
        for image in images:
            resolved = dtbridge.resolve_image(image)
            c = by_path.get(resolved.path)
            if c is None:
                warnings.append(
                    f"{resolved.path} is not in {import_path}'s files[]: exported with "
                    "no input_sha256 in the manifest"
                )
                c = {
                    "dest": None,
                    "path": resolved.path,
                    "input_sha256": None,
                    "rating": xmp.rating(resolved.path) or 0,
                }
            selected.append(c)
        rule = "images"
        label_histogram: dict[str, int] = {}
    elif preset in FINAL_PRESETS:
        if label not in COLOR_LABEL_VOCAB:
            raise UsageError(
                f"export.final_label {label!r} is not one of darktable's colour labels "
                f"({', '.join(sorted(COLOR_LABEL_VOCAB))})",
                detail={"final_label": label, "vocabulary": sorted(COLOR_LABEL_VOCAB)},
            )
        if candidates:
            selected, label_histogram = _select_by_label(candidates, label, warnings)
        else:
            selected, label_histogram = [], {}
        rule = f"color_label={label}"
        warnings.append(
            "colour-label selection reads a real, but still HOST-unverified, "
            "darktable Lua field (image.color_labels); task 14 confirms it live"
        )
    else:
        selected = [c for c in candidates if c["rating"] >= resolved_min_rating]
        rule = f"rating>={resolved_min_rating}"
        label_histogram = {}

    if not selected:
        # Carried finding 6: the warnings collected so far travel WITH the
        # refusal. They are usually the actual explanation -- "12 frames are
        # not registered in darktable's library, so their colour label could
        # not be read" is why the label selection came back empty, and
        # raising a bare histogram used to throw that sentence away and
        # leave the caller staring at `{}`.
        suffix = (" Warnings: " + " | ".join(warnings)) if warnings else ""
        if rule.startswith("color_label="):
            raise UsageError(
                f"no frames matched {rule} (0 of {len(candidates)}); label histogram "
                f"(label -> count of frames): {label_histogram}.{suffix}",
                detail={
                    "rule": rule,
                    "final_label": label,
                    "histogram": label_histogram,
                    "of": len(candidates),
                    "warnings": warnings,
                },
            )
        histogram: dict[str, int] = {}
        for c in candidates:
            key = str(c["rating"])
            histogram[key] = histogram.get(key, 0) + 1
        raise UsageError(
            f"no frames matched {rule} (0 of {len(candidates)}); ratings histogram "
            f"(rating -> count of frames): {histogram}.{suffix}",
            detail={
                "rule": rule,
                "min_rating": resolved_min_rating,
                "histogram": histogram,
                "of": len(candidates),
                "warnings": warnings,
            },
        )

    out_dir = config.exports_dir(job_path, preset)
    planned = _planned_outputs(selected, out_dir, spec["ext"])
    existing = [str(out) for _, out in planned if out.exists()]

    plan = [
        {
            "action": "export.release",
            "input": c["path"],
            "output": str(out),
            "exists": out.exists(),
            "preset": preset,
        }
        for c, out in planned
    ]
    # Fresh (no existing output) is still gated at n>1, exactly like
    # export.run (fix round 1, should-fix 4): a release always touches more
    # than one file's worth of judgment call (the selection rule), so the
    # FIRST one of more than one frame is a dry run too, not just a repeat.
    needs_confirm = len(planned) > 1 or bool(existing)
    if dry_run or (needs_confirm and not confirm):
        reasons = []
        if len(planned) > 1:
            reasons.append(f"{len(planned)} frames selected")
        if existing:
            reasons.append(f"{len(existing)} output file(s) already exist")
        return ReleaseResult(
            outcome="dry_run",
            job_slug=job_slug,
            preset=preset,
            out_dir=str(out_dir),
            selection_rule=rule,
            selected=len(selected),
            of=len(candidates),
            plan=plan,
            reason=(
                "; ".join(reasons) + "; re-run with --yes / confirm=true"
                if needs_confirm and not confirm
                else "dry run: nothing was exported"
            ),
            warnings=warnings,
            path=str(job_path),
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    extra_args = preset_args(spec, style=style, overwrite_style=overwrite_style)

    # Carried finding 10: this stored the raw multi-line `--version` blob in
    # the delivery manifest and shelled out unconditionally -- including from
    # unit tests running against a FakeBridge, where `scout.cli_binary()`
    # still resolves the real Mac install path. `wb._resolve_darktable_version`
    # already solved both: it returns the parsed `X.Y.Z` token (or
    # `"unknown"`, never `None`) and refuses to spawn a subprocess unless the
    # installed bridge is the real one.
    darktable_version = wb._resolve_darktable_version()

    files_result: list[ExportedFile] = []
    manifest_files: list[dict] = []
    total_bytes = 0
    for c, out in planned:
        try:
            render_ms = scout.render(
                c["path"], out, spec["px"] or 0, spec["ext"], extra_args=extra_args
            )
        except Exception as e:  # one bad file must not lose the rest
            files_result.append(
                ExportedFile(input=c["path"], output=None, ok=False, bytes=0, error=str(e))
            )
            continue
        size = out.stat().st_size
        total_bytes += size
        output_sha256 = _sha256_file(out)
        files_result.append(
            ExportedFile(input=c["path"], output=str(out), ok=True, bytes=size, error=None)
        )
        manifest_files.append(
            {
                "input": c["dest"] or c["path"],
                "input_sha256": c["input_sha256"],
                "output": f"exports/{preset}/{out.name}",
                "output_sha256": output_sha256,
                "bytes": size,
                "px": spec["px"],
                "render_ms": render_ms,
            }
        )

    exported = sum(1 for f in files_result if f.ok)
    failed = len(files_result) - exported

    if exported == 0 and files_result:
        # Fix round 1, should-fix 7: every render failed -- a typed refusal,
        # never a success with exported=0. Raised BEFORE the manifest is
        # written: a manifest describing zero exported files is not a
        # release.
        raise ExportAllFailed(
            f"all {len(files_result)} render(s) failed for preset {preset!r}",
            detail={
                "files": [{"input": f.input, "error": f.error} for f in files_result if not f.ok]
            },
        )

    scouter_d = config.scouter_dir(job_path)
    scouter_d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    manifest_doc = {
        "version": 1,
        "job_slug": job_slug,
        "preset": preset,
        "released_at": _now_z(),
        "scouter_version": __version__,
        "darktable": darktable_version,
        "style": style,
        "style_overwrite": overwrite_style,
        "selection": {"rule": rule, "selected": len(selected), "of": len(candidates)},
        "input_sha256_source": "import.json",
        "files": manifest_files,
        "totals": {"exported": exported, "failed": failed, "bytes": total_bytes},
    }
    # `manifest-<preset>-<ts>.sha256` (fix round 1, nit 8: design 10.2's own
    # naming -- not `.json.sha256`). Both files fsync'd before the atomic
    # rename, manifest first then its sidecar, so a crash between the two
    # writes never leaves a sidecar authenticating a manifest that isn't
    # durably on disk yet.
    manifest_path = scouter_d / f"manifest-{preset}-{ts}.json"
    manifest_text = json.dumps(manifest_doc, indent=2)
    _write_atomic(manifest_path, manifest_text, fsync=True)
    manifest_sha = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
    sha_path = manifest_path.with_suffix(".sha256")
    _write_atomic(sha_path, f"{manifest_sha}  {manifest_path.name}\n", fsync=True)

    if failed:
        warnings.append(f"{failed} file(s) failed; see `files[].error`")
    warnings.append(spec["note"])

    try:
        from scouter.core import vault

        rows = [
            ("preset", preset),
            ("selected", f"{len(selected)} of {len(candidates)} ({rule})"),
            ("exported", str(exported)),
            ("failed", str(failed)),
            ("bytes", str(total_bytes)),
        ]
        vault.append_stage(cfg, job_slug, "Export", rows, f"{preset} exported")
    except Exception as e:  # noqa: BLE001 - vault.py hasn't landed yet (task 4)
        warnings.append(f"vault stage not appended: {e}")

    return ReleaseResult(
        job_slug=job_slug,
        preset=preset,
        out_dir=str(out_dir),
        manifest_path=str(manifest_path),
        selection_rule=rule,
        selected=len(selected),
        of=len(candidates),
        exported=exported,
        failed=failed,
        bytes=total_bytes,
        files=files_result,
        warnings=warnings,
        path=str(job_path),
    )


@metrics.instrumented("export.verify")
def verify(manifest: str, *, root: str | None = None, job: str | None = None) -> VerifyResult:
    """Re-hash every output in a release manifest against it.

    First checks the manifest against its own `manifest-<preset>-<ts>.sha256`
    sidecar (`export.release` writes one alongside every manifest, fsync'd)
    -- a MISSING sidecar is a typed refusal (`manifest_unauthenticated`,
    fix round 1, blocker 3), never an optional check silently skipped: a
    manifest with no way to authenticate it must not be trusted to check the
    outputs it claims to describe. A sidecar that IS present but does not
    match is `checksum_mismatch`, same as `library.verify`'s own tampering
    story. Returns `library.verify`'s result shape (`checked`, `ok`,
    `mismatched`, `missing`) either way.

    `root` (fix round 1, nit 8) is the directory `files[].output`'s relative
    paths resolve against; default is the manifest's own `<job>/.scouter/`
    parent (`manifest_path.parent.parent`), which is right when the manifest
    is still where `export.release` wrote it. Pass `root` explicitly to
    verify a manifest that shipped BESIDE its `exports/` folder instead
    (e.g. copied onto a USB stick handed to a client) rather than nested
    under `.scouter/`.
    """
    manifest_path = Path(manifest)
    if not manifest_path.is_file():
        raise UsageError(f"no manifest at {manifest_path}", detail={"manifest": str(manifest_path)})
    manifest_text = manifest_path.read_text(encoding="utf-8")
    actual_manifest_sha = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()

    sha_path = manifest_path.with_suffix(".sha256")
    if not sha_path.is_file():
        raise ManifestUnauthenticated(
            f"no {sha_path} next to {manifest_path}: cannot verify an unauthenticated manifest",
            detail={"manifest": str(manifest_path), "sha256_sidecar": str(sha_path)},
        )
    recorded_line = sha_path.read_text(encoding="utf-8").strip()
    recorded_sha = recorded_line.split()[0] if recorded_line else ""
    if recorded_sha != actual_manifest_sha:
        raise ChecksumMismatch(
            f"{manifest_path} does not match {sha_path}",
            detail={
                "expected": recorded_sha,
                "actual": actual_manifest_sha,
                "manifest": str(manifest_path),
            },
        )

    doc = json.loads(manifest_text)
    base = Path(root) if root else manifest_path.parent.parent
    checked = 0
    mismatched: list[dict] = []
    missing: list[str] = []
    for f in doc.get("files", []):
        out_rel = f.get("output", "")
        out_path = base / out_rel
        if not out_path.is_file():
            missing.append(out_rel)
            continue
        checked += 1
        actual = _sha256_file(out_path)
        expected = f.get("output_sha256", "")
        if actual != expected:
            mismatched.append({"name": out_rel, "expected": expected, "actual": actual})

    ok = not mismatched and not missing
    return VerifyResult(
        job_slug=doc.get("job_slug", ""),
        checked=checked,
        ok=ok,
        mismatched=mismatched,
        missing=missing,
        path=str(manifest_path),
    )


register(
    Verb(
        id="export.run",
        group="export",
        fn=run,
        args=(
            ArgSpec(
                "images",
                "list[str]",
                required=True,
                positional=True,
                help="one or more paths or image ids",
            ),
            ArgSpec(
                "preset",
                "str",
                required=True,
                choices=tuple(PRESETS),
                help=(
                    "preview (1024px PNG), client-proof (2048px sRGB JPEG), "
                    "client-final (full-size sRGB JPEG) or print-tiff (full-size "
                    "16-bit AdobeRGB TIFF)"
                ),
            ),
            ArgSpec("out_dir", "str", required=True, help="destination directory"),
            ArgSpec("dry_run", "bool", default=False, help="plan only"),
            ArgSpec(
                "confirm",
                "bool",
                default=False,
                help="required for more than one image, or to overwrite files",
            ),
        ),
        writes=True,
        gated=True,
        summary="Export images through a preset into a directory.",
    )
)

register(
    Verb(
        id="export.release",
        group="export",
        fn=release,
        args=(
            ArgSpec("job_slug", "str", required=True, positional=True, help="job slug to release"),
            ArgSpec(
                "preset",
                "str",
                required=True,
                choices=tuple(PRESETS),
                help=(
                    "preview (1024px PNG, by rating), client-proof (2048px sRGB "
                    "JPEG, by rating), client-final (full-size sRGB JPEG, by "
                    "colour label) or print-tiff (full-size 16-bit AdobeRGB TIFF, "
                    "by colour label)"
                ),
            ),
            ArgSpec("date", "str", help="disambiguate when the slug matches more than one job"),
            ArgSpec(
                "min_rating",
                "int",
                help="override export.min_rating for rating-based presets",
            ),
            ArgSpec(
                "images",
                "list[str]",
                help="explicit image list, overriding the rating/label selection rule",
            ),
            ArgSpec(
                "style",
                "str",
                help="apply this style at render time only; nothing in the library changes",
            ),
            ArgSpec(
                "overwrite_style",
                "bool",
                default=False,
                help="pass --style-overwrite to darktable-cli (render-only)",
            ),
            ArgSpec("dry_run", "bool", default=False, help="plan only"),
            ArgSpec(
                "confirm",
                "bool",
                default=False,
                help="required to overwrite an existing preset output",
            ),
        ),
        writes=True,
        gated=True,
        summary="Export a job's selected frames through a preset, with a verifiable manifest.",
    )
)

register(
    Verb(
        id="export.verify",
        group="export",
        fn=verify,
        args=(
            ArgSpec(
                "manifest",
                "str",
                required=True,
                positional=True,
                help="path to a manifest-<preset>-<ts>.json written by export.release",
            ),
            ArgSpec(
                "root",
                "str",
                help=(
                    "directory files[].output resolves against; default is the "
                    "manifest's own <job>/.scouter/ parent. Pass this to verify a "
                    "manifest shipped beside its exports/ folder instead"
                ),
            ),
        ),
        writes=False,
        gated=False,
        summary=(
            "Re-hash a release manifest's outputs against it (manifest_unauthenticated "
            "if its .sha256 sidecar is missing)."
        ),
    )
)
