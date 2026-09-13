"""Export presets. Two in phase 1: client-final and preview."""

from __future__ import annotations

from pathlib import Path

from scouter import metrics
from scouter.core import dtbridge, scout
from scouter.paths import current_job
from scouter.registry import ArgSpec, Verb, register
from scouter.types import ExportedFile, ExportResult

#: preset -> (extension, longest edge in px or 0 for full size, quality)
PRESETS: dict[str, dict] = {
    "client-final": {"ext": "jpg", "px": 0, "quality": 95,
                     "note": "full-size sRGB JPEG q95"},
    "preview": {"ext": "png", "px": 1024, "quality": 95,
                "note": "1024 px PNG for review"},
}


@metrics.instrumented("export.run")
def run(images: list[str | int], preset: str, out_dir: str, *, confirm: bool = False,
        dry_run: bool = False, job: str | None = None) -> ExportResult:
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
        {"action": "export.run", "image_id": item.image_id, "input": item.path,
         "output": str(out), "exists": out.exists(), "preset": preset}
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
            outcome="dry_run", preset=preset, out_dir=str(destination), plan=plan,
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
        except Exception as e:                     # one bad file must not lose the rest
            files.append(ExportedFile(input=item.path, output=None, ok=False, bytes=0,
                                      error=str(e)))
            continue
        files.append(ExportedFile(input=item.path, output=str(out), ok=True,
                                  bytes=out.stat().st_size, error=None))
    exported = sum(1 for f in files if f.ok)
    return ExportResult(
        preset=preset, out_dir=str(destination), files=files, exported=exported,
        failed=len(files) - exported,
        image_id=resolved[0].image_id if resolved else None,
        path=resolved[0].path if resolved else None,
        warnings=[spec["note"]] + (
            [f"{len(files) - exported} file(s) failed; see `files[].error`"]
            if exported != len(files) else []
        ),
    )


register(
    Verb(id="export.run", group="export", fn=run,
         args=(ArgSpec("images", "list[str]", required=True, positional=True,
                       help="one or more paths or image ids"),
               ArgSpec("preset", "str", required=True, choices=tuple(PRESETS),
                       help="client-final (full-size JPEG) or preview (1024 px PNG)"),
               ArgSpec("out_dir", "str", required=True, help="destination directory"),
               ArgSpec("dry_run", "bool", default=False, help="plan only"),
               ArgSpec("confirm", "bool", default=False,
                       help="required for more than one image, or to overwrite files")),
         writes=True, gated=True,
         summary="Export images through a preset into a directory.")
)
