"""Styles: extract the hero's recipe, apply it back.

`style.extract`'s module filter is real (task 7): the default path writes a
`.dtstyle` from the image's own XMP (`scouter.core.dtstyle`) and imports it,
so `modules_honoured` genuinely reports `true`. `dt.styles.create` (phase
1's path, `no_filter=True`) still has no module filter of its own -- kept as
an honest escape hatch, e.g. when a caller wants the whole history on
purpose, surfaced as `modules_honoured=False` rather than hidden.

One honest limit remains unconditional: `image:apply_style` always APPENDS.
`mode="overwrite"` is refused OUTRIGHT -- in this module, with the typed code
`unsupported_mode`, before any bridge call -- because silently appending when
the caller asked to overwrite is exactly the phantom success the honesty
pass removed. It is not a confirmation gate: no `--yes` makes it work, so
there is nothing to confirm (final review, should-fix 7). The bridge refuses
it too, but that refusal arrives as a generic bridge failure after a round
trip.
"""

from __future__ import annotations

from scouter import metrics, paths
from scouter.core import dtbridge, dtstyle
from scouter.core import xmp as xmp_module
from scouter.errors import NotInDarkroom, ScouterError, StylePartial, StyleWriteFailed, UsageError
from scouter.paths import current_job
from scouter.registry import ArgSpec, Verb, register
from scouter.types import ResolvedImage, StyleResult

MODES = ("append", "overwrite")


class UnsupportedMode(ScouterError):
    """`style.apply` was asked to overwrite history, which no API here can do.

    Generic exit code 1, like the module-local refusals in `core.darkroom` and
    `core.scout`: this is a Scouter policy refusal about a capability darktable
    does not expose, not one of spec section 9's darktable failure classes.
    """

    code = "unsupported_mode"
    fix = (
        "use mode=append (the only mode darktable's Lua API implements), or "
        "render with darktable-cli --style-overwrite for an export-only overwrite"
    )


def _existing_styles() -> list[str]:
    result = dtbridge.call("list_styles", {})
    return [str(s.get("name")) for s in result.get("styles", [])]


def _require_darkroom_shows(image_id: int) -> None:
    """Refuse, before ANY `style.create_from_current` call, when the darkroom
    is showing a different image than the one `extract` resolved.

    Final re-review: `style.create_from_current` (the Lua side) builds the
    style from whatever `loaded_darkroom_image()` reports -- the darkroom's
    OWN idea of "current image" -- not from the `image` argument, which
    `extract` resolves by library lookup (`dtbridge.resolve_image`, a pure
    read with no relation to what is on screen). Extracting image A while the
    darkroom shows image B would silently build the style from B and report A
    as the source. `darkroom.current` is a read-only query (no side effects,
    same call `core.darkroom._current_image` uses) so this check costs one
    round trip and moves nothing. Absent (`image_id` missing) means nothing
    is loaded at all -- also a refusal, not a pass.
    """
    current = dtbridge.call("darkroom.current", {})
    loaded = current.get("image_id")
    if loaded is None:
        raise NotInDarkroom(
            f"no image is loaded in the darkroom (view is {current.get('view', 'unknown')})",
            detail={"view": current.get("view")},
        )
    if int(loaded) != int(image_id):
        raise NotInDarkroom(
            f"darkroom shows {int(loaded)}, requested {int(image_id)}",
            fix=(
                "open the intended image first (`scouter darkroom open <image>`), "
                "or extract from the image the darkroom already shows"
            ),
            detail={"loaded_image_id": int(loaded), "requested_image_id": int(image_id)},
        )


def _extract_whole_history(
    resolved: ResolvedImage,
    name: str,
    modules: list[str],
    description: str,
    *,
    extra_warning: str | None,
) -> StyleResult:
    """Phase 1's path: the whole history, honestly labelled `modules_honoured=False`.

    Used when the caller passes `no_filter=True`, or when `.dtstyle`
    filtering itself is unavailable (task 7's host probe found it always is
    on this darktable build -- see `dtstyle` module docstring -- so
    `extra_warning` is only ever set by a future build that regresses that).

    `style.create_from_current` (the Lua side) builds the style from
    whatever `loaded_darkroom_image()` reports -- the darkroom's OWN idea of
    "current image" -- so this path (and only this path) needs the darkroom
    open and showing the resolved image.
    """
    dtbridge.require_darkroom()
    _require_darkroom_shows(resolved.image_id)
    result = dtbridge.call(
        "style.create_from_current",
        {"name": name, "modules": modules, "description": description},
    )
    warnings: list[str] = []
    if result.get("note"):
        warnings.append(str(result["note"]))
    if extra_warning:
        warnings.append(extra_warning)
    return StyleResult(
        image_id=resolved.image_id,
        path=resolved.path,
        name=name,
        modules_requested=modules,
        modules_honoured=bool(result.get("modules_honoured")),
        created=bool(result.get("created")),
        note=result.get("note"),
        source=result.get("source"),
        warnings=warnings,
    )


