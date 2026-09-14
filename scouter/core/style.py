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

`outliers.list` lives in this module too (task 9): the registry
(`scouter.registry._CORE_MODULES`) has no separate `scouter.core.outliers`
entry, only `scouter.core.style`, so a verb registered from a standalone
module would never load. It is otherwise unrelated to styles -- it closes
spec 4.4's loop, flagging frames whose rendered exposure, white balance or
clipping drift too far from the hero for `style.apply` to have made them
look consistent. A pure read verb: one metrics event, no bridge writes
beyond the read-only `image.lookup` behind `resolve_image`, everything else
from `scout.stats_many` and the XMP sidecars.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from pathlib import Path

from scouter import config, metrics, paths
from scouter.core import dtbridge, dtstyle, exif, scout
from scouter.core import xmp as xmp_module
from scouter.errors import (
    ExifUnreadable,
    NoSidecar,
    NotInDarkroom,
    ScouterError,
    StylePartial,
    StyleWriteFailed,
    UsageError,
)
from scouter.paths import current_job
from scouter.registry import ArgSpec, Verb, register
from scouter.types import OutliersResult, ResolvedImage, StyleResult

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
    `extract` uses the `.dtstyle` writer instead, even under `no_filter`).

    `extra_warning` carries `extract`'s "--modules is ignored under
    --no-filter" notice (fix round 3, item 3) when the caller passed a
    non-empty `modules` alongside `no_filter=True` -- there is no other
    reason for it to be set today, since `.dtstyle` filtering itself has
    been available on every darktable build task 7's host probe has seen
    (see `dtstyle` module docstring).

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

    `no_filter=True` reads the sidecar and requires a committed history, same
    as the filtered path (fix round 3, item 3): it ignores `modules` as a
    RESTRICTION (every active operation is a candidate, not just the
    requested ones) rather than bypassing `cfg.style.never_include` --
    per-image geometry and repairs (crop, retouch, spot white balance, ...)
    must never ride a style EITHER way (fix round 2, blocker 1; an earlier
    round let `--no-filter` bypass this entirely, which was wrong). When none
    of the image's active operations are in `never_include`, this is phase
    1's cheap native path (`style.create_from_current`, which DOES need the
    darkroom showing `image` -- that Lua call builds the style from whatever
    the darkroom currently shows, not from `image` itself, hence
    `_require_darkroom_shows`). When some ARE, they are DROPPED (never a
    refusal -- crop in particular is close to universal, so refusing
    whole-history extraction over it would make `no_filter` useless) via the
    same `.dtstyle` writer the filtered path uses, and named in `forbidden`.

    `modules` naming a `never_include` operation EXPLICITLY is always a hard
    refusal, even under `no_filter=True` (fix round 3, item 1): the explicit
    request wins over `no_filter`'s "ignore modules" meaning, since asking
    for a forbidden module by name and also asking to ignore the module list
    is a contradiction Scouter should not silently resolve either way.
    `--no-filter` cannot be combined with `--modules` naming a forbidden op.
    A non-empty `modules` that names nothing forbidden is otherwise ignored
    under `no_filter=True` and a warning says so.

    The gate is unchanged either way: `confirm` is required only when a
    style of that name already exists.
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

    # Fix round 3, item 1: a `modules` entry naming a `never_include`
    # operation is ALWAYS a hard refusal, on every path, including
    # `no_filter=True` -- the explicit request wins over `no_filter`'s
    # "ignore modules" meaning, since naming a forbidden module by hand while
    # also asking to ignore the module list is a contradiction, not something
    # to silently resolve either way. Computed and raised BEFORE anything
    # else (no sidecar read, no bridge call) -- unlike the IMPLICIT drop
    # below (forbidden operations merely present in a whole-history extract),
    # this is a pure argument-shape check.
    explicitly_forbidden = sorted(m for m in modules if m in never_include)
    if explicitly_forbidden:
        raise UsageError(
            f"{explicitly_forbidden} may never ride a style: per-image geometry "
            "and repairs (cfg.style.never_include) are excluded on purpose. "
            "Remove them from --modules and re-run"
            + (
                " (--no-filter cannot be combined with --modules naming a "
                "forbidden op -- --no-filter only ignores modules it was not "
                "explicitly told to include)"
                if no_filter
                else ""
            ),
            detail={
                "forbidden": explicitly_forbidden,
                "never_include": never_include_sorted,
                "no_filter": no_filter,
            },
        )

    # Fix round 3, item 3: `no_filter=True` ignores a non-empty `modules` as a
    # RESTRICTION (it names nothing forbidden, or the check above would
    # already have raised) -- warn so the caller is never left thinking their
    # `--modules` list did anything.
    modules_ignored_warning: str | None = None
    if no_filter and modules:
        modules_ignored_warning = (
            f"--modules is ignored under --no-filter (every active operation "
            f"is a candidate instead): {modules}"
        )

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
    active_ops: set[str] = set()

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
        forbidden = sorted(m for m in modules if m in never_include)  # always [] here; see above
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
            "modules_ignored": bool(modules_ignored_warning),
            "forbidden": forbidden,
            "modules_missing": modules_missing,
        }
    ]

    if not modules_present:
        if no_filter and active_ops:
            # Fix round 3, item 2: this is the IMPLICIT-drop case -- nothing
            # was "requested" under no_filter, so "none of the requested
            # modules are present" would be a lie. Say what actually
            # happened: every active operation is forbidden.
            active_sorted = sorted(active_ops)
            raise UsageError(
                f"all {len(active_sorted)} active modules are per-image modules "
                f"(cfg.style.never_include) and cannot form a style: {active_sorted}",
                detail={"active": active_sorted, "never_include": never_include_sorted},
            )
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
        return _extract_whole_history(
            resolved, name, modules, description, extra_warning=modules_ignored_warning
        )

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
    if modules_ignored_warning:
        warnings.append(modules_ignored_warning)
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


