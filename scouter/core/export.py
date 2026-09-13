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
from scouter.core import dtbridge, scout, xmp
from scouter.errors import ChecksumMismatch, UsageError
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

#: Streaming chunk size for output-file hashing (matches library.py's `CHUNK`).
_CHUNK = 1 << 20


def preset_args(
    spec: dict, *, style: str | None = None, overwrite_style: bool = False
) -> list[str]:
    """Every darktable-cli flag a preset needs, built in ONE place.

    --core must come last: everything after it is darktable's own argument
    list, so --conf entries follow it and nothing of ours may.
    """
    args: list[str] = []
    if spec.get("icc"):
        args += ["--icc-type", spec["icc"]]
    if style:
        args += ["--style", style]
        if overwrite_style:
            args += ["--style-overwrite"]
    conf: list[str] = []
    if "quality" in spec:
        conf += ["--conf", f"plugins/imageio/format/jpeg/quality={spec['quality']}"]
    if "bpp" in spec:
        conf += [
            "--conf",
            f"plugins/imageio/format/tiff/bpp={spec['bpp']}",
            "--conf",
            "plugins/imageio/format/tiff/compress=5",
        ]
    return args + (["--core", *conf] if conf else [])


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


def _write_atomic(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _now_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _select_by_label(candidates: list[dict], label: str, warnings: list[str]) -> list[dict]:
    """Candidates carrying `label` as a colour label, per one batched bridge call.

    Read-only, like `image.lookup` (controller ruling R6: "both read through
    the bridge"; ruling R12, fix round 0: `image.color_labels` now exists in
    the Lua plugin, reading darktable's own `red`/`yellow`/`green`/`blue`/
    `purple` boolean fields off `dt.database[id]`). Every candidate is
    resolved to an image id first (`dtbridge.resolve_image`, itself
    read-only) and looked up in ONE call, not one per frame. An id the
    bridge did not know about (`missing`) is a warning naming it, not a
    silent drop and not a refusal -- everything else that DOES resolve is
    still trusted.
    """
    by_image_id = {}
    for c in candidates:
        resolved = dtbridge.resolve_image(c["path"])
        by_image_id[resolved.image_id] = c
    result = dtbridge.call("image.color_labels", {"image_ids": list(by_image_id)})
    missing = result.get("missing") or []
    if missing:
        warnings.append(
            f"colour label lookup missed {len(missing)} image id(s) not in darktable's "
            f"library: {missing}"
        )
    selected = []
    for entry in result.get("per_image") or []:
        c = by_image_id.get(int(entry["image_id"]))
        if c is not None and label in (entry.get("labels") or []):
            selected.append(c)
    return selected


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
    resolved = [dtbridge.resolve_image(image) for image in images]

    planned: list[tuple[object, Path]] = []
    for item in resolved:
        planned.append((item, destination / f"{Path(item.path).stem}.{spec['ext']}"))
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
    elif preset in FINAL_PRESETS:
        selected = _select_by_label(candidates, label, warnings) if candidates else []
        rule = f"color_label={label}"
    else:
        selected = [c for c in candidates if c["rating"] >= resolved_min_rating]
        rule = f"rating>={resolved_min_rating}"

    if not selected:
        histogram: dict[str, int] = {}
        for c in candidates:
            key = str(c["rating"])
            histogram[key] = histogram.get(key, 0) + 1
        raise UsageError(
            f"no frames matched {rule} (0 of {len(candidates)}); ratings histogram "
            f"(rating -> count of frames): {histogram}",
            detail={
                "rule": rule,
                "min_rating": resolved_min_rating,
                "histogram": histogram,
                "of": len(candidates),
            },
        )

    out_dir = config.exports_dir(job_path, preset)
    planned = [(c, out_dir / f"{Path(c['path']).stem}.{spec['ext']}") for c in selected]
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
    if dry_run or (existing and not confirm):
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
                f"{len(existing)} output file(s) already exist; re-run with --yes / confirm=true"
                if existing and not confirm
                else "dry run: nothing was exported"
            ),
            warnings=warnings,
            path=str(job_path),
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    extra_args = preset_args(spec, style=style, overwrite_style=overwrite_style)

    try:
        darktable_version = scout.CLIWrapper(darktable_cli_path=scout.cli_binary()).get_version()
    except Exception:  # doctor-shaped info only, never a reason to refuse a release
        darktable_version = None

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
    manifest_path = scouter_d / f"manifest-{preset}-{ts}.json"
    manifest_text = json.dumps(manifest_doc, indent=2)
    _write_atomic(manifest_path, manifest_text)
    manifest_sha = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
    sha_path = manifest_path.with_name(f"{manifest_path.name}.sha256")
    _write_atomic(sha_path, f"{manifest_sha}  {manifest_path.name}\n")

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
def verify(manifest: str, *, job: str | None = None) -> VerifyResult:
    """Re-hash every output in a release manifest against it.

    First checks the manifest itself against its own `manifest-*.sha256`
    sidecar (`export.release` writes one alongside every manifest) -- a
    tampered or truncated manifest must not be trusted to check the outputs
    it claims to describe. Returns `library.verify`'s shape.
    """
    manifest_path = Path(manifest)
    if not manifest_path.is_file():
        raise UsageError(f"no manifest at {manifest_path}", detail={"manifest": str(manifest_path)})
    manifest_text = manifest_path.read_text(encoding="utf-8")
    actual_manifest_sha = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()

    sha_path = manifest_path.with_name(f"{manifest_path.name}.sha256")
    if sha_path.is_file():
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
    base = manifest_path.parent.parent  # <job>/.scouter/manifest-*.json -> <job>
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
                help="client-final (full-size JPEG) or preview (1024 px PNG)",
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
                help="client-proof (2048px), client-final, print-tiff, or preview",
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
        ),
        writes=False,
        gated=False,
        summary="Re-hash a release manifest's outputs against it, and check its own .sha256.",
    )
)
