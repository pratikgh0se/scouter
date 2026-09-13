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

`style.apply_set` (task 8) is the batched sibling: it never touches the
darkroom view at all. The Lua handler for `style.apply` looks images up in
`dt.database` and calls `image:apply_style` directly -- it does not read
`view.current` or care whether darktable is showing the lighttable or the
darkroom, on the target image or any other. So there is no "refuse if the
darkroom is open on one of the target images" guard here: stated honestly,
that guard would be theatre, because the Lua side applies styles from
either view identically. Ruling 30's job-image guard (a darkroom WRITE
refusing when `darkroom.current` differs from the job's last opened image)
does not apply either -- a style application is not a darkroom write, it
is a library-level history append.
"""

from __future__ import annotations

import json

from scouter import metrics, paths
from scouter.core import dtbridge, dtstyle
from scouter.core import xmp as xmp_module
from scouter.errors import (
    BridgeTimeout,
    NotInDarkroom,
    ScouterError,
    StylePartial,
    StyleWriteFailed,
    UsageError,
)
from scouter.paths import current_job
from scouter.registry import ArgSpec, Verb, register
from scouter.types import ResolvedImage, StyleResult, StyleSetResult

MODES = ("append", "overwrite")

#: `style.apply_set` batches ids through the bridge at this size (task 8);
#: the batch loop stops after this many CONSECUTIVE batch-level failures
#: (a `BridgeTimeout` on the whole batch, e.g. darktable stopped responding)
#: so a stalled worker does not spin through every remaining batch one at a
#: time, each waiting out the full `style.apply` timeout before giving up.
DEFAULT_BATCH_SIZE = 25
MAX_CONSECUTIVE_BATCH_FAILURES = 2


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


def _images_from_job_slug(job_slug: str) -> list[str]:
    """Every raw's absolute path from `<job>/.scouter/import.json`'s `files[].dest`.

    Reads the manifest directly rather than through `scouter.core.library`
    (task 3): that module is not on this branch, and this mirrors the same
    shortcut task 13's `backup.verify` took for its `sampled_raws` check --
    the format matches design 4.4/6's documented `import.json` shape,
    `files: [{"dest": "raw/<name>", ...}, ...]`, relative to the job dir.
    """
    from scouter import config as config_module

    cfg = config_module.load()
    job_path = config_module.find_job(cfg, job_slug)
    manifest_path = config_module.scouter_dir(job_path) / "import.json"
    if not manifest_path.is_file():
        raise UsageError(
            f"no import.json for job {job_slug!r} at {manifest_path}: run "
            "`scouter library import` first",
            detail={"job_slug": job_slug, "path": str(manifest_path)},
        )
    manifest = json.loads(manifest_path.read_text())
    return [str(job_path / f["dest"]) for f in manifest.get("files", [])]


def _resolve_set(
    job_slug: str | None, images: list[str | int] | None, exclude: list[str | int] | None
) -> list[ResolvedImage]:
    """`--images`, else every file in the job's `import.json`, minus `--exclude`.

    Every candidate -- and every `--exclude` entry -- is resolved through
    `image.lookup` (`dtbridge.resolve_image`, a pure read), same as a single
    `style.apply`; `--exclude` is matched by resolved `image_id` rather than
    by raw string/path equality, so a path and the id it resolves to exclude
    the same frame either way.
    """
    if images:
        raw = list(images)
    elif job_slug:
        raw = _images_from_job_slug(job_slug)
    else:
        raw = []
    if not raw:
        raise UsageError(
            "no images to apply the style to: pass --images, or --job-slug "
            "naming a job whose import.json lists files",
            detail={"job_slug": job_slug, "images": images},
        )
    resolved = [dtbridge.resolve_image(item) for item in raw]
    if exclude:
        excluded_ids = {dtbridge.resolve_image(item).image_id for item in exclude}
        resolved = [r for r in resolved if r.image_id not in excluded_ids]
        if not resolved:
            raise UsageError(
                "every candidate image was excluded: nothing to apply the style to",
                detail={"job_slug": job_slug, "excluded": sorted(excluded_ids)},
            )
    return resolved


@metrics.instrumented("style.apply_set")
def apply_set(
    name: str,
    *,
    job_slug: str | None = None,
    images: list[str | int] | None = None,
    exclude: list[str | int] | None = None,
    mode: str = "append",
    batch: int = DEFAULT_BATCH_SIZE,
    confirm: bool = False,
    dry_run: bool = False,
    job: str | None = None,
) -> StyleSetResult:
    """Apply one style to a whole set, in batches, keeping per-image results.

    A partial set is a RESULT, not an exception: 280 of 300 applied is
    information the caller needs, and phase 1's single-image `apply` raises
    StylePartial, which would discard the payload. So `outcome="partial"` when
    applied > 0 and failed > 0 (exit 9, the same code as style_partial, so one
    shell branch covers "some of it worked"), and StylePartial only when
    NOTHING applied.

    Always gated: a set apply is inherently a bulk write (there is no
    "safe by default" single-image case here, unlike `style.apply`), so
    `confirm` is required regardless of how many images the set resolves to
    -- the same reasoning `library.import` uses ("an import is always > 1
    file, so it is always gated", design 3.1). Dry run, and a real call
    without confirm, both resolve the full set and check `name` against
    `list_styles` before returning the plan -- a typo costs no round trips
    -- but make no `style.apply` call and touch nothing.

    Never touches the darkroom view (see the module docstring): the Lua
    `style.apply` handler applies from `dt.database` directly, so there is
    no lighttable/darkroom guard to enforce here, stated honestly rather
    than pretended.
    """
    job = job or current_job()
    if batch <= 0:
        raise UsageError(
            f"batch must be a positive integer, got {batch!r}", detail={"batch": batch}
        )
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    if mode == "overwrite":
        raise UnsupportedMode(
            "mode=overwrite cannot be honoured for a set either: darktable's Lua "
            "API only appends a style to an image's history (image:apply_style). "
            "The one place overwrite exists at all is a render-only shortcut: "
            "`export.release --overwrite-style`",
            detail={"name": name, "mode": mode, "modes_available": ["append"]},
        )

    from scouter import config as config_module

    cfg = config_module.load()
    resolved = _resolve_set(job_slug, images, exclude)
    plan = [
        {
            "action": "style.apply_set",
            "image_id": r.image_id,
            "path": r.path,
            "name": name,
            "mode": mode,
        }
        for r in resolved
    ]

    if dry_run or not confirm:
        existing = _existing_styles()
        if name not in existing:
            raise UsageError(
                f"no style named {name!r} exists", detail={"name": name, "existing": existing}
            )
        return StyleSetResult(
            outcome="dry_run",
            name=name,
            mode=mode,
            requested=len(resolved),
            plan=plan,
            reason=(
                "dry run: nothing was applied"
                if dry_run
                else f"applying a style to {len(resolved)} images requires --yes / confirm=true"
            ),
        )

    id_to_path = {r.image_id: r.path for r in resolved}
    ids = [r.image_id for r in resolved]
    batches = [ids[i : i + batch] for i in range(0, len(ids), batch)]

    applied_ids: list[int] = []
    per_image: list[dict] = []
    missed_ids: list[int] = []
    consecutive_failures = 0
    batches_run = 0
    warnings: list[str] = []

    for batch_ids in batches:
        batches_run += 1
        try:
            result = dtbridge.call(
                "style.apply", {"image_ids": batch_ids, "name": name, "mode": mode}
            )
        except BridgeTimeout as e:
            # The whole batch failed as one call -- every id in it is a miss,
            # and a stalled worker on frame 140 must not lose frames 1-139:
            # earlier batches' successes are already recorded in per_image.
            consecutive_failures += 1
            for iid in batch_ids:
                missed_ids.append(iid)
                per_image.append(
                    {
                        "image_id": iid,
                        "path": id_to_path.get(iid),
                        "applied": False,
                        "error": str(e),
                    }
                )
            if consecutive_failures >= MAX_CONSECUTIVE_BATCH_FAILURES:
                warnings.append(
                    f"stopped after {consecutive_failures} consecutive batch timeouts "
                    f"({batches_run} of {len(batches)} batches attempted)"
                )
                break
            continue
        consecutive_failures = 0
        batch_missed = {int(i) for i in result.get("missed", [])}
        errors = list(result.get("errors", []))
        error_ids = {int(e["image_id"]) for e in errors}
        for iid in batch_ids:
            if iid in batch_missed:
                missed_ids.append(iid)
                per_image.append(
                    {
                        "image_id": iid,
                        "path": id_to_path.get(iid),
                        "applied": False,
                        "error": "not found in darktable's library",
                    }
                )
            elif iid in error_ids:
                err = next(e for e in errors if int(e["image_id"]) == iid)
                missed_ids.append(iid)
                per_image.append(
                    {
                        "image_id": iid,
                        "path": id_to_path.get(iid),
                        "applied": False,
                        "error": err.get("error"),
                    }
                )
            else:
                applied_ids.append(iid)
                per_image.append({"image_id": iid, "path": id_to_path.get(iid), "applied": True})

    requested = len(resolved)
    applied = len(applied_ids)
    failed = len(missed_ids)
    never_attempted = requested - applied - failed
    if never_attempted > 0:
        warnings.append(f"{never_attempted} images were never attempted")

    if job_slug:
        try:
            from scouter.core import vault as vault_module

            rows = [
                {
                    "image": pi["path"] or str(pi["image_id"]),
                    "result": "applied" if pi["applied"] else "failed",
                }
                for pi in per_image
            ]
            vault_module.append_stage(
                cfg, job_slug, "Style applied", rows, "style applied to the set"
            )
        except Exception as e:
            # `scouter/core/vault.py` is task 4's and does not exist yet on
            # this branch (always ImportError today); any other vault
            # failure degrades the same way -- a warning, never a rollback
            # of work that already landed in darktable.
            warnings.append(f"vault stage not recorded: {e}")

    if applied == 0:
        raise StylePartial(
            f"style {name!r} applied to 0 of {requested} images",
            detail={"per_image": per_image, "missed": missed_ids, "name": name},
        )

    outcome = "partial" if failed > 0 else "ok"
    return StyleSetResult(
        outcome=outcome,
        name=name,
        mode=mode,
        requested=requested,
        applied=applied,
        failed=failed,
        batches=batches_run,
        missed=sorted(missed_ids),
        per_image=per_image,
        warnings=warnings,
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
    # Always gated (task 8): a set apply is inherently a bulk write, so
    # unlike `style.apply` there is always something to confirm.
    Verb(
        id="style.apply_set",
        group="style",
        fn=apply_set,
        args=(
            ArgSpec("name", "str", required=True, positional=True, help="style name"),
            ArgSpec(
                "job_slug",
                "str",
                default=None,
                help="job whose import.json lists the set (used when --images is not given)",
            ),
            ArgSpec(
                "images",
                "list[str]",
                default=None,
                help="paths or image ids; defaults to every file in --job-slug's import.json",
            ),
            ArgSpec("exclude", "list[str]", default=None, help="paths or image ids to leave out"),
            ArgSpec(
                "mode",
                "str",
                default="append",
                choices=MODES,
                help="append adds to history; overwrite is refused "
                "(unsupported_mode) -- darktable cannot replace history",
            ),
            ArgSpec(
                "batch",
                "int",
                default=DEFAULT_BATCH_SIZE,
                help="how many image ids per bridge call",
            ),
            ArgSpec(
                "confirm",
                "bool",
                default=False,
                help="required: applying to a set is always gated",
            ),
            ArgSpec("dry_run", "bool", default=False, help="plan only"),
        ),
        writes=True,
        gated=True,
        summary="Apply one style to a whole set, in batches, keeping per-image results.",
    ),
):
    register(verb)