@metrics.instrumented("style.extract")
def extract(
    image: str | int,
    name: str,
    modules: list[str],
    *,
    description: str = "",
    confirm: bool = False,
    dry_run: bool = False,
    no_filter: bool = False,
    job: str | None = None,
) -> StyleResult:
    """Save the image's history as a darktable style, filtered to `modules`.

    The default (filtered) path reads the XMP of the image `extract` already
    resolved by library lookup, writes `<cache>/styles/<name>.dtstyle`
    itself (`scouter.core.dtstyle`, pinned against a real darktable file --
    see that module's docstring), and imports it through the new bridge verb
    `style.import_file` -- so `modules_honoured` is genuinely `True`. This
    path never touches the darkroom: no `dtbridge.require_darkroom()`, no
    `_require_darkroom_shows` -- it reads a file, not the live GUI, so a
    style can be extracted from the lighttable.

    `no_filter=True` (the escape hatch named in `StyleWriteFailed.fix`) takes
    phase 1's whole-history path instead (`style.create_from_current`),
    which DOES require the darkroom and keeps `_require_darkroom_shows`: that
    Lua call builds the style from whatever the darkroom currently shows, not
    from `image`. The gate is unchanged either way: `confirm` is required
    only when a style of that name already exists.
    """
    job = job or current_job()
    modules = list(modules)
    resolved = dtbridge.resolve_image(image)
    existing = _existing_styles()
    replaces = name in existing
    plan = [
        {
            "action": "style.extract",
            "image_id": resolved.image_id,
            "name": name,
            "modules_requested": modules,
            "replaces_existing": replaces,
            "no_filter": no_filter,
        }
    ]
    if dry_run or (replaces and not confirm):
        return StyleResult(
            outcome="dry_run",
            image_id=resolved.image_id,
            path=resolved.path,
            name=name,
            modules_requested=modules,
            plan=plan,
            reason=(
                f"a style named {name!r} already exists and would be replaced; "
                "re-run with --yes / confirm=true"
                if replaces and not confirm
                else "dry run: nothing was written to darktable"
            ),
        )

    if no_filter:
        return _extract_whole_history(resolved, name, modules, description, extra_warning=None)

    from scouter.config import load as _load_config

    cfg = _load_config()
    never_include = set(cfg.style.never_include)
    forbidden = [m for m in modules if m in never_include]
    if forbidden:
        raise UsageError(
            f"{sorted(forbidden)} may never ride a style: per-image geometry and "
            "repairs (cfg.style.never_include) are excluded on purpose. Remove "
            "them from --modules, or use --no-filter for the whole-history style",
            detail={"forbidden": sorted(forbidden), "never_include": sorted(never_include)},
        )

    text, _mtime = xmp_module.read_sidecar(resolved.path)
    parsed = xmp_module.parse(text)
    present_ops = {entry.operation for entry in xmp_module.active_entries(parsed)}
    modules_present = [m for m in modules if m in present_ops]
    modules_missing = [m for m in modules if m not in present_ops]
    if not modules_present:
        raise UsageError(
            "none of the requested modules are in the image's active history: "
            "an empty style is not a style",
            detail={"modules_requested": modules, "modules_missing": modules_missing},
        )

    style_text = dtstyle.build(parsed, name, description, modules_present)
    dtstyle_path = dtstyle.write(paths.styles_dir() / f"{name}.dtstyle", style_text)

    try:
        result = dtbridge.call(
            "style.import_file",
            {"path": str(dtstyle_path), "name": name, "overwrite": confirm},
        )
    except ScouterError as e:
        raise StyleWriteFailed(
            f"darktable refused the extracted style {name!r}: {e}",
            detail={"path": str(dtstyle_path)},
        ) from e

    written_ops = set(dtstyle.read_operations(dtstyle_path))
    expected_ops = set(modules_present)
    if written_ops != expected_ops:
        raise StyleWriteFailed(
            f"the written style {name!r} carries {sorted(written_ops)}, not the "
            f"requested-and-present {sorted(expected_ops)}",
            detail={
                "path": str(dtstyle_path),
                "written": sorted(written_ops),
                "expected": sorted(expected_ops),
            },
        )

    warnings: list[str] = []
    if modules_missing:
        warnings.append(f"modules not in the active history, skipped: {modules_missing}")

    return StyleResult(
        image_id=resolved.image_id,
        path=resolved.path,
        name=name,
        modules_requested=modules,
        modules_honoured=True,
        created=bool(result.get("imported")),
        dtstyle_path=str(dtstyle_path),
        operations_included=modules_present,
        modules_missing=modules_missing,
        source="xmp",
        warnings=warnings,
    )