def _ev_proxy(frame_lum: float, ref_lum: float) -> float:
    """log2 of the rendered-luminance ratio. Named ev_proxy, NEVER ev.

    It is a ratio of mean luminance on a 512/1024 px render, not a metered
    exposure difference: scene content moves it, and two frames of different
    subjects can differ by 0.4 "EV" with identical camera settings. The name
    and the result's own label say so, so no caller mistakes it for a reading.
    """
    return math.log2((frame_lum + 0.5) / (ref_lum + 0.5))


def _frame_coeffs(path: str) -> list[float] | None:
    """The frame's `temperature` coefficients [r, g, b] from its XMP, or
    `None` when there is no sidecar or no active `temperature` entry."""
    from scouter.core import darkroom

    parsed = darkroom._read_sidecar_parsed(path)
    if parsed is None:
        return None
    params = xmp_module.latest_params(parsed, "temperature")
    if not params:
        return None
    try:
        return [float(params["coeff_r"]), float(params["coeff_g"]), float(params["coeff_b"])]
    except (KeyError, TypeError, ValueError):
        return None


def _resolve_kelvin(coeffs: list[float] | None, camera: str | None) -> tuple[float | None, str]:
    """`wb.kelvin`'s answer for one frame's coefficients, or a reason why
    there isn't one. `wb.kelvin` is imported lazily (task 11 -- until it
    lands this always degrades to the ratio test, its own designed
    fallback)."""
    if coeffs is None:
        return None, "no temperature coefficients in this frame's XMP (module not committed)"
    if not camera:
        return None, "camera unknown: cannot look up a wb calibration table"
    try:
        from scouter.core import wb
    except ImportError:
        return None, "wb.kelvin is not available yet (run scouter wb calibrate once task 11 lands)"
    result = wb.kelvin(coeffs, camera)
    return result.get("kelvin"), result.get("reason") or ""


