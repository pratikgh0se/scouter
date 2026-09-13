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
from scouter.errors import (
    NoSidecar,
    NotInDarkroom,
    ScouterError,
    StylePartial,
    StyleWriteFailed,
    UsageError,
)
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


def _read_filtered_sidecar(path: str) -> xmp_module.ParsedXmp:
    """Read+parse the sidecar for the filtered path: typed refusals, never a
    raw exception (fix round 1, blockers 2-3).

    Reuses `darkroom._read_sidecar_parsed` (lazy import to avoid a module
    cycle at import time -- darkroom.py does not import style.py, so this is
    safe, but the lazy form matches the rest of the codebase's convention for
    cross-core-module reuse, e.g. `darkroom._commit`). That function already
    retries a torn (mid-write) read and raises `CommitTimeout` if the file
    never settles into well-formed XML; `None` means no sidecar at all, which
    becomes `NoSidecar` here rather than a raw `FileNotFoundError`.
    """
    from scouter.core import darkroom

    parsed = darkroom._read_sidecar_parsed(path)
    if parsed is None:
        raise NoSidecar(
            f"no sidecar for {path}: commit history first",
            detail={"path": path},
        )
    return parsed


#: Fix round 2, item 7: this is a best-effort, background check -- a dead
#: bridge must not stall the extract for the default 15s fallback timeout
#: (`DEFAULT_TIMEOUTS` has no entry for `darkroom.current`).
_STALENESS_CHECK_TIMEOUT_S = 2.0


def _sidecar_may_predate_darkroom(image_id: int) -> str | None:
    """A cheap, read-only warning (fix round 1, should-fix 7): darktable only
    writes the sidecar when the darkroom is LEFT (design finding, history.py's
    module docstring), so if the darkroom right now shows this SAME image,
    anything edited since the last view-leave has not reached disk yet -- the
    sidecar this call is about to read may predate that live state.

    Mirrors `_require_darkroom_shows`'s query (`darkroom.current`, no side
    effects) rather than a fixed sleep or a full commit. The filtered path
    never needs the DARKROOM (no `dtbridge.require_darkroom()`, no
    `_require_darkroom_shows`) -- but it does need darktable/the bridge
    running for `_existing_styles` and `style.import_file` (fix round 2, item
    7: an earlier draft of this docstring overclaimed "darktable does not
    need to be running at all", which is only true of THIS ONE optional
    check). A short, explicit timeout keeps an unreachable bridge from
    stalling this best-effort check for the 15s default.
    """
    try:
        current = dtbridge.call("darkroom.current", {}, timeout=_STALENESS_CHECK_TIMEOUT_S)
    except ScouterError:
        return None
    loaded = current.get("image_id")
    if loaded is not None and int(loaded) == int(image_id):
        return (
            "the darkroom currently shows this image: the sidecar may predate "
            "an edit made since it was last committed -- run `scouter history "
            "commit` first to be sure the style reflects the latest state"
        )
    return None