@metrics.instrumented("style.apply")
def apply(
    image: str | int,
    name: str,
    *,
    mode: str = "append",
    dry_run: bool = False,
    job: str | None = None,
) -> StyleResult:
    """Apply a style to ONE image (a set is phase 2). `append` is the only mode."""
    job = job or current_job()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    if mode == "overwrite":
        # Before ANY bridge call, and before resolving the image: there is no
        # confirmation that makes this work, so asking for one (which this verb
        # used to do) implied a capability that does not exist.
        raise UnsupportedMode(
            "mode=overwrite cannot be honoured: darktable's Lua API only appends "
            "a style to an image's history (image:apply_style), so nothing here "
            "can replace it",
            detail={"name": name, "mode": mode, "modes_available": ["append"]},
        )
    resolved = dtbridge.resolve_image(image)
    plan = [
        {
            "action": "style.apply",
            "image_id": resolved.image_id,
            "path": resolved.path,
            "name": name,
            "mode": mode,
        }
    ]
    if dry_run:
        return StyleResult(
            outcome="dry_run",
            image_id=resolved.image_id,
            path=resolved.path,
            name=name,
            mode=mode,
            plan=plan,
            applied=0,
            reason="dry run: nothing was applied",
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
        image_id=resolved.image_id,
        path=resolved.path,
        name=name,
        mode=mode,
        applied=applied,
        per_image=errors,
    )


for verb in (
    Verb(
        id="style.extract",
        group="style",
        fn=extract,
        args=(
            ArgSpec(
                "image",
                "image",
                required=True,
                positional=True,
                help="path or image id of the hero frame",
            ),
            ArgSpec("name", "str", required=True, help="style name"),
            ArgSpec(
                "modules",
                "list[str]",
                required=True,
                help="modules to keep -- filtered for real by writing a .dtstyle from the XMP",
            ),
            ArgSpec("description", "str", default="", help="style description"),
            ArgSpec("dry_run", "bool", default=False, help="plan only"),
            ArgSpec(
                "confirm",
                "bool",
                default=False,
                help="required when a style of this name already exists",
            ),
            ArgSpec(
                "no_filter",
                "bool",
                default=False,
                help="skip the .dtstyle filter and take the whole history "
                "(phase 1's path; requires the darkroom to show this image)",
            ),
        ),
        writes=True,
        gated=True,
        summary="Save the image's history as a darktable style, filtered to --modules.",
    ),
    # Not `gated`: its only would-be gate was mode=overwrite, which is refused
    # outright rather than confirmed (see `UnsupportedMode`), so there is no
    # confirm argument to offer -- offering one would advertise a capability
    # that does not exist.
    Verb(
        id="style.apply",
        group="style",
        fn=apply,
        args=(
            ArgSpec("image", "image", required=True, positional=True, help="path or image id"),
            ArgSpec("name", "str", required=True, help="style name"),
            ArgSpec(
                "mode",
                "str",
                default="append",
                choices=MODES,
                help="append adds to history; overwrite is refused "
                "(unsupported_mode) -- darktable cannot replace history",
            ),
            ArgSpec("dry_run", "bool", default=False, help="plan only"),
        ),
        writes=True,
        gated=False,
        summary="Apply a style to one image, appending it to the history.",
    ),
):
    register(verb)