def _measure_frame(frame: dict, ref: dict, tolerances) -> dict:
    """One frame's four measures against `ref` (the hero): `ev_proxy_delta`,
    `temperature_k_delta` (or a reason it is `None`), the always-available
    `wb_ratio_rg_delta`/`wb_ratio_bg_delta`, and the absolute
    `clipped_high_pct`. `reasons` carries one sentence per breach, each with
    both numbers and the tolerance; `severity` is `max(|breach| /
    tolerance)` across every breach this frame has, for worst-first sorting.
    """
    reasons: list[str] = []
    severities: list[float] = []

    frame_lum = frame["stats"].get("luminance_mean", 0.0)
    ref_lum = ref["stats"].get("luminance_mean", 0.0)
    ev_delta = _ev_proxy(frame_lum, ref_lum)
    if abs(ev_delta) > tolerances.exposure_ev:
        severities.append(abs(ev_delta) / tolerances.exposure_ev)
        reasons.append(
            f"ev_proxy {ev_delta:+.2f} outside ±{tolerances.exposure_ev:.2f} "
            f"(mean luminance {frame_lum:.0f} vs hero {ref_lum:.0f})"
        )

    rg_delta = frame["stats"].get("wb_ratio_rg", 0.0) - ref["stats"].get("wb_ratio_rg", 0.0)
    bg_delta = frame["stats"].get("wb_ratio_bg", 0.0) - ref["stats"].get("wb_ratio_bg", 0.0)

    frame_k, frame_k_reason = _resolve_kelvin(frame.get("coeffs"), frame.get("camera"))
    ref_k, ref_k_reason = _resolve_kelvin(ref.get("coeffs"), frame.get("camera"))

    temperature_k_delta: float | None = None
    temperature_k_reason: str | None = None
    if frame_k is not None and ref_k is not None:
        wb_test = "kelvin"
        temperature_k_delta = frame_k - ref_k
        if abs(temperature_k_delta) > tolerances.temperature_k:
            severities.append(abs(temperature_k_delta) / tolerances.temperature_k)
            reasons.append(
                f"temperature_k {temperature_k_delta:+.0f} outside "
                f"±{tolerances.temperature_k:.0f}"
            )
    else:
        wb_test = "ratio"
        temperature_k_reason = frame_k_reason or ref_k_reason
        worst = rg_delta if abs(rg_delta) >= abs(bg_delta) else bg_delta
        worst_name = "wb_ratio_rg" if abs(rg_delta) >= abs(bg_delta) else "wb_ratio_bg"
        if abs(worst) > tolerances.wb_ratio:
            severities.append(abs(worst) / tolerances.wb_ratio)
            reasons.append(
                f"{worst_name} {worst:+.3f} outside ±{tolerances.wb_ratio:.3f} "
                f"({temperature_k_reason})"
            )

    clip = frame["stats"].get("clipped_high_pct", 0.0)
    if clip > tolerances.clipped_high_pct:
        severities.append(clip / tolerances.clipped_high_pct)
        reasons.append(f"clipped_high_pct {clip:.2f}% outside {tolerances.clipped_high_pct:.2f}%")

    row: dict = {
        "seq": frame["seq"],
        "path": frame["path"],
        "image_id": frame.get("image_id"),
        "ev_proxy_delta": round(ev_delta, 4),
        "temperature_k_delta": (
            round(temperature_k_delta, 1) if temperature_k_delta is not None else None
        ),
        "wb_ratio_rg_delta": round(rg_delta, 6),
        "wb_ratio_bg_delta": round(bg_delta, 6),
        "wb_test": wb_test,
        "clipped_high_pct": clip,
        "reasons": reasons,
        "is_outlier": bool(reasons),
        "severity": max(severities) if severities else 0.0,
    }
    if temperature_k_delta is None:
        row["temperature_k_reason"] = temperature_k_reason
    return row


