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

Fix round 1 (Opus quality review): `image:apply_style` always APPENDS, for
a set the same as for one image -- re-running `apply_set` on a set that
already carries the style stacks a SECOND copy of every operation the
style contains, it does not replace or dedupe the first. `apply_set` now
reads each target's sidecar locally before applying (`_already_has_style`,
cheap -- one `image.lookup` per target plus a local file read, no render
or write) and skips a target that already looks like it carries the
style, `force=True` overrides. See `apply_set`'s own docstring for the
error-handling and idempotency changes in full; `StyleSetResult`'s fixed
fields (`scouter/types.py`, out of this fix round's file scope) have no
room for new top-level lists, so `status` per image (`"applied"`,
`"failed"`, `"unknown"`, `"already_has_style"`) lives in `per_image` rows,
with the aggregate counts and id lists also named in `warnings`.
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
) -> tuple[list[ResolvedImage], list[dict]]:
    """`--images`, else every file in the job's `import.json`, minus `--exclude`.

    Returns `(resolved, unresolved)`. `unresolved` is one `per_image`-shaped
    failure row (`status="failed"`, `image_id=None`) per candidate
    `image.lookup` could not find -- fix round 1: a stale or renamed raw
    inside a 300-file `import.json` must not kill the whole set, or the dry
    run that previews it. `dtbridge.resolve_image` raises a `ScouterError`
    (`ImageNotFound`, a `usage`-coded one) for that case; it is caught here,
    not left to propagate, and every OTHER candidate is still resolved.

    Every resolvable candidate -- and every `--exclude` entry -- goes
    through `image.lookup` (`dtbridge.resolve_image`, a pure read), same as
    a single `style.apply`; `--exclude` matches by resolved `image_id`, so a
    path and the id it resolves to exclude the same frame either way. An
    `--exclude` entry that does not itself resolve excludes nothing -- a
    typo there costs a no-op, not a crash.

    `images=[]` (an EXPLICIT empty list, not simply omitted) is refused as a
    caller mistake rather than silently falling back to `--job-slug`'s
    import.json (fix round 1 nit): only `images is None` means "not given".
    When both `--images` and `--job-slug` are given, every path-form entry
    in `--images` must already be one of the job's own raws (fix round 1
    nit) -- an id-form entry is not checked against the manifest, since
    doing so would mean resolving every one of the job's own raws just to
    compare ids, defeating the point of staying bridge-call-free until the
    real resolution pass below.
    """
    if images is not None:
        raw = list(images)
        if not raw:
            raise UsageError(
                "--images was empty: pass at least one image, or omit --images "
                "to use every file in --job-slug's import.json",
                detail={"job_slug": job_slug, "images": images},
            )
        if job_slug:
            job_files = set(_images_from_job_slug(job_slug))
            outside = [
                item
                for item in raw
                if isinstance(item, str) and not item.isdigit() and item not in job_files
            ]
            if outside:
                raise UsageError(
                    f"{len(outside)} of --images are not in job {job_slug!r}'s "
                    f"import.json: {outside[:5]}{' ...' if len(outside) > 5 else ''}",
                    detail={"job_slug": job_slug, "outside": outside},
                )
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

    resolved: list[ResolvedImage] = []
    unresolved: list[dict] = []
    for item in raw:
        try:
            resolved.append(dtbridge.resolve_image(item))
        except ScouterError as e:
            unresolved.append(
                {"image_id": None, "path": str(item), "status": "failed", "error": str(e)}
            )

    if exclude:
        excluded_ids: set[int] = set()
        for item in exclude:
            try:
                excluded_ids.add(dtbridge.resolve_image(item).image_id)
            except ScouterError:
                continue
        resolved = [r for r in resolved if r.image_id not in excluded_ids]
        if not resolved and not unresolved:
            raise UsageError(
                "every candidate image was excluded: nothing to apply the style to",
                detail={"job_slug": job_slug, "excluded": sorted(excluded_ids)},
            )

    return resolved, unresolved


