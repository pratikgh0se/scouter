"""Styles: extract the hero's recipe, apply it back. One image in phase 1.

Two honest limits of Lua API 9.6.0, surfaced in the result rather than hidden:
  * dt.styles.create has no module filter, so `modules` is recorded as
    requested and `modules_honoured` is false. Filtering by writing a .dtstyle
    from the XMP and importing it is phase 2.
  * image:apply_style always APPENDS. `mode="overwrite"` is refused with the
    reason, because silently appending when the caller asked to overwrite is
    exactly the phantom success the honesty pass removed.
"""

from __future__ import annotations

from scouter import metrics
from scouter.core import dtbridge
from scouter.errors import StylePartial
from scouter.paths import current_job
from scouter.registry import ArgSpec, Verb, register
from scouter.types import StyleResult

MODES = ("append", "overwrite")


def _existing_styles() -> list[str]:
    result = dtbridge.call("list_styles", {})
    return [str(s.get("name")) for s in result.get("styles", [])]


@metrics.instrumented("style.extract")
def extract(image: str | int, name: str, modules: list[str], *, description: str = "",
            confirm: bool = False, dry_run: bool = False,
            job: str | None = None) -> StyleResult:
    """Save the darkroom image's current history as a darktable style."""
    job = job or current_job()
    modules = list(modules)
    resolved = dtbridge.resolve_image(image)
    existing = _existing_styles()
    replaces = name in existing
    plan = [{"action": "style.extract", "image_id": resolved.image_id, "name": name,
             "modules_requested": modules, "replaces_existing": replaces}]
    if dry_run or (replaces and not confirm):
        return StyleResult(
            outcome="dry_run", image_id=resolved.image_id, path=resolved.path,
            name=name, modules_requested=modules, plan=plan,
            reason=(
                f"a style named {name!r} already exists and would be replaced; "
                "re-run with --yes / confirm=true"
                if replaces and not confirm
                else "dry run: nothing was written to darktable"
            ),
        )
    dtbridge.require_darkroom()
    result = dtbridge.call(
        "style.create_from_current",
        {"name": name, "modules": modules, "description": description},
    )
    warnings: list[str] = []
    if result.get("note"):
        warnings.append(str(result["note"]))
    return StyleResult(
        image_id=resolved.image_id, path=resolved.path, name=name,
        modules_requested=modules,
        modules_honoured=bool(result.get("modules_honoured")),
        created=bool(result.get("created")), note=result.get("note"),
        warnings=warnings,
    )


@metrics.instrumented("style.apply")
def apply(image: str | int, name: str, *, mode: str = "append", confirm: bool = False,
          dry_run: bool = False, job: str | None = None) -> StyleResult:
    """Apply a style to ONE image (a set is phase 2)."""
    job = job or current_job()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    resolved = dtbridge.resolve_image(image)
    gated = mode == "overwrite"
    plan = [{"action": "style.apply", "image_id": resolved.image_id,
             "path": resolved.path, "name": name, "mode": mode}]
    if dry_run or (gated and not confirm):
        return StyleResult(
            outcome="dry_run", image_id=resolved.image_id, path=resolved.path,
            name=name, mode=mode, plan=plan, applied=0,
            reason=(
                "mode=overwrite replaces this image's history; re-run with "
                "--yes / confirm=true"
                if gated and not confirm
                else "dry run: nothing was applied"
            ),
        )
    result = dtbridge.call(
        "style.apply",
        {"image_ids": [resolved.image_id], "name": name, "mode": mode},
    )
    applied = int(result.get("applied", 0))
    missed = [int(i) for i in result.get("missed", [])]
    errors = list(result.get("errors", []))
    if applied != 1 or missed or errors:
        raise StylePartial(
            f"style {name!r} applied to {applied} of 1 image",
            detail={"per_image": errors, "missed": missed, "name": name},
        )
    return StyleResult(
        image_id=resolved.image_id, path=resolved.path, name=name, mode=mode,
        applied=applied, per_image=errors,
    )


for verb in (
    Verb(id="style.extract", group="style", fn=extract,
         args=(ArgSpec("image", "image", required=True, positional=True,
                       help="path or image id of the hero frame"),
               ArgSpec("name", "str", required=True, help="style name"),
               ArgSpec("modules", "list[str]", required=True,
                       help="modules to keep (recorded; Lua API 9.6.0 cannot filter)"),
               ArgSpec("description", "str", default="", help="style description"),
               ArgSpec("dry_run", "bool", default=False, help="plan only"),
               ArgSpec("confirm", "bool", default=False,
                       help="required when a style of this name already exists")),
         writes=True, gated=True,
         summary="Save the darkroom image's history as a darktable style."),
    Verb(id="style.apply", group="style", fn=apply,
         args=(ArgSpec("image", "image", required=True, positional=True,
                       help="path or image id"),
               ArgSpec("name", "str", required=True, help="style name"),
               ArgSpec("mode", "str", default="append", choices=MODES,
                       help="append adds to history; overwrite replaces it"),
               ArgSpec("dry_run", "bool", default=False, help="plan only"),
               ArgSpec("confirm", "bool", default=False,
                       help="required for mode=overwrite (a history overwrite)")),
         writes=True, gated=True,
         summary="Apply a style to one image (a set is phase 2)."),
):
    register(verb)