def _verify_created(name: str, *, dtstyle_path: str | None = None) -> bool:
    """`created` must be genuinely true, or the caller never learns the write
    silently failed (fix round 2, blocker 2): a fresh `list_styles` round
    trip is the one source of truth here, never a bridge response field
    alone (`style.import_file` returning without raising only proves darktable
    did not RAISE, same lesson as fix round 1's should-fix 9 on the Lua side).
    """
    if name in _existing_styles():
        return True
    raise StyleWriteFailed(
        f"style {name!r} was not created: a fresh list_styles round trip does "
        "not show it, even though the write reported no error",
        detail={"path": dtstyle_path} if dtstyle_path else {"name": name},
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

    Used when `no_filter=True` AND nothing in the image's active history is
    in `cfg.style.never_include` (so there is nothing to drop and the native
    call is equivalent -- fix round 2, blocker 1: when something IS forbidden,
    `extract` uses the `.dtstyle` writer instead, even under `no_filter`), or
    when `.dtstyle` filtering itself is unavailable (task 7's host probe
    found it always is on this darktable build -- see `dtstyle` module
    docstring -- so `extra_warning` is only ever set by a future build that
    regresses that).

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
    path never touches the DARKROOM (no `dtbridge.require_darkroom()`, no
    `_require_darkroom_shows` -- so a style can be extracted from the
    lighttable), but it does need darktable/the bridge running: `_existing_styles`
    and `style.import_file` are both real round trips (fix round 2, item 7).

    `no_filter=True` ignores the `modules` request (every active operation is
    a candidate, not just the requested ones) rather than bypassing
    `cfg.style.never_include` -- per-image geometry and repairs (crop,
    retouch, spot white balance, ...) must never ride a style EITHER way
    (fix round 2, blocker 1; an earlier round let `--no-filter` bypass this
    entirely, which was wrong). When none of the image's active operations
    are in `never_include`, this is phase 1's cheap native path
    (`style.create_from_current`, which DOES need the darkroom showing
    `image` -- that Lua call builds the style from whatever the darkroom
    currently shows, not from `image` itself, hence `_require_darkroom_shows`).
    When some ARE, they are DROPPED (never a refusal -- crop in particular is
    close to universal, so refusing whole-history extraction over it would
    make `no_filter` useless) via the same `.dtstyle` writer the filtered
    path uses, and named in `forbidden`. The gate is unchanged either way:
    `confirm` is required only when a style of that name already exists.
    """
    job = job or current_job()
    # Fix round 1, blocker 1: validated BEFORE any path join -- `name` becomes
    # `<cache>/styles/<name>.dtstyle` below, so `../../x` or `/etc/x` must
    # never reach `Path(...)` at all.
    paths.validate_style_name(name)
    modules = list(modules)
    resolved = dtbridge.resolve_image(image)
    existing = _existing_styles()
    replaces = name in existing

    from scouter.config import load as _load_config

    cfg = _load_config()
    never_include = set(cfg.style.never_include)
    never_include_sorted = sorted(never_include)

    # Fix round 1, should-fix 8: computed BEFORE the dry-run return, so a
    # dry run (or a real run) with a forbidden or entirely-missing module
    # refuses immediately instead of reporting a plan that hides the problem.
    # Fix round 2, item 4: keyed on (operation, multi_priority), not just
    # operation -- two instances of the same module (e.g. two `exposure`
    # sliders) must never collide into one entry and silently lose the
    # other's `enabled` state.
    forbidden: list[str] = []
    modules_missing: list[str] = []
    modules_present: list[str] = []
    modules_disabled: list[str] = []
    parsed: xmp_module.ParsedXmp | None = None
    active_by_key: dict[tuple[str, int], xmp_module.HistoryEntry] = {}

    if no_filter:
        # Fix round 2, blocker 1: `no_filter` drops `modules` as a RESTRICTION
        # (every active operation is a candidate), never `never_include` --
        # that check is unconditional, so the sidecar has to be read here too.
        parsed = _read_filtered_sidecar(resolved.path)
        active_by_key = {
            (e.operation, e.multi_priority): e for e in xmp_module.active_entries(parsed)
        }
        active_ops = {op for op, _prio in active_by_key}
        forbidden = sorted(active_ops & never_include)
        modules_present = sorted(active_ops - never_include)
    else:
        forbidden = sorted(m for m in modules if m in never_include)
        if not forbidden:
            parsed = _read_filtered_sidecar(resolved.path)
            active_by_key = {
                (e.operation, e.multi_priority): e for e in xmp_module.active_entries(parsed)
            }
            active_ops = {op for op, _prio in active_by_key}
            modules_present = [m for m in modules if m in active_ops]
            modules_missing = [m for m in modules if m not in active_ops]

    if active_by_key:
        # Fix round 1, should-fix 5: still written (the request is honoured
        # literally), but named so the caller is never left thinking a
        # disabled module -- or one disabled INSTANCE of it -- is live.
        present_set = set(modules_present)
        modules_disabled = sorted(
            {
                (op if prio == 0 else f"{op}#{prio}")
                for (op, prio), entry in active_by_key.items()
                if op in present_set and not entry.enabled
            }
        )

    plan = [
        {
            "action": "style.extract",
            "image_id": resolved.image_id,
            "name": name,
            "modules_requested": modules,
            "replaces_existing": replaces,
            "no_filter": no_filter,
            "forbidden": forbidden,
            "modules_missing": modules_missing,
        }
    ]

    # Fix round 2, blocker 1: a forbidden module in the FILTERED path (the
    # caller explicitly asked for it by name) is still a hard refusal -- only
    # `no_filter` downgrades a forbidden module already present in the
    # history to a drop, since it is never explicitly "requested" there.
    if forbidden and not no_filter:
        raise UsageError(
            f"{forbidden} may never ride a style: per-image geometry and "
            "repairs (cfg.style.never_include) are excluded on purpose. Remove "
            "them from --modules and re-run",
            detail={"forbidden": forbidden, "never_include": never_include_sorted},
        )
    if not modules_present:
        raise UsageError(
            "none of the requested modules are in the image's active history: "
            "an empty style is not a style",
            detail={"modules_requested": modules, "modules_missing": modules_missing},
        )

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

    if no_filter and not forbidden:
        # Nothing to drop: the cheap native path is equivalent and needs no
        # .dtstyle round trip at all.
        return _extract_whole_history(resolved, name, modules, description, extra_warning=None)

    assert parsed is not None  # guaranteed: forbidden->raised above, modules_present non-empty

    sidecar_warning = _sidecar_may_predate_darkroom(resolved.image_id)

    style_text = dtstyle.build(parsed, name, description, modules_present)
    dtstyle_path = dtstyle.write(paths.styles_dir() / f"{name}.dtstyle", style_text)

    try:
        dtbridge.call(
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

    # Fix round 1, should-fix 4 / fix round 2, blocker 2: `created` is true
    # only once a fresh `list_styles` round trip actually shows the name --
    # never a silent `ok` when it does not (`_verify_created` raises).
    created = _verify_created(name, dtstyle_path=str(dtstyle_path))

    warnings: list[str] = []
    if modules_missing:
        warnings.append(f"modules not in the active history, skipped: {modules_missing}")
    if modules_disabled:
        warnings.append(
            "modules included but their newest entry is disabled (still "
            f"written, honestly labelled): {modules_disabled}"
        )
    if no_filter and forbidden:
        warnings.append(f"never_include modules dropped from the whole-history style: {forbidden}")
    if sidecar_warning:
        warnings.append(sidecar_warning)

    return StyleResult(
        image_id=resolved.image_id,
        path=resolved.path,
        name=name,
        modules_requested=modules,
        modules_honoured=True,
        created=created,
        dtstyle_path=str(dtstyle_path),
        operations_included=modules_present,
        modules_missing=modules_missing,
        modules_disabled=modules_disabled,
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
                help="ignore --modules and keep every active operation "
                "(never_include is still excluded either way)",
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