def _already_has_style(resolved: ResolvedImage, name: str, job: str) -> bool:
    """Cheap, local-mostly check: does this image's sidecar already carry
    an instance darktable tagged with this style's name?

    Heuristic, not a guarantee -- fix round 1's idempotency guard.
    `image:apply_style` always APPENDS (module docstring), so re-running
    `apply_set` on a set that already has the style stacks a SECOND copy of
    every operation the style contains, it does not replace or dedupe the
    first. darktable tags a NEW instance a style application CREATES with
    `multi_name` set to the style's own name (observed on real history
    stacks, not a documented Lua API guarantee), so a case-insensitive
    `multi_name` match against any of the image's history entries is a
    real, if imperfect, signal: it can miss a style whose instances
    darktable did not name after it, and it cannot distinguish an earlier
    application from a deliberate repeat with different values. `force=True`
    bypasses this entirely, for exactly that reason.

    Reads `history.read.__wrapped__` directly (ruling 21/23: lazy import so
    this module stays importable regardless of load order, and the
    unwrapped call so one `apply_set` invocation still emits exactly one
    metrics event). `history.read` still re-resolves the image (one cheap
    `image.lookup` -- `dt.database`'s own dict lookup by id, not a render or
    a write) before its actual work, which is the sidecar read: a local
    file read, no bridge round trip for the part that matters at 300-image
    scale.
    """
    from scouter.core import history

    read_fn = getattr(history.read, "__wrapped__", history.read)
    result = read_fn(resolved.image_id, job=job)
    target = name.strip().lower()
    return any((entry.multi_name or "").strip().lower() == target for entry in result.entries)


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
    force: bool = False,
    job: str | None = None,
) -> StyleSetResult:
    """Apply one style to a whole set, in batches, keeping per-image results.

    A partial set is a RESULT, not an exception: 280 of 300 applied is
    information the caller needs, and phase 1's single-image `apply` raises
    StylePartial, which would discard the payload. So `outcome="partial"` when
    applied > 0 and (failed > 0 or a batch came back "unknown") -- exit 9,
    the same code as style_partial, so one shell branch covers "some of it
    worked" -- and StylePartial only when NOTHING applied (and something did
    go wrong; see the idempotency note below for the one case that is not
    a failure at all).

    Always gated: a set apply is inherently a bulk write (there is no
    "safe by default" single-image case here, unlike `style.apply`), so
    `confirm` is required regardless of how many images the set resolves to
    -- the same reasoning `library.import` uses ("an import is always > 1
    file, so it is always gated", design 3.1).

    Fix round 1 (Opus quality review), five changes:

    1. The `name` typo check (`list_styles`) now runs FIRST, before
       resolving a single image -- a typo costs zero round trips, dry run or
       real, instead of only being caught on the no-confirm/dry-run path.
    2. Resolution never aborts the whole set for one bad candidate: a raw
       `image.lookup` cannot find (a stale or renamed entry in a 300-file
       `import.json`, most likely) becomes a `per_image` failure row
       (`status="failed"`) via `_resolve_set`, and every other candidate is
       still attempted. This applies to the dry run too -- it still returns
       `outcome="dry_run"`, listing the failures rather than raising.
    3. The batch loop catches every `ScouterError` `dtbridge.call` can raise
       for `style.apply` (`DbLocked`, `NotInDarkroom`, `BridgeTimeout`,
       whatever else), not only `BridgeTimeout` -- and NEVER lets one
       propagate out of this function discarding batches that already
       succeeded. `BridgeTimeout` specifically means the client gave up
       without a response, so the bridge genuinely does not know whether
       darktable finished the write -- those ids get `status="unknown"`,
       excluded from both `applied` and `failed`, with a warning pointing at
       `history.read`. Any OTHER `ScouterError` means the bridge answered
       with a definite refusal, so those ids get `status="failed"`. Both
       count toward the existing two-consecutive-batch-failures stop rule.
    4. Idempotency: before the real batch loop, every resolved target's
       sidecar is read locally (`_already_has_style`, cheap -- see its own
       docstring) and a
       target that already looks like it carries this style is skipped by
       default (`status="already_has_style"` in `per_image`) --
       `image:apply_style` always APPENDS, so re-running this verb on an
       already-styled set would otherwise stack a second copy of every
       operation the style contains. `force=True` applies anyway. This does
       not run during a dry run, matching `extract`'s own dry-run path
       (which also defers its local reads past the gate).
    5. `batch` is bounds-checked to 1..100 (`usage` if not), `images=[]`
       (an explicit empty list) is a caller mistake rather than a silent
       fallback to `--job-slug`, and `--images` entries outside
       `--job-slug`'s own `import.json` are refused when both are given.
       See `_resolve_set`'s own docstring for the exact rules.

    `StyleSetResult` (`scouter/types.py`) is out of this fix round's file
    scope, so there is no new top-level field for "unknown" ids or
    "already carries this style" ids -- every `per_image` row carries a
    `status` (`"applied"` | `"failed"` | `"unknown"` | `"already_has_style"`)
    and an `error` (`None` when not applicable); `warnings` also names the
    aggregate counts and ids for whichever of those categories is non-empty,
    so nothing is buried in a list a caller has to filter for by hand.

    `--exclude-hero` (a shortcut to exclude a caller's own extract source
    automatically) is NOT implemented -- there is no such flag; `--exclude`
    only takes an explicit list. Left for task 14 to decide whether it is
    worth adding.

    Never touches the darkroom view (see the module docstring): the Lua
    `style.apply` handler applies from `dt.database` directly, so there is
    no lighttable/darkroom guard to enforce here, stated honestly rather
    than pretended.
    """
    job = job or current_job()
    if not (1 <= batch <= 100):
        raise UsageError(
            f"batch must be between 1 and 100, got {batch!r}",
            detail={"batch": batch, "min": 1, "max": 100},
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

    # Fix round 1, item 4: the typo check runs before ANY image is resolved,
    # dry run or real -- zero round trips wasted either way.
    existing = _existing_styles()
    if name not in existing:
        raise UsageError(
            f"no style named {name!r} exists", detail={"name": name, "existing": existing}
        )

    from scouter import config as config_module

    cfg = config_module.load()
    resolved, unresolved = _resolve_set(job_slug, images, exclude)
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
        dry_warnings: list[str] = []
        if unresolved:
            preview = ", ".join(u["path"] for u in unresolved[:5])
            dry_warnings.append(
                f"{len(unresolved)} of {len(resolved) + len(unresolved)} candidates did "
                f"not resolve and would fail: {preview}" + (" ..." if len(unresolved) > 5 else "")
            )
        return StyleSetResult(
            outcome="dry_run",
            name=name,
            mode=mode,
            requested=len(resolved) + len(unresolved),
            plan=plan,
            per_image=list(unresolved),
            warnings=dry_warnings,
            reason=(
                "dry run: nothing was applied"
                if dry_run
                else f"applying a style to {len(resolved)} images requires --yes / confirm=true"
            ),
        )

    # Fix round 1, item 3 (idempotency): cheap local sidecar reads.
    already_rows: list[dict] = []
    to_apply: list[ResolvedImage] = resolved
    if not force:
        to_apply = []
        for r in resolved:
            if _already_has_style(r, name, job):
                already_rows.append(
                    {
                        "image_id": r.image_id,
                        "path": r.path,
                        "status": "already_has_style",
                        "error": None,
                    }
                )
            else:
                to_apply.append(r)

    id_to_path = {r.image_id: r.path for r in to_apply}
    ids = [r.image_id for r in to_apply]
    batches = [ids[i : i + batch] for i in range(0, len(ids), batch)]

    applied_ids: list[int] = []
    batch_rows: list[dict] = []
    missed_ids: list[int] = []
    unknown_ids: list[int] = []
    consecutive_failures = 0
    batches_run = 0
    warnings: list[str] = []

    for batch_ids in batches:
        batches_run += 1
        try:
            result = dtbridge.call(
                "style.apply", {"image_ids": batch_ids, "name": name, "mode": mode}
            )
        except BridgeTimeout:
            # Fix round 1, item 2: a real client-side timeout -- the bridge
            # genuinely does not know whether darktable finished the write,
            # so this is "unknown", not "failed". Every id in the batch is
            # affected, and earlier batches' successes are already recorded
            # in batch_rows -- a stalled worker on frame 140 must not lose
            # frames 1-139.
            consecutive_failures += 1
            for iid in batch_ids:
                unknown_ids.append(iid)
                batch_rows.append(
                    {
                        "image_id": iid,
                        "path": id_to_path.get(iid),
                        "status": "unknown",
                        "error": "timed out; may or may not have applied -- check "
                        "history.read before retrying",
                    }
                )
            if consecutive_failures >= MAX_CONSECUTIVE_BATCH_FAILURES:
                warnings.append(
                    f"stopped after {consecutive_failures} consecutive batch failures "
                    f"({batches_run} of {len(batches)} batches attempted)"
                )
                break
            continue
        except ScouterError as e:
            # Fix round 1, item 1: any OTHER typed bridge failure (DbLocked,
            # NotInDarkroom, ...) -- the bridge DID answer, with a definite
            # refusal, so this is a real per-image failure, not an unknown.
            # Never propagates: recorded and the loop continues exactly like
            # the BridgeTimeout branch above.
            consecutive_failures += 1
            for iid in batch_ids:
                missed_ids.append(iid)
                batch_rows.append(
                    {
                        "image_id": iid,
                        "path": id_to_path.get(iid),
                        "status": "failed",
                        "error": str(e),
                    }
                )
            if consecutive_failures >= MAX_CONSECUTIVE_BATCH_FAILURES:
                warnings.append(
                    f"stopped after {consecutive_failures} consecutive batch failures "
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
                batch_rows.append(
                    {
                        "image_id": iid,
                        "path": id_to_path.get(iid),
                        "status": "failed",
                        "error": "not found in darktable's library",
                    }
                )
            elif iid in error_ids:
                err = next(e for e in errors if int(e["image_id"]) == iid)
                missed_ids.append(iid)
                batch_rows.append(
                    {
                        "image_id": iid,
                        "path": id_to_path.get(iid),
                        "status": "failed",
                        "error": err.get("error"),
                    }
                )
            else:
                applied_ids.append(iid)
                batch_rows.append(
                    {
                        "image_id": iid,
                        "path": id_to_path.get(iid),
                        "status": "applied",
                        "error": None,
                    }
                )

    per_image = list(unresolved) + already_rows + batch_rows
    requested = len(resolved) + len(unresolved)
    applied = len(applied_ids)
    failed = len(unresolved) + len(missed_ids)
    unknown = len(unknown_ids)
    skipped = len(already_rows)
    never_attempted = requested - applied - failed - unknown - skipped
    if never_attempted > 0:
        warnings.append(f"{never_attempted} images were never attempted")
    if unknown_ids:
        warnings.append(
            f"{unknown} images have unknown status (bridge timeout, may or may not "
            f"have applied): {sorted(unknown_ids)}; check history.read before retrying"
        )
    if already_rows:
        warnings.append(
            f"{skipped} images already carried this style and were skipped "
            f"(force=True to apply again): {sorted(r['image_id'] for r in already_rows)}"
        )

    if job_slug:
        try:
            from scouter.core import vault as vault_module

            rows = [
                {"image": pi["path"] or str(pi["image_id"]), "result": pi["status"]}
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
        if failed == 0 and unknown == 0 and skipped > 0:
            # Every candidate already carried this style: nothing failed,
            # nothing is ambiguous, there was simply nothing left to do --
            # NOT the "nothing applied" failure StylePartial exists for.
            return StyleSetResult(
                outcome="ok",
                name=name,
                mode=mode,
                requested=requested,
                applied=0,
                failed=0,
                batches=batches_run,
                missed=[],
                per_image=per_image,
                warnings=warnings,
                reason="every image already carried this style; nothing applied",
            )
        raise StylePartial(
            f"style {name!r} applied to 0 of {requested} images",
            detail={"per_image": per_image, "missed": missed_ids, "name": name},
        )

    outcome = "partial" if (failed > 0 or unknown > 0) else "ok"
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
                help="how many image ids per bridge call, 1..100",
            ),
            ArgSpec(
                "confirm",
                "bool",
                default=False,
                help="required: applying to a set is always gated",
            ),
            ArgSpec("dry_run", "bool", default=False, help="plan only"),
            ArgSpec(
                "force",
                "bool",
                default=False,
                help="apply even to images that already look like they carry this "
                "style (fix round 1's idempotency guard; a local sidecar-only "
                "heuristic, see apply_set's own docstring)",
            ),
        ),
        writes=True,
        gated=True,
        summary="Apply one style to a whole set, in batches, keeping per-image results.",
    ),
):
    register(verb)