def _gather_frames(
    cfg,
    ref_resolved: ResolvedImage,
    *,
    job_slug: str | None,
    images: list | None,
) -> tuple[list[dict], Path | None]:
    """Every non-reference frame in the set, as `{seq, path, image_id,
    camera}` dicts. `--job-slug` reads the job's `import.json` (same shape
    `cull.propose` reads); `--images` resolves each image and reads its own
    EXIF for a camera slug. The reference is excluded from its own set
    either way (fix round: a hero comparing itself to itself is not a
    finding)."""
    job_path: Path | None = None
    raw_frames: list[dict] = []
    if images:
        for idx, image in enumerate(images, start=1):
            resolved = dtbridge.resolve_image(image)
            if resolved.image_id == ref_resolved.image_id:
                continue
            try:
                tags = exif.read_tags(resolved.path)
                camera = exif.camera_slug(tags, cfg)
            except (ExifUnreadable, OSError):
                camera = ""
            raw_frames.append(
                {
                    "seq": idx,
                    "path": resolved.path,
                    "image_id": resolved.image_id,
                    "camera": camera,
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
        camera = manifest.get("camera", "")
        for f in manifest.get("files", []):
            path = str(job_path / f["dest"])
            resolved = dtbridge.resolve_image(path)
            if resolved.image_id == ref_resolved.image_id:
                continue
            raw_frames.append(
                {"seq": f["seq"], "path": path, "image_id": resolved.image_id, "camera": camera}
            )
    return raw_frames, job_path


@metrics.instrumented("outliers.list")
def outliers(
    reference: str | int,
    *,
    job_slug: str | None = None,
    images: list[str | int] | None = None,
    px: int | None = None,
    workers: int | None = None,
    job: str | None = None,
) -> OutliersResult:
    """Flag frames whose rendered exposure, white balance or clipping drift
    too far from `reference` (the hero) -- spec 4.4's loop closer, "these six
    frames need you". A read verb: one metrics event, `scout.stats_many` and
    the XMP sidecars only, no bridge writes."""
    cfg = config.load()
    px = px if px is not None else cfg.stats.set_px
    workers = workers if workers is not None else cfg.stats.workers

    if not job_slug and not images:
        raise UsageError(
            "outliers.list needs --job-slug or --images",
            detail={"job_slug": job_slug, "images": images},
        )

    ref_resolved = dtbridge.resolve_image(reference)
    try:
        ref_tags = exif.read_tags(ref_resolved.path)
        ref_camera = exif.camera_slug(ref_tags, cfg)
    except (ExifUnreadable, OSError):
        ref_camera = ""

    raw_frames, job_path = _gather_frames(cfg, ref_resolved, job_slug=job_slug, images=images)
    if not raw_frames:
        raise UsageError(
            "outliers.list: the comparison set is empty once the reference is excluded",
            detail={"job_slug": job_slug, "images": images, "reference": str(reference)},
        )

    all_paths = [ref_resolved.path] + [f["path"] for f in raw_frames]
    stats_job_dir = config.scouter_dir(job_path) if job_path is not None else None
    stats_results = scout.stats_many(all_paths, px=px, workers=workers, job_dir=stats_job_dir)

    ref_stat = stats_results[0]
    if "error" in ref_stat:
        raise UsageError(
            f"could not compute stats for the reference {ref_resolved.path}: "
            f"{ref_stat['error'].get('message')}",
            detail={"reference": ref_resolved.path, "error": ref_stat["error"]},
        )
    ref_frame = {
        "path": ref_resolved.path,
        "image_id": ref_resolved.image_id,
        "camera": ref_camera,
        "stats": ref_stat.get("stats", {}),
        "coeffs": _frame_coeffs(ref_resolved.path),
    }

    failed: list[dict] = []
    rows: list[dict] = []
    for raw, stat in zip(raw_frames, stats_results[1:]):
        if "error" in stat:
            failed.append({"seq": raw["seq"], "path": raw["path"], "error": stat["error"]})
            continue
        merged = dict(raw)
        merged["stats"] = stat.get("stats", {})
        merged["coeffs"] = _frame_coeffs(raw["path"])
        rows.append(_measure_frame(merged, ref_frame, cfg.tolerances))

    outlier_rows = sorted(
        (r for r in rows if r["is_outlier"]), key=lambda r: (-r["severity"], r["seq"])
    )
    within_rows = [r for r in rows if not r["is_outlier"]]

    wb_tests_used = {r["wb_test"] for r in rows}
    if wb_tests_used == {"kelvin"}:
        wb_test = "kelvin"
    elif wb_tests_used == {"ratio"} or not wb_tests_used:
        wb_test = "ratio"
    else:
        wb_test = "mixed"

    warnings: list[str] = [
        "ev_proxy is log2 of the rendered-luminance ratio on a 512/1024px preview, "
        "not a metered exposure reading -- scene content moves it, never call it 'ev'"
    ]
    if failed:
        warnings.append(
            f"stats failed for {len(failed)} frame(s); excluded from both outliers and "
            "within_tolerance -- an unmeasured frame is not a passing frame"
        )

    if job_slug:
        vault_rows = [
            ("checked", str(len(rows))),
            ("within tolerance", str(len(within_rows))),
            ("outliers", str(len(outlier_rows))),
            ("failed", str(len(failed))),
            ("wb test", wb_test),
        ]
        try:
            from scouter.core import vault

            vault.append_stage(cfg, job_slug, "Outliers", vault_rows, "outliers reviewed")
        except Exception as e:  # noqa: BLE001 - vault write must never roll back a read verb
            warnings.append(f"vault stage not appended: {e}")

    return OutliersResult(
        reference={
            "image_id": ref_resolved.image_id,
            "path": ref_resolved.path,
            "camera": ref_camera,
        },
        tolerances=asdict(cfg.tolerances),
        wb_test=wb_test,
        checked=len(rows),
        within_tolerance=len(within_rows),
        outliers=outlier_rows,
        failed=failed,
        warnings=warnings,
        path=str(job_path) if job_path is not None else None,
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
                help="reads the sidecar and requires a committed history, same "
                "as the default path; ignores --modules and keeps every active "
                "operation instead (never_include is still excluded either way "
                "-- naming a forbidden op in --modules is still refused)",
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


register(
    Verb(
        id="outliers.list",
        group="outliers",
        fn=outliers,
        args=(
            ArgSpec(
                "reference",
                "image",
                required=True,
                help="path or image id of the hero frame every other frame is compared to",
            ),
            ArgSpec("job_slug", "str", help="job slug; frames come from its import.json"),
            ArgSpec(
                "images",
                "list[str]",
                help="explicit image list, instead of (or alongside) --job-slug "
                "(the reference is excluded from its own comparison either way)",
            ),
            ArgSpec("px", "int", help="stats render size (default: stats.set_px)"),
            ArgSpec("workers", "int", help="thread pool size (default: stats.workers)"),
        ),
        writes=False,
        gated=False,
        summary="Flag frames whose exposure, white balance or clipping drift "
        "too far from the reference.",
    )
)
