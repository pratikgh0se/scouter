"""The live darkroom verbs. Every write is one darktable history item."""

from __future__ import annotations

import contextvars
import math
import time
from collections.abc import Callable

from scouter import metrics, paths
from scouter.core import dtbridge, journal, vocab, xmp
from scouter.errors import (
    CommitTimeout,
    ModuleUnavailable,
    NotInDarkroom,
    ScouterError,
    UsageError,
    WriteUnverified,
)
from scouter.paths import current_job
from scouter.registry import ArgSpec, Verb, register
from scouter.types import (
    CommitResult,
    HistoryResult,
    ModuleResult,
    OpenResult,
    SetResult,
    StatusResult,
    UndoResult,
)

# `nudge` never computes a step size of its own: it repeats darktable's own
# `up`/`down` effect, which moved exposure by 0.05 EV in the phase 0 spike. That
# keeps the step whatever darktable says it is, per module, with no table to rot.

#: darktable rewrites the sidecar in place; a read can race a write and see a
#: torn (not-yet-well-formed) document. Fix round 1, should-fix 10: retry
#: briefly before giving up, rather than reporting a bogus `None` value.
_TORN_RETRIES = 5
_TORN_SLEEP_S = 0.1


class _UnsupportedUndo(ScouterError):
    """`reset` discarded a module's params; there is nothing to restore.

    Fix round 1, blocker 6: generic exit code 1 -- `unsupported_undo` is not
    one of `scouter.errors.EXIT_CODES`'s rows, deliberately: it is a Scouter
    policy refusal, not a darktable/bridge failure class.
    """

    code = "unsupported_undo"
    fix = "press Ctrl-Z in darktable; Scouter cannot restore discarded params"


class _UndoUnverified(ScouterError):
    """Task 11 fix round 1, blocker 2: tier 3's Kelvin-inversion undo (design
    11.4) sent a value and committed, but the post-restore coefficient ratio
    did not come back within `wb._EXACT_TOLERANCE` -- an approximate undo
    Scouter cannot confirm landed is never silently reported as done. The
    journal entry stays open (this raises before `mark_undone` is reached),
    so the same undo is reachable again on the next call.
    """

    code = "undo_unverified"
    fix = (
        "check the darkroom by eye; pin the coefficient sliders "
        "(spike/scouter_wb_probe.lua) for an exact undo, or restore by hand"
    )


def _require_finite(name: str, value: float) -> float:
    """Reject nan/inf BEFORE any bridge call (final review, should-fix 5).

    `dt.gui.action(..., "set", nan)` is not a no-op: nan is also the bridge's
    own "not in the darkroom / module not in the pipe" signal, so sending one
    makes the reply indistinguishable from that failure, and JSON has no
    literal for it either (`Envelope.to_dict` turns one into null). A caller
    mistake is worth a typed `usage` error, not a slider set to garbage.
    """
    try:
        number = float(value)
    except (TypeError, ValueError) as e:
        raise UsageError(
            f"{name} must be a number, got {value!r}", detail={"argument": name}
        ) from e
    if not math.isfinite(number):
        raise UsageError(
            f"{name} must be a finite number, got {number!r}",
            detail={"argument": name, "value": repr(number)},
        )
    return number


def _record_opened_image(job: str, image_id: int, path: str) -> None:
    """Remember which image THIS job opened (ruling 30).

    Read-modify-write under a lock, not a blind overwrite (task 12 fix round
    1, should-fix 5): `scouter.core.history` writes the same per-job state
    file (a `watermarks` key) from a possibly-concurrent writer, and
    `paths.job_state_update` is the one seam both modules go through so
    neither can silently clobber the other's key.
    """

    def _mutate(state: dict) -> None:
        state["opened_image_id"] = int(image_id)
        state["opened_path"] = path

    paths.job_state_update(job, _mutate)


def _require_job_image(job: str, loaded_image_id: int) -> None:
    """Refuse a write when the darkroom no longer shows the job's own image.

    Ruling 30: `darkroom.current` reads the SELECTION (`dt.gui.action_images`
    -- the display_image getter deadlocks the worker, see the plugin), so
    "an image is loaded" is not by itself proof it is the one this job opened.
    If the job has opened an image and the darkroom now reports a different
    one, the write would land on somebody else's photo: refuse, and say both
    ids. A job that has never opened anything has nothing to compare against
    and is left alone.
    """
    expected = paths.job_state_read(job).get("opened_image_id")
    if expected is None or int(expected) == int(loaded_image_id):
        return
    raise NotInDarkroom(
        f"darkroom.current reports {int(loaded_image_id)}, job opened {int(expected)}: "
        "refusing to write to an image this job did not open",
        fix=(
            "re-open the image you meant with darkroom open (CLI `scouter darkroom open "
            "<image>`, MCP darkroom_open), or start a new job"
        ),
        detail={"loaded_image_id": int(loaded_image_id), "job_image_id": int(expected), "job": job},
    )


def _commit_with_readback(
    job: str,
    image_id: int,
    *,
    xmp_path: str,
    confirm: Callable[[], bool],
    what: str,
    strategy: str = "auto",
    predicted_noop: bool = False,
) -> tuple[bool, list[str], bool]:
    """`_commit`, with a commit timeout resolved by reading the XMP back.

    Returns `(committed, warnings, noop)`. `noop` is True when darktable wrote
    nothing because the requested state was already the sidecar's (confirmed
    by `confirm()`, never assumed).

    Final review, should-fix 6: `darkroom.set` already did this, and every
    other writer did not -- so a write whose journal entry says `completed`
    and whose value is already on disk came back as `commit_timeout`.
    darktable writes a sidecar only when the history actually CHANGES, so a
    write that asks for the state the image already has legitimately produces
    no write at all. `confirm()` re-reads the sidecar; if it agrees with what
    was asked for, the request is satisfied and says so in a warning. If it
    does not, the timeout is re-raised untouched -- a write that never landed
    is never reported as committed.

    `predicted_noop` (row 433, night-1 C): the caller already saw, BEFORE
    sending, that the sidecar holds the requested state (a reset of a module
    already at its defaults, an undo whose value is already back, an enable
    of an enabled module). Then one expect-no-change bounce (~1 s) is spent,
    not the full two-bounce 5.4 s wait for a sidecar darktable will never
    write; `confirm()` still decides, and a wrong prediction falls through
    to the real commit.

    `xmp_path` (fix round 1, should-fix 3): the CALLER's already-resolved
    sidecar path, so watermarking a readback-resolved commit costs no extra
    `darkroom.current` round-trip -- every caller already has this path from
    resolving its own image.
    """
    from scouter.core import history

    noop_warning = (
        f"darktable wrote no new sidecar for {what}: the state requested is the "
        "one the XMP already carried, so there was no history change to write. "
        "Confirmed by reading it back from the sidecar."
    )
    if predicted_noop:
        try:
            first = _commit(job, image_id, strategy=strategy, attempts=1, expect_change=False)
        except CommitTimeout:
            first = None
        if first is not None and first.committed:
            return True, list(first.warnings), False
        if confirm():
            history.watermark_write(job, image_id, xmp_path)
            return True, [noop_warning], True
    try:
        result = _commit(job, image_id, strategy=strategy)
    except CommitTimeout:
        if not confirm():
            raise
        # Task 12, step 2: a readback-resolved commit still means the sidecar
        # on disk is now the state Scouter believes it committed, so the
        # watermark is recorded here too -- `history.commit`'s own success
        # path never ran (it raised), so nothing else would record it. This
        # branch's own `confirm()` already IS the readback confirmation
        # blocker 2 (fix round 1) requires, so watermarking here is safe.
        history.watermark_write(job, image_id, xmp_path)
        return True, [noop_warning], True
    return result.committed, list(result.warnings), False


def _commit(
    job: str,
    image_id: int,
    *,
    strategy: str = "auto",
    attempts: int | None = None,
    expect_change: bool = True,
) -> CommitResult:
    """Every write's post-action commit, routed through one seam (ruling 21).

    Imports `scouter.core.history` lazily so `darkroom.py` stays importable
    (and `registry.load_verbs()` keeps working) whether or not it has landed
    yet. Calls the *unwrapped* `history.commit` (fix round 1, blocker 13) so
    one `darkroom.*` verb call still produces exactly one metrics event, not
    two -- `history.commit` is itself `@instrumented`. Falls back to the
    wrapped function if `__wrapped__` is ever absent, so this never breaks
    outright if that decorator detail changes.

    `attempts`/`expect_change` (task 12): forwarded straight to
    `history.commit`, whose docstring covers the no-op-pre-check use case.
    `attempts=None` lets `history.commit` supply its own default.
    """
    from scouter.core import history

    commit_fn = getattr(history.commit, "__wrapped__", history.commit)
    kwargs: dict = {
        "timeout_s": 5.0,
        "strategy": strategy,
        "expect_change": expect_change,
        "job": job,
    }
    if attempts is not None:
        kwargs["attempts"] = attempts
    return commit_fn(image_id, **kwargs)


def _close(a: float | None, b: float | None, tol: float = 1e-4) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < tol


def _read_sidecar_parsed(path: str) -> xmp.ParsedXmp | None:
    """Parse the sidecar, retrying briefly on a torn (mid-write) read.

    Returns `None` when there is no sidecar at all (nothing to retry: that is
    a normal "not committed yet" state, not a torn write). Raises
    `CommitTimeout` if the file exists but never settles into well-formed XML
    within `_TORN_RETRIES` attempts.
    """
    for attempt in range(_TORN_RETRIES):
        try:
            text, _ = xmp.read_sidecar(path)
        except OSError:
            return None
        parsed = xmp.parse(text)
        if parsed.well_formed:
            return parsed
        if attempt < _TORN_RETRIES - 1:
            time.sleep(_TORN_SLEEP_S)
    raise CommitTimeout(
        f"sidecar not readable (torn write): {xmp.sidecar_path(path)}",
        detail={"path": path, "attempts": _TORN_RETRIES},
    )


def _read_value(path: str, module: str, param: str, instance: int) -> float | None:
    """Current value from the XMP, or None when it is not decodable."""
    parsed = _read_sidecar_parsed(path)
    if parsed is None:
        return None
    return xmp.latest_value(parsed, module, param, instance)


def _module_state(path: str, module: str, instance: int) -> dict:
    """`enabled` + `history_end` for one module instance, from the XMP."""
    parsed = _read_sidecar_parsed(path)
    if parsed is None:
        return {"source": "xmp", "enabled": None, "history_end": None}
    enabled = None
    for entry in sorted(parsed.entries, key=lambda e: e.num, reverse=True):
        if entry.operation == module and entry.active and entry.multi_priority == instance:
            enabled = entry.enabled
            break
    return {"source": "xmp", "enabled": enabled, "history_end": parsed.history_end}


def _module_enabled(path: str, module: str, instance: int) -> bool | None:
    return _module_state(path, module, instance)["enabled"]


def _temperature_extra(path: str, instance: int) -> dict:
    """`params` (the whole decoded vector) + `params_hex`, for `temperature`
    only (task 11 step 2). A `temperature` write needs the FULL coefficient
    vector to be undoable, not just the one field `_read_value` can name --
    the Kelvin slider has no `temperature` field in the XMP at all (design
    §11.1: Kelvin is never stored, only camera-relative coefficients are).
    """
    parsed = _read_sidecar_parsed(path)
    if parsed is None:
        return {"params": None, "params_hex": None}
    params = xmp.latest_params(parsed, "temperature", instance)
    if params is None:
        return {"params": None, "params_hex": None}
    params = dict(params)
    params_hex = params.pop("params_hex", None)
    return {"params": params, "params_hex": params_hex}


#: darktable 5.4 `src/iop/temperature.c`: `dt_iop_temperature_preset_t`'s
#: DT_IOP_TEMP_D65_LATE, the `preset` field (5th, `xmp.LAYOUTS[("temperature",
#: 4)]`) of a "camera reference (late)" white balance -- 5.4's scene-referred
#: default. darktable stores the AS-SHOT coefficients and sets
#: `late_correction`, so color calibration applies the D65 correction late.
#: Every temperature/tint/coefficient slider change runs
#: `_temp_tint_callback`/`gui_changed`, which force DT_IOP_TEMP_USER (2) and
#: drop that correction: the frame turns cooler whichever way Kelvin moved
#: (gauntlet night 1, cov-phrase-warmer-olympus-touch). No GUI action keeps
#: preset 4 while changing the coefficients.
WB_PRESET_D65_LATE = 4

#: Python-only exemption (no CLI/MCP argument; `set_param`'s signature is
#: pinned by the layer-parity test): `wb.calibrate`'s sweep measures the
#: Kelvin->coefficient mapping, which the late correction does not enter.
WB_LATE_EXEMPT: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "scouter_wb_late_exempt", default=False)


def wb_preset(path: str, instance: int = 0) -> int | None:
    """The temperature module's decoded `preset`, from the sidecar, or `None`
    (no sidecar, no active temperature entry, or params that did not decode).
    Local read only; no bridge call."""
    parsed = _read_sidecar_parsed(path)
    if parsed is None:
        return None
    params = xmp.latest_params(parsed, "temperature", instance)
    preset = (params or {}).get("preset")
    return preset if isinstance(preset, int) else None


def wb_late_fix(params: set[str] | list[str] | tuple[str, ...]) -> str:
    """The honest `fix` for a white-balance write refused in late mode. It names
    only what exists: colour calibration is darktable's place for warmth in
    this mode, Scouter does not drive it, and the one live substitute (a
    color balance rgb grade, for temperature only) is labelled as a grade."""
    fix = (
        "darktable's white balance on this image is in \"camera reference (late)\" mode "
        "(temperature preset 4, darktable 5.4's scene-referred default): it keeps the as-shot "
        "coefficients and color calibration applies the D65 correction late. Changing "
        "temperature, tint or the coefficients through darktable's sliders switches the module "
        "to \"user modified\" and switches that late correction off, so the colours shift the "
        "wrong way (cooler, whichever way you asked). Nothing was changed in darktable. In this "
        "mode darktable adjusts warmth in the color calibration module (its CAT illuminant), "
        "which Scouter does not drive yet: change it there by hand."
    )
    if "temperature" in set(params):
        fix += (
            " Nearest thing Scouter can do: a colour grade, not a white-balance correction -- "
            "say 'warm the highlights' or 'cool the shadows' (color balance rgb)."
        )
    else:
        fix += " Scouter has no substitute for a tint change."
    return fix


def refuse_wb_late(path: str, module: str, params: list[str], instance: int = 0, *,
                   what: str, extra: dict | None = None) -> None:
    """Raise `ModuleUnavailable` when a `temperature` write would land on a
    preset-4 (camera reference, late) white balance. A no-op for every other
    module and every other preset (0/1/2/3), and when the preset is unknown."""
    if module != "temperature":
        return
    preset = wb_preset(path, instance)
    if preset != WB_PRESET_D65_LATE:
        return
    raise ModuleUnavailable(
        f"{what} cannot run: darktable's white balance on this image is in camera reference "
        "(late) mode, and changing it through darktable's sliders would switch off the late "
        "correction and shift the colours the wrong way",
        fix=wb_late_fix(params),
        detail={"module": module, "params": list(params), "instance": instance,
                "temperature_preset": preset, "mode": "camera reference (late)",
                "darktable_enum": "DT_IOP_TEMP_D65_LATE", "path": path, **(extra or {})},
    )


def _read_state(path: str, module: str, param: str, instance: int) -> dict:
    """Everything the journal needs about one slider's current state

    (fix round 1, blocker 7): not just `value`, but `enabled` and
    `history_end` too, so a future reader can tell "this slider was disabled"
    or "this was a stale read" apart from a plain missing value. Ruling 20
    still holds: `enabled` travels with every one of these, so a disabled
    module is never reported as live.
    """
    state = _module_state(path, module, instance)
    state = {**state, "value": _read_value(path, module, param, instance)}
    if module == "temperature":
        state = {**state, **_temperature_extra(path, instance)}
    return state


def _declared_default(module: str, param: str) -> float | None:
    """The vocabulary row's declared `neutral` (else darktable `default`)."""
    s = vocab.spec(module, param)
    if s is None:
        return None
    if s.neutral is not None:
        return float(s.neutral)
    if s.default is not None:
        return float(s.default)
    return None


def _effective_current(raw_state: dict, module: str, param: str) -> tuple[float | None, str]:
    """What darktable really holds for this slider, per the sidecar ON DISK:
    `(value, source)`.

    The decoded value when there is one (`"xmp"`). When the sidecar exists
    but the module has NO active history entry, darktable runs it at its
    defaults, so the row's declared neutral/default is the value (`"default"`)
    -- row 433, night-1 C: `set colorbalancergb.contrast 0` on a frame with
    no colour-balance history wrote nothing (0 is what it already held) and
    Scouter, comparing against "no value", waited 5.4 s for a commit_timeout.
    Anything else (no sidecar, an entry that did not decode) is `None`.
    """
    value = raw_state.get("value")
    if value is not None:
        return float(value), "xmp"
    if raw_state.get("history_end") is not None and raw_state.get("enabled") is None:
        default = _declared_default(module, param)
        if default is not None:
            return default, "default"
    return None, "xmp"


def _effective_read(path: str, module: str, param: str, instance: int) -> float | None:
    """`_effective_current` of the sidecar as it reads right now."""
    state = {
        **_module_state(path, module, instance),
        "value": _read_value(path, module, param, instance),
    }
    return _effective_current(state, module, param)[0]


def _module_at_defaults(path: str, module: str, instance: int) -> bool:
    """Does the sidecar show `module` at its darktable defaults?

    Only answerable from what Scouter knows: every vocabulary row of the
    module that declares a neutral/default must decode, and equal it. A
    module with no such row (exposure, sigmoid, ...) is never "known at
    defaults" -- its reset stays unconfirmable, and a timeout stays a
    timeout.
    """
    rows = [
        s for s in vocab.PARAMS
        if s.module == module and s.value_source == "xmp"
        and _declared_default(module, s.param) is not None
    ]
    if not rows:
        return False
    parsed = _read_sidecar_parsed(path)
    if parsed is None:
        return False
    params = xmp.latest_params(parsed, module, instance)
    if not params:
        return False
    for s in rows:
        value = params.get(s.param)
        if not isinstance(value, (int, float)):
            return False
        if not _close(float(value), _declared_default(module, s.param)):
            return False
    return True


def _raise_unverified(
    what: str,
    *,
    module: str,
    param: str,
    requested: float,
    expected: float,
    applied: float | None,
    previous: float | None,
    history_end_before: int | None,
    path: str,
    instance: int,
) -> None:
    """Row 433: a committed write whose sidecar value is not the one asked
    for (nor a declared darktable clamp) is a typed error, never `ok`."""
    history_end_after = _module_state(path, module, instance).get("history_end")
    message = (
        f"{what}: darktable was sent {requested:g} and the commit reported done, but the "
        f"sidecar reads {applied!r} (expected {expected:g}, was {previous!r})"
    )
    if (
        history_end_before is not None
        and history_end_after is not None
        and history_end_after < history_end_before
    ):
        message += (
            f"; the sidecar's history went BACKWARDS ({history_end_before} -> "
            f"{history_end_after} items): it was rewritten from an older library state"
        )
    raise WriteUnverified(
        message,
        detail={
            "module": module,
            "param": param,
            "requested": requested,
            "expected": expected,
            "applied": applied,
            "previous": previous,
            "tolerance": vocab.verify_tolerance(module, param, expected, previous),
            "history_end_before": history_end_before,
            "history_end_after": history_end_after,
        },
    )


def _last_journal_write(
    job: str, image_id: int, module: str, param: str, instance: int
) -> journal.Entry | None:
    """The newest entry for this exact (image, module, param, instance) that
    actually landed a value -- skipping anything undone or failed (fix round
    2, blocker 2): an undone write is reverted, and a failed one never
    happened, so neither is a source of truth for "what Scouter last set."

    A landed `module.reset` for the same image/module/instance stops the
    search (fix round 3, item 2): reset discards every param of that module,
    so nothing journaled before it is still valid "current state" for any of
    them -- not even an entry the reset-refusal path later marked `undone`
    (that flag only means "don't ask to undo this again"; the reset itself
    really happened). A reset that itself failed never happened and does not
    stop the search.
    """
    for entry in reversed(journal.read_all(job)):
        is_error = entry.outcome is not None and entry.outcome.startswith("error")
        if (
            entry.verb == "module.reset"
            and not is_error
            and entry.image_id == image_id
            and entry.module == module
            and entry.instance == instance
        ):
            return None
        if entry.undone:
            continue
        if is_error:
            continue
        if (
            entry.image_id == image_id
            and entry.module == module
            and entry.param == param
            and entry.instance == instance
            and entry.after
            and "value" in entry.after
        ):
            return entry
    return None


def _raw_and_before_state(
    job: str, image_id: int, path: str, module: str, param: str, instance: int
) -> tuple[dict, dict, bool]:
    """Returns `(raw_state, before_state, pending_uncommitted)`.

    `raw_state` is EXACTLY what the sidecar shows on disk right now -- the
    only state the no-op shortcut and the stale-but-matching mid path may
    ever compare a requested value against (task 12 fix round 1, blocker 1).
    `before_state` additionally applies the journal `commit=False` fallback
    below, for callers (the journal entry's `before=`, `SetResult.previous`,
    `undo_last`) that want the logical "current value" INCLUDING an
    uncommitted write Scouter itself made -- a fallback that is exactly
    wrong for the no-op decision, which must never trust anything but the
    bytes actually on disk.

    `pending_uncommitted` is True when the last landed write for this exact
    (image, module, param, instance) has `commit=False`: darktable's live
    history already differs from the sidecar in a way the journal knows
    about but the sidecar cannot show yet. Neither the shortcut nor the mid
    path may fire in that case -- not even when `raw_state` happens to
    coincidentally equal the requested value, because sending the SAME value
    Scouter set last time is what would silently re-confirm a stale-on-disk
    edit without ever flushing it (the reviewer's repro: set 4.25 with
    `commit=False`, then set 4.25 again -- the second call must still bounce
    and commit).

    Ordinarily `before_state`'s fallback value is whatever the XMP currently
    shows. But when the last (live, landed) write to this exact key used
    `commit=False`, darktable has not written the sidecar for it yet -- the
    XMP still shows whatever was true before THAT write, not Scouter's own
    true current value, so the journal's own `after` is used instead. This
    fallback applies only when that last write's own `commit` flag was False
    (fix round 2, blocker 2 -- not a "does the XMP look stale" guess, which
    fix round 1 used and which a coincidental value match could fool): a
    `commit=True` write's own `history.commit` call already waited for the
    XMP to advance, so its value is trustworthy and no fallback is needed.
    """
    raw_state = _read_state(path, module, param, instance)
    last = _last_journal_write(job, image_id, module, param, instance)
    pending_uncommitted = last is not None and last.commit is False
    before_state = {**raw_state, "value": last.after["value"]} if pending_uncommitted else raw_state
    return raw_state, before_state, pending_uncommitted


def _noop_shortcut(
    job: str,
    resolved: dtbridge.ResolvedImage,
    module: str,
    param: str,
    instance: int,
    target: float | None,
    raw_state: dict,
    *,
    requested: float | None,
    bound: float | None = None,
) -> SetResult | None:
    """The zero-bounce, zero-bridge-write path (task 12).

    `target` is the absolute value this write would end up at, known BEFORE
    sending anything: `set`'s own requested value, or (for `nudge`) its own
    current value when `steps=0` asks for no movement at all. `None` means
    either the caller cannot know the target without sending the action
    first (a nonzero-step nudge), or (fix round 1, blocker 1) there is a
    pending `commit=False` write for this key that disqualifies ANY
    comparison against the sidecar -- either way, there is nothing to
    short-circuit.

    `raw_state` must be the sidecar EXACTLY as it reads on disk right now
    (never the journal-adjusted `before_state`): a value that matches only
    because of that fallback is proof of an unflushed edit sitting in
    darktable, not proof there is nothing to write.

    When the target already matches the module's raw on-disk value, the
    module is enabled, AND the watermark proves that value is still fresh
    (not stale from an uncommitted edit), there is nothing to send: darktable
    only writes the sidecar when the history actually CHANGES (phase 1 live
    finding 4), so a repeat of the same value would otherwise cost a full
    commit to discover it had nothing to write -- ~5.5s in the worst case,
    two bounces and two sidecar waits. Returns `None` (not a shortcut) when
    the watermark is missing or stale, or the value or `enabled` disagrees --
    the caller falls through to the normal write-and-commit path.
    """
    if target is None:
        return None
    from scouter.core import history

    fresh, _why = history.watermark_fresh(job, resolved.image_id, resolved.xmp_path)
    if not (
        fresh and raw_state.get("enabled") is not False and _close(target, raw_state.get("value"))
    ):
        return None
    warnings = [
        f"no-op: {module}.{param} is already {raw_state.get('value')} in the sidecar, "
        "which Scouter last committed; nothing was sent to darktable"
    ]
    if bound is not None:
        warnings.append(_clamp_warning(module, param, requested, bound))
    return SetResult(
        image_id=resolved.image_id,
        path=resolved.path,
        module=module,
        param=param,
        instance=instance,
        requested=requested,
        applied=raw_state.get("value"),
        previous=raw_state.get("value"),
        committed=True,
        noop=True,
        clamped=bound is not None,
        source=raw_state.get("source", "xmp"),
        warnings=warnings,
    )


def _clamp_warning(module: str, param: str, requested: float | None, bound: float) -> str:
    return (
        f"clamped: {module}.{param} {requested:g} is beyond darktable's own hard limit; "
        f"darktable stores {bound:g} (the bound), not the value requested"
    )


def _current_image() -> dtbridge.ResolvedImage:
    """The darkroom's currently loaded image, read-only, via `darkroom.current`.

    Fix round 2, blocker 1: never `darkroom.open {}` -- that method actively
    loads/switches the darkroom and now errors outright when given nothing to
    load (there is no "current image" fallback in it any more). `darkroom.
    current` has no side effects and reports `image_id`/`path` absent when
    nothing is loaded, which is exactly the answer a read like this needs.
    """
    result = dtbridge.call("darkroom.current", {})
    image_id = result.get("image_id")
    path = result.get("path")
    if image_id is None or path is None:
        raise NotInDarkroom(
            f"no image is loaded in the darkroom (view is {result.get('view', 'unknown')})",
            detail={"view": result.get("view")},
        )
    path = str(path)
    return dtbridge.ResolvedImage(
        image_id=int(image_id), path=path, xmp_path=str(xmp.sidecar_path(path))
    )


@metrics.instrumented("darkroom.open")
def open_image(image: str | int, *, job: str | None = None) -> OpenResult:
    """Load `image` in the darkroom, importing it into the library if needed."""
    job = job or current_job()
    key = {"image_id": int(image)} if str(image).isdigit() else {"path": str(image)}
    result = dtbridge.call("darkroom.open", key)
    # Ruling 30: this job now owns that image. Every later write in the job
    # checks `darkroom.current` against it and refuses if they disagree.
    _record_opened_image(job, int(result["image_id"]), str(result["path"]))
    return OpenResult(
        image_id=int(result["image_id"]),
        path=str(result["path"]),
        view=str(result.get("view", "unknown")),
        opened=bool(result.get("opened")),
        already_open=bool(result.get("already_open")),
    )


@metrics.instrumented("darkroom.set")
def set_param(
    module: str,
    param: str,
    value: float,
    *,
    instance: int = 0,
    commit: bool = True,
    dry_run: bool = False,
    job: str | None = None,
) -> SetResult:
    """Set one module parameter to an absolute value (phase 0 finding 1).

    A `temperature` write on a white balance in camera reference (late) mode
    is refused (`module_unavailable`) before anything is sent; see
    `refuse_wb_late`. `wb.calibrate`'s sweep is exempt via `WB_LATE_EXEMPT`.

    A dry run makes no darktable calls at all -- not even to learn which image
    is in the darkroom (fix round 1, blocker 14) -- so it reports only the
    action that would be sent, never a value or an image id.
    """
    job = job or current_job()
    value = _require_finite("value", value)
    # Row 408, B8: computed ONCE, up front, before EITHER branch does
    # anything else -- an unpinned (module, param) is refused before
    # `_current_image`/`require_darkroom` (bridge round trips) or
    # `journal.begin`, not just before the `darkroom.action` call itself.
    path_ = dtbridge.action_path(module, param)
    if dry_run:
        return SetResult(
            outcome="dry_run",
            module=module,
            param=param,
            instance=instance,
            requested=value,
            plan=[
                {
                    "action": "darkroom.set",
                    "path": path_,
                    "effect": "set",
                    "value": value,
                    "instance": instance,
                }
            ],
            reason="dry run: no darktable calls were made; the darkroom image was not resolved",
        )
    resolved = _current_image()
    _require_job_image(job, resolved.image_id)
    if not WB_LATE_EXEMPT.get():
        refuse_wb_late(resolved.path, module, [param], instance,
                       what=f"darkroom set {module}.{param}")
    dtbridge.require_darkroom()
    raw_state, before_state, pending_uncommitted = _raw_and_before_state(
        job, resolved.image_id, resolved.path, module, param, instance
    )
    # Row 433, night-1 B/C: what darktable will actually STORE for `value`
    # (a declared hard limit clamps it; a hue wraps), and what it holds now
    # (the decoded value, or the module default when the module has no
    # history entry). Every no-op decision and readback below compares these
    # effective values, never the raw request against "no value".
    effective, bound = vocab.dt_effective(module, param, value)
    current, current_source = _effective_current(raw_state, module, param)
    effective_raw = {**raw_state, "value": current, "source": current_source}
    shortcut = _noop_shortcut(
        job,
        resolved,
        module,
        param,
        instance,
        None if pending_uncommitted else effective,
        effective_raw,
        requested=value,
        bound=bound,
    )
    if shortcut is not None:
        return shortcut
    # The pre-check matched the RAW on-disk value but the watermark was stale
    # (or absent), so the write below is genuinely sent -- but a real change
    # is still the unlikely outcome here: the XMP already says `value`, just
    # not provably freshly. `expect_change=False, attempts=1` tells the
    # commit to spend one bounce, not two (task 12). Never predicted when
    # `pending_uncommitted` (blocker 1, fix round 1): a value that matches
    # the sidecar only by coincidence, while a real unflushed edit is
    # sitting in darktable, is not a no-op -- it can be a real change that
    # must still be sent and committed for real (`expect_change=True`).
    predicted_noop = (
        not pending_uncommitted
        and raw_state.get("enabled") is not False
        and _close(effective, current)
    )
    entry = journal.begin(
        job,
        verb="darkroom.set",
        image_id=resolved.image_id,
        path=resolved.path,
        module=module,
        param=param,
        instance=instance,
        before=before_state,
        commit=commit,
    )
    try:
        dtbridge.action(
            path_,
            instance=instance,
            element="value",
            effect="set",
            value=value,
        )
    except Exception as e:
        code = e.code if isinstance(e, ScouterError) else "error"
        journal.fail(job, entry.seq, outcome=f"error:{code}")
        raise
    journal.complete(job, entry.seq, after={"value": value})

    committed = False
    noop = False
    applied = None
    warnings: list[str] = []

    def _readback() -> float | None:
        return _effective_read(resolved.path, module, param, instance)

    if commit:
        # darktable writes a sidecar only when the history actually CHANGES
        # (host-proven, task 12 fix round 1). Setting a slider to the value it
        # already holds adds no history item, so nothing is written and the
        # commit "times out" -- while the XMP on disk already says exactly what
        # was asked for. That is a satisfied request, not a failure, and it is
        # the difference between this test suite passing once and passing
        # twice. So: on a commit timeout, read the XMP back before deciding.
        # The timeout is still raised when the readback does NOT confirm the
        # value -- a write that genuinely never landed must never be reported
        # as committed. "The value" is the EFFECTIVE one (row 433): a request
        # beyond darktable's hard limit that it stored AT the limit, which
        # the sidecar already held, is equally a satisfied no-op.
        def _try_commit(expect_change: bool, attempts: int | None) -> CommitResult | None:
            nonlocal commit_error
            try:
                return _commit(
                    job,
                    resolved.image_id,
                    strategy="auto",
                    expect_change=expect_change,
                    attempts=attempts,
                )
            except CommitTimeout as e:
                commit_error = e
                return None

        commit_error: CommitTimeout | None = None
        if predicted_noop:
            commit_result = _try_commit(False, 1)
        else:
            commit_result = _try_commit(True, None)

        if commit_result is not None and not commit_result.committed:
            # `expect_change=False` (fix round 1, blocker 2): the bridge's own
            # `sidecar_written=False` is NOT proof nothing changed -- only proof
            # that this ONE bounce found nothing to flush. `history.commit`
            # never stamps a watermark on this path for exactly that reason
            # (`CommitResult.confirmed=False`); only an independent readback
            # against the REQUESTED value earns one. A mismatch means the
            # one-bounce prediction was wrong -- a real, unflushed change is
            # pending -- so this escalates to a genuine `expect_change=True`
            # commit rather than ever certifying a stale sidecar as fresh.
            applied = _readback()
            if _close(applied, effective):
                from scouter.core import history as _history

                _history.watermark_write(job, resolved.image_id, resolved.xmp_path)
                committed = True
                noop = True
                warnings.extend(commit_result.warnings)
                warnings.append(
                    f"readback confirmed {module}.{param} already carries {effective:g}: "
                    "the watermark was recorded now, from this confirmation, not from "
                    "the bridge's own (unconfirmed) report."
                )
            else:
                commit_result = _try_commit(True, None)
                applied = None

        if not committed:
            if commit_result is not None and commit_result.committed:
                committed = True
                warnings.extend(commit_result.warnings)
            else:
                if applied is None:
                    applied = _readback()
                if _close(applied, effective):
                    committed = True
                    noop = True
                    warnings.append(
                        f"darktable wrote no new sidecar for {module}.{param}: the value "
                        f"requested ({value:g}) is the one the XMP already carried, so "
                        "there was no history change to write. Confirmed by reading the "
                        "value back from the sidecar."
                    )
                else:
                    assert commit_error is not None
                    raise commit_error
        if applied is None:
            applied = _readback()
        if applied is None:
            warnings.append(
                f"{module}.{param} committed but could not be decoded from the XMP; "
                "`requested` is what was sent, `applied` is unknown"
            )
        elif _read_value(resolved.path, module, param, instance) is None:
            warnings.append(
                f"{module} has no history entry in the sidecar, so darktable runs it at "
                f"its default: {module}.{param} = {applied:g}"
            )
    clamped = False
    if commit and noop:
        # Already confirmed against the effective value by the readback above.
        clamped = bound is not None
        if clamped:
            warnings.append(_clamp_warning(module, param, value, bound))
    elif commit and applied is not None:
        # Row 433, night-1 A/B: never `ok` for a value the sidecar does not
        # show. A match with the request is a landed write; a match with a
        # declared darktable hard limit the request crossed is a clamp,
        # reported as one; anything else is `write_unverified`.
        previous_eff = current if current is not None else before_state.get("value")
        if vocab.values_match(module, param, applied, value, previous=previous_eff):
            pass
        elif bound is not None and vocab.values_match(
            module, param, applied, effective, previous=previous_eff
        ):
            clamped = True
            warnings.append(_clamp_warning(module, param, value, bound))
        else:
            _raise_unverified(
                f"{module}.{param}",
                module=module,
                param=param,
                requested=value,
                expected=effective,
                applied=applied,
                previous=previous_eff,
                history_end_before=raw_state.get("history_end"),
                path=resolved.path,
                instance=instance,
            )
    return SetResult(
        image_id=resolved.image_id,
        path=resolved.path,
        module=module,
        param=param,
        instance=instance,
        requested=value,
        applied=applied,
        previous=before_state.get("value"),
        committed=committed,
        noop=noop,
        clamped=clamped,
        warnings=warnings,
    )


@metrics.instrumented("darkroom.nudge")
def nudge(
    module: str,
    param: str,
    steps: int = 1,
    *,
    instance: int = 0,
    commit: bool = True,
    dry_run: bool = False,
    job: str | None = None,
) -> SetResult:
    """Move a slider by `steps` of darktable's own default step (+/-).

    Journals *before* the loop, not after (fix round 1, blocker 2): a crash
    partway through would otherwise leave a write darktable actually applied
    with no journal trace at all. `steps_landed` records how many of `steps`
    actually reached the bridge; `undo_last` always reverts to `before`
    regardless of how many landed, so a partial nudge is still fully undoable.
    """
    job = job or current_job()
    # `steps` is an int in every layer's schema, but MCP arguments arrive as
    # whatever JSON carried, so a float nan/inf can reach here (should-fix 5).
    steps_number = _require_finite("steps", steps)
    if steps_number != int(steps_number):
        raise UsageError(
            f"steps must be a whole number of slider steps, got {steps!r}",
            detail={"argument": "steps"},
        )
    steps = int(steps_number)
    path_ = dtbridge.action_path(module, param)
    effect = "up" if steps >= 0 else "down"
    if dry_run:
        return SetResult(
            outcome="dry_run",
            module=module,
            param=param,
            instance=instance,
            plan=[
                {
                    "action": "darkroom.nudge",
                    "path": path_,
                    "effect": effect,
                    "repeats": abs(steps),
                    "instance": instance,
                }
            ],
            reason="dry run: no darktable calls were made; the darkroom image was not resolved",
        )
    resolved = _current_image()
    _require_job_image(job, resolved.image_id)
    refuse_wb_late(resolved.path, module, [param], instance,
                   what=f"darkroom nudge {module}.{param}")
    dtbridge.require_darkroom()
    raw_state, before_state, pending_uncommitted = _raw_and_before_state(
        job, resolved.image_id, resolved.path, module, param, instance
    )
    # `nudge` computes its absolute target BEFORE writing only in the trivial
    # case: `steps=0` asks for no movement at all, so the target is simply
    # the module's own RAW on-disk value (task 12). A nonzero-step nudge
    # cannot know its landing value without sending the action -- darktable,
    # not Scouter, owns the step size -- so there is nothing to short-circuit
    # there; `_noop_shortcut` returns `None` and the nudge proceeds normally.
    # `pending_uncommitted` (fix round 1, blocker 1) disqualifies the
    # shortcut the same way it does for `set`.
    shortcut = _noop_shortcut(
        job,
        resolved,
        module,
        param,
        instance,
        raw_state.get("value") if (steps == 0 and not pending_uncommitted) else None,
        raw_state,
        requested=None,
    )
    if shortcut is not None:
        return shortcut
    entry = journal.begin(
        job,
        verb="darkroom.nudge",
        image_id=resolved.image_id,
        path=resolved.path,
        module=module,
        param=param,
        instance=instance,
        before=before_state,
        commit=commit,
    )
    landed = 0
    last_status: float | None = None
    try:
        for _ in range(abs(steps)):
            result = dtbridge.action(
                path_, instance=instance, element="value", effect=effect, value=1
            )
            landed += 1
            status_value = result.get("status")
            if isinstance(status_value, (int, float)):
                last_status = float(status_value)
    except Exception as e:
        code = e.code if isinstance(e, ScouterError) else "error"
        journal.fail(job, entry.seq, outcome=f"error:{code}")
        raise
    # `dt.gui.action` returns the resulting absolute value for effect up/down
    # (not just "set") -- phase 0 finding 1 applies here too, so this is a
    # real bridge-reported number, not a guessed one. Recording it in `after`
    # (fix round 2, blocker 6) is what lets `_last_journal_write` treat a
    # nudge the same as a `set` for the commit=False fallback.
    after: dict = {"steps": steps, "steps_landed": landed}
    if last_status is not None:
        after["value"] = last_status
    journal.complete(job, entry.seq, after=after)

    committed, applied, warnings = False, None, []
    if commit:
        # Same commit-timeout resolution `set` has (should-fix 6): the honest
        # confirmation for a nudge is the value darktable itself reported for
        # the last step (`after["value"]`) already being in the sidecar.
        landed_value = after.get("value")

        def _confirm_nudge() -> bool:
            if landed_value is None:
                return False
            return _close(_read_value(resolved.path, module, param, instance), landed_value)

        committed, warnings, _noop = _commit_with_readback(
            job,
            resolved.image_id,
            xmp_path=resolved.xmp_path,
            confirm=_confirm_nudge,
            what=f"{module}.{param}",
        )
        applied = _read_value(resolved.path, module, param, instance)
        # Row 433: darktable itself reported where the last step landed; a
        # committed sidecar that says something else is not a landed nudge.
        if (
            landed_value is not None
            and applied is not None
            and not vocab.values_match(
                module, param, applied, landed_value, previous=before_state.get("value")
            )
        ):
            _raise_unverified(
                f"{module}.{param}",
                module=module,
                param=param,
                requested=landed_value,
                expected=landed_value,
                applied=applied,
                previous=before_state.get("value"),
                history_end_before=raw_state.get("history_end"),
                path=resolved.path,
                instance=instance,
            )
    return SetResult(
        image_id=resolved.image_id,
        path=resolved.path,
        module=module,
        param=param,
        instance=instance,
        requested=None,
        applied=applied,
        previous=before_state.get("value"),
        committed=committed,
        warnings=warnings,
    )


def _do_module_action(
    action_name: str,
    method: str,
    *,
    module: str,
    instance: int,
    dry_run: bool,
    confirm: bool,
    job: str | None,
) -> ModuleResult:
    job = job or current_job()
    # Row 408, B6/B8: refused BEFORE any bridge call, journal entry or XMP
    # read -- an unrecognised module wedges darktable's Lua worker for the
    # rest of the session, so it must never even reach a dry-run plan.
    dtbridge.require_known_module(module)
    gated = action_name == "reset"
    plan = [{"action": f"module.{action_name}", "module": module, "instance": instance}]
    if dry_run or (gated and not confirm):
        return ModuleResult(
            outcome="dry_run",
            module=module,
            action=action_name,
            instance=instance,
            plan=plan,
            reason=(
                "reset discards this module's parameters and cannot be undone by "
                "Scouter (design 8.2); re-run with --yes / confirm=true"
                if gated and not confirm
                else "dry run: no darktable calls were made; the darkroom image was not resolved"
            ),
        )
    resolved = _current_image()
    _require_job_image(job, resolved.image_id)
    dtbridge.require_darkroom()
    before_state = _module_state(resolved.path, module, instance)
    # Row 433: already in the requested state before anything is sent?
    if action_name == "reset":
        predicted_noop = _module_at_defaults(resolved.path, module, instance)
    else:
        predicted_noop = before_state["enabled"] is {"enable": True, "disable": False}.get(
            action_name
        )
    entry = journal.begin(
        job,
        verb=f"module.{action_name}",
        image_id=resolved.image_id,
        path=resolved.path,
        module=module,
        param="",
        instance=instance,
        before=before_state,
    )
    try:
        dtbridge.call(method, {"module": module, "instance": instance})
    except Exception as e:
        code = e.code if isinstance(e, ScouterError) else "error"
        journal.fail(job, entry.seq, outcome=f"error:{code}")
        raise
    # Should-fix 6: enable/disable can be confirmed from the XMP (the module's
    # own `enabled` flag), so a commit that wrote nothing because the module
    # was ALREADY in the requested state is reported honestly instead of as a
    # commit_timeout. `reset` cannot be confirmed this way -- it discards
    # params, and the sidecar cannot tell "reset to defaults" from "never
    # touched" -- so its timeout is left to propagate, which is the honest
    # answer there.
    expected_enabled = {"enable": True, "disable": False}.get(action_name)

    def _confirm_module() -> bool:
        if action_name == "reset":
            # Row 433, night-1 C (`cov-colorbalancergb-*-reset`): a reset of a
            # module already at its defaults writes nothing. Confirmable only
            # for a module whose vocabulary rows declare their darktable
            # defaults, and only when every one of them reads back at it.
            return _module_at_defaults(resolved.path, module, instance)
        if expected_enabled is None:
            return False
        return _module_enabled(resolved.path, module, instance) is expected_enabled

    committed, warnings, noop = _commit_with_readback(
        job,
        resolved.image_id,
        xmp_path=resolved.xmp_path,
        confirm=_confirm_module,
        what=f"{module} ({action_name})",
        predicted_noop=predicted_noop,
    )
    after_state = _module_state(resolved.path, module, instance)
    journal.complete(
        job, entry.seq, after={"action": action_name, "enabled": after_state["enabled"]}
    )
    return ModuleResult(
        image_id=resolved.image_id,
        path=resolved.path,
        module=module,
        action=action_name,
        instance=instance,
        enabled=after_state["enabled"],
        committed=committed,
        noop=noop,
        warnings=warnings,
    )


@metrics.instrumented("darkroom.enable")
def enable(
    module: str, *, instance: int = 0, dry_run: bool = False, job: str | None = None
) -> ModuleResult:
    """Enable a module. Not gated -- `reset` is the only destructive verb here."""
    return _do_module_action(
        "enable",
        "module.enable",
        module=module,
        instance=instance,
        dry_run=dry_run,
        confirm=True,
        job=job,
    )


@metrics.instrumented("darkroom.reset")
def reset(
    module: str,
    *,
    instance: int = 0,
    confirm: bool = False,
    dry_run: bool = False,
    job: str | None = None,
) -> ModuleResult:
    """Reset a module to its defaults. Gated: discards params, needs `confirm`."""
    return _do_module_action(
        "reset",
        "module.reset",
        module=module,
        instance=instance,
        dry_run=dry_run,
        confirm=confirm,
        job=job,
    )


#: The fields `("temperature", 4)`'s layout names (`scouter/core/xmp.py`).
#: `coeff_g2` is `inf` on the EM10iii body (never a real slider target there),
#: so it is only ever sent when it is BOTH pinned AND finite (fix round: a
#: caller never sends `_require_finite`-rejected inf/nan to the bridge).
def _rb_ratio(params: dict | None) -> float | None:
    """A `temperature` param dict's red/blue coefficient ratio, or `None`.

    Carried finding 10 (task 11): this arithmetic, and the tolerance test
    below, were spelled out three times inside the tier-3 Kelvin-inversion
    undo -- in `_after_ratio`, in `_confirm`, and again inlined for
    `verified`. Three copies of a comparison that decides whether an undo
    is reported as verified is three chances for them to drift apart.

    `None` when either coefficient is missing, when blue is zero, or when
    EITHER is non-finite: the ratio does not exist, and a number would be a
    lie about it.

    Fix round 1, minor b: the docstring already promised `None` for a
    non-finite blue and the code did not deliver it -- `r / inf` is `0.0`, a
    finite-looking ratio that `_ratio_within_tolerance` would then happily
    compare, and `coeff_g2 = inf` is a REAL value on this camera (phase 1's
    pinned `temperature` params), so a stray `inf` in `coeff_b` is not a
    hypothetical. `nan` is caught by the same check: it is not finite, and a
    `nan` ratio compares false against everything, which would read as "did
    not verify" rather than "could not be compared".
    """
    if not params:
        return None
    r, b = params.get("coeff_r"), params.get("coeff_b")
    if r is None or not b:
        return None
    if not math.isfinite(r) or not math.isfinite(b):
        return None
    return r / b


def _ratio_within_tolerance(measured: float | None, target: float | None) -> bool:
    """`measured` is within `wb._EXACT_TOLERANCE` (relative) of `target`.

    A missing ratio on either side is `False` -- "cannot compare" is never
    reported as "verified".
    """
    from scouter.core import wb

    if measured is None or target is None:
        return False
    return abs(measured - target) / abs(target) <= wb._EXACT_TOLERANCE


_TEMPERATURE_COEFF_NAMES = ("coeff_r", "coeff_g", "coeff_b", "coeff_g2")


def _temperature_sliders_pinned() -> bool:
    """Tier 2 (design 11.3, "probe-gated: undo through the coefficient
    sliders") is available only once the host probe (task 11 step 3,
    `tests/test_host_phase2.py::test_pin_wb_coefficient_sliders`) has pinned
    all three always-finite coefficients' `dt.gui.action` paths into
    `dtbridge.ACTION_PATHS`. `coeff_g2` is optional -- it is `inf`/unused on
    the EM10iii body -- and is restored too when it happens to be pinned and
    the journalled value is finite.
    """
    return all(
        ("temperature", name) in dtbridge.ACTION_PATHS for name in ("coeff_r", "coeff_g", "coeff_b")
    )


def _undo_temperature_via_sliders(
    job: str, entry: journal.Entry, before_params: dict
) -> tuple[bool, list[str], dict]:
    """Tier 2 (design 11.3): one absolute `set` per pinned coefficient, ONE
    `history.commit`, then verify the whole vector from the XMP (brief step 4).

    `_close` compares each component against the CLEANED value --
    `xmp.latest_params` already turns `inf`/`nan` into `None` (fix: both this
    function's `before_params` and its post-restore read go through the same
    `_temperature_extra`/`xmp.latest_params` cleaning), so `coeff_g2 = inf`
    compares as "both non-finite", never as two unequal raw floats.
    """
    sent: list[str] = []
    for name in _TEMPERATURE_COEFF_NAMES:
        value = before_params.get(name)
        if value is None or ("temperature", name) not in dtbridge.ACTION_PATHS:
            continue
        dtbridge.action(
            dtbridge.action_path("temperature", name),
            instance=entry.instance,
            element="value",
            effect="set",
            value=float(value),
        )
        sent.append(name)

    def _after_params() -> dict:
        return _temperature_extra(entry.path, entry.instance).get("params") or {}

    def _confirm() -> bool:
        after_params = _after_params()
        return all(_close(after_params.get(name), before_params.get(name)) for name in sent)

    committed, warnings, _noop = _commit_with_readback(
        job,
        entry.image_id,
        xmp_path=str(xmp.sidecar_path(entry.path)),
        confirm=_confirm,
        what="temperature (undo coefficients)",
    )
    after_params = _after_params()
    failed = [name for name in sent if not _close(after_params.get(name), before_params.get(name))]
    verified = not failed
    if failed:
        warnings.append(
            f"could not verify the restore for seq {entry.seq}: component(s) {failed} did not "
            f"come back as journaled (read {after_params!r}, expected {before_params!r})"
        )
    undone = {
        "verb": entry.verb,
        "module": entry.module,
        "param": entry.param,
        "restored_to": after_params,
        "verified": verified,
        "tier": "coefficient_sliders",
    }
    return committed, warnings, undone


def _temperature_camera(entry: journal.Entry) -> str | None:
    """Best-effort camera slug for `entry.path`, or `None` -- tier 3/4 both
    need this, and neither may raise: an EXIF read failing is not a reason
    to skip straight past tier 3 to a worse refusal message."""
    from scouter.config import load as load_config
    from scouter.core import exif

    try:
        cfg = load_config()
        tags = exif.read_tags(entry.path)
        return exif.camera_slug(tags, cfg)
    except ScouterError:
        return None


def _temperature_target_kelvin(
    before_params: dict | None, camera: str | None
) -> tuple[float | None, dict]:
    """`wb.kelvin`'s answer for the journalled coefficients, or `(None, {})`."""
    if not before_params or not camera:
        return None, {}
    coeffs = [
        before_params.get("coeff_r"),
        before_params.get("coeff_g"),
        before_params.get("coeff_b"),
    ]
    if any(c is None for c in coeffs):
        return None, {}
    from scouter.core import wb

    result = wb.kelvin(coeffs, camera)
    return result.get("kelvin"), result


def _undo_temperature_via_kelvin(
    job: str, entry: journal.Entry, before_params: dict, camera: str
) -> tuple[bool, list[str], dict] | None:
    """Tier 3 (design 11.4, "built regardless: the per-camera calibration
    table"): when the coefficient sliders are not pinned, invert the
    journalled coefficients to a target Kelvin from the measured table
    (`wb.calibrate`) and restore THAT through the ordinary Kelvin slider.
    This is an APPROXIMATE undo -- a Kelvin round trip through darktable's
    own blackbody model, not an exact coefficient restore -- which is why
    tier 2 (the coefficient sliders) is always tried first. Returns `None`
    when no measured table (or no camera) is available, so the caller falls
    through to tier 4's honest refusal.

    Fix round 1, blocker 2: the write is only reported "done" when the
    post-restore ratio verifies within `wb._EXACT_TOLERANCE` -- the same
    tolerance `wb._invert` uses to call a sample "exact", since both
    questions are "is this ratio close enough to trust." On a verification
    failure this RAISES `_UndoUnverified` (never returns a normal result
    with a warning): the caller's `journal.mark_undone` is then never
    reached, so the entry stays open and reachable on the next call --
    an approximate undo Scouter cannot confirm landed must not be
    indistinguishable from one that did.
    """
    from scouter.core import wb

    target_k, _ = _temperature_target_kelvin(before_params, camera)
    if target_k is None:
        return None
    target_ratio = _rb_ratio(before_params)

    dtbridge.action(
        dtbridge.action_path("temperature", "temperature"),
        instance=entry.instance,
        element="value",
        effect="set",
        value=float(target_k),
    )

    def _confirm() -> bool:
        after_params = _temperature_extra(entry.path, entry.instance).get("params") or {}
        return _ratio_within_tolerance(_rb_ratio(after_params), target_ratio)

    try:
        committed, warnings, _noop = _commit_with_readback(
            job,
            entry.image_id,
            xmp_path=str(xmp.sidecar_path(entry.path)),
            confirm=_confirm,
            what="temperature (undo via Kelvin inversion)",
        )
    except CommitTimeout as e:
        # Row 433, night-1 C (`cov-p2-brighter-warmer-olympus`): a RETRY of an
        # unverified Kelvin undo sends the Kelvin darktable already holds, so
        # darktable writes nothing and the commit times out. The honest
        # answer is the same typed `undo_unverified` the first attempt gave
        # (with the ratio the sidecar really holds), not a `commit_timeout`.
        after_ratio = _rb_ratio(_temperature_extra(entry.path, entry.instance).get("params"))
        raise _UndoUnverified(
            f"seq {entry.seq}: Kelvin-inversion undo sent {target_k:.0f} K but darktable wrote "
            "no new sidecar (it already holds that Kelvin), and the coefficient ratio does not "
            f"verify within {wb._EXACT_TOLERANCE:.0%}: measured r/b={after_ratio!r}, "
            f"target r/b={target_ratio!r}",
            detail={
                "seq": entry.seq,
                "target_kelvin": target_k,
                "measured_ratio": after_ratio,
                "target_ratio": target_ratio,
                "tolerance": wb._EXACT_TOLERANCE,
                "coefficients": before_params,
                "commit": e.to_dict(),
            },
        ) from e
    # Fix round 2, minor (d): ONE read here for both the verification ratio
    # and `restored_to`, instead of three separate `_temperature_extra`
    # calls (`_after_ratio()`, `_confirm()` again, and a bare
    # `_temperature_extra` for `after_params`) round 1 had. `_confirm`'s own
    # read, used during a commit-TIMEOUT retry inside
    # `_commit_with_readback`, is a genuinely different moment (before this
    # function knows the commit settled) and is left alone.
    after_params = _temperature_extra(entry.path, entry.instance).get("params") or {}
    after_ratio = _rb_ratio(after_params)
    verified = _ratio_within_tolerance(after_ratio, target_ratio)
    if not verified:
        raise _UndoUnverified(
            f"seq {entry.seq}: Kelvin-inversion undo committed ({target_k:.0f} K from "
            f"{camera}'s measured table) but the coefficient ratio did not verify within "
            f"{wb._EXACT_TOLERANCE:.0%}: measured r/b={after_ratio!r}, target r/b={target_ratio!r}",
            detail={
                "seq": entry.seq,
                "target_kelvin": target_k,
                "measured_ratio": after_ratio,
                "target_ratio": target_ratio,
                "tolerance": wb._EXACT_TOLERANCE,
                "coefficients": before_params,
                # Fix round 2, minor (a): `_commit_with_readback`'s own
                # warnings (e.g. a commit-timeout resolved by readback) were
                # silently dropped on this path -- real information about
                # what happened, not just the verified path's to keep.
                "warnings": warnings,
            },
        )
    warnings.append(
        f"restored via Kelvin inversion ({target_k:.0f} K from {camera}'s measured table), not "
        "the exact coefficient sliders -- pin the sliders on this host (`scouter_wb_probe.lua`) "
        "for an exact undo"
    )
    undone = {
        "verb": entry.verb,
        "module": entry.module,
        "param": entry.param,
        "restored_to": after_params,
        "restored_kelvin": target_k,
        "verified": verified,
        "tier": "kelvin_inversion",
    }
    return committed, warnings, undone


def _warn_late_not_restored(entry: journal.Entry, before_params: dict | None, undone: dict,
                            warnings: list[str]) -> None:
    """Undo is never blocked by the late-WB guard, but a move made while the
    white balance was in camera reference (late) mode (`preset` 4) cannot be
    fully restored: every tier restores through darktable's sliders, which
    force preset 2, so the coefficients come back WITHOUT the late D65
    correction. Say so rather than let a verified ratio read as the old look."""
    if (before_params or {}).get("preset") != WB_PRESET_D65_LATE:
        return
    after = wb_preset(entry.path, entry.instance)
    restored = after == WB_PRESET_D65_LATE
    undone["late_correction_restored"] = restored
    if not restored:
        warnings.append(
            f"seq {entry.seq} was made while white balance was in \"camera reference (late)\" "
            f"mode (preset 4); the coefficients are restored, but darktable's sliders cannot "
            f"switch that mode back on (preset now {after}), so the late D65 correction is "
            "still off and the colours will not match the pre-move look. To get it back, "
            "step back in darktable's own history stack (select the item before this "
            "move, or Ctrl-Z) -- that restores the stored preset."
        )


def _refuse_temperature_undo(entry: journal.Entry, before_params: dict | None) -> None:
    """Tier 4 (brief step 6): the honest refusal, carrying the journalled
    coefficient vector, the target Kelvin (when a table happens to answer
    one even though sliders are not pinned), and the exact fix."""
    camera = _temperature_camera(entry)
    target_kelvin, _ = _temperature_target_kelvin(before_params, camera)
    raise _UnsupportedUndo(
        f"cannot undo seq {entry.seq}: temperature's coefficient sliders are not pinned on "
        "this host, and no Kelvin calibration table lets Scouter compute a safe restore",
        detail={
            "seq": entry.seq,
            "module": entry.module,
            "coefficients": before_params,
            "target_kelvin": target_kelvin,
            "hint": (
                "darktable stores white balance as camera-relative coefficients; run "
                "`scouter wb calibrate` to make this undoable, or restore these coefficients "
                "in the module's coefficients tab"
            ),
        },
    )


def _undo_one(*, dry_run: bool = False, job: str | None = None) -> UndoResult:
    """Revert the newest Scouter write in this job by re-setting its old value."""
    job = job or current_job()
    entry = journal.latest_open(job)
    if entry is None:
        # Fix round 2, should-fix 4: `latest_open` already skips a failed
        # write (it never happened, so there is nothing to undo), but "the
        # journal is empty" and "the newest write in it failed" are different
        # facts -- name the failed one when that is why there is nothing here.
        failed = journal.latest_failed(job)
        if failed is not None:
            # Fix round 3, nit: a real (non-dry-run) call reports this as an
            # actual error outcome, not "dry_run" -- there is no plan here to
            # preview, the newest write in the job simply never took effect.
            return UndoResult(
                outcome="dry_run" if dry_run else "error",
                seq=failed.seq,
                reason=(
                    f"nothing to undo: seq {failed.seq} ({failed.verb} on "
                    f"{failed.module}.{failed.param or ''}) failed ({failed.outcome}) "
                    "and never took effect"
                ),
            )
        # Row 408, B2: this used to be unconditionally "dry_run" -- so a
        # REAL (non-dry) undo call with an empty journal reported
        # `outcome: dry_run` / `ok: true`, indistinguishable from a genuine
        # preview. `registry.call` counts both "ok" and "dry_run" as
        # success, so a real call that changed nothing looked identical to
        # one that worked. Mirrors the `failed` branch just above: "dry_run"
        # only when this call actually WAS a dry run; a real call gets
        # "noop", which `ok = outcome in ("ok", "dry_run")` correctly
        # excludes from success.
        # Review round 1, should-fix (row 408 B2): named the REAL cause, not
        # just the symptom -- every MCP call got its own job id
        # (`paths.current_job()`: `job-<UTC second>-<pid>`) unless the
        # caller pinned one, so the journal a fresh call's undo_last looked
        # at was always empty. Phase 3 task 9's MCP session job
        # (`scouter/mcp/server.py::_pin_session_job`) closes that for good
        # by pinning `SCOUTER_JOB` once per server process, so this now
        # only fires for a bare CLI/Python call that never pins
        # `SCOUTER_JOB`/`job=` across calls, or a genuinely empty journal.
        return UndoResult(
            outcome="dry_run" if dry_run else "noop",
            reason=(
                f"nothing to undo: journal for {job} is empty -- a bare CLI/Python "
                "call gets a fresh job per call unless $SCOUTER_JOB (CLI) or job= "
                "(MCP/Python) is pinned across calls; the MCP server already pins "
                "one for you (phase 3 task 9's session job)"
            ),
        )

    # Blocker 6 / fix round 3, item 1: a `module.reset` entry can never be
    # undone. A dry run reports that honestly WITHOUT mutating the journal --
    # `mark_undone(note=...)` runs only on a real call, after this branch, so
    # `undo_last(dry_run=True)` is pure (previewing must never have side
    # effects). Should-fix 5: the real call marks the entry undone (with a
    # note, not a real undo) so this refusal happens once per reset and the
    # write underneath it stays reachable on the next call.
    if entry.verb == "module.reset":
        reason = (
            f"cannot undo the reset of {entry.module}: its parameters were discarded. "
            "darktable's own Ctrl-Z is the only way back"
        )
        if dry_run:
            return UndoResult(
                outcome="dry_run",
                image_id=entry.image_id,
                path=entry.path,
                seq=entry.seq,
                reason=f"{reason} (this entry would be skipped, not undone)",
            )
        journal.mark_undone(job, entry.seq, note="skipped: reset is not undoable")
        raise _UnsupportedUndo(reason, detail={"seq": entry.seq, "module": entry.module})

    # Phase 3 task 8 (design §8.4): a `preset.apply` entry can never be
    # undone either -- it is a history APPEND (darktable's Lua API has no
    # way to remove history items), the same honest limit as `module.reset`
    # above, and refused the same way: marked undone with a note on a real
    # call (so this refusal fires once per application and the write
    # underneath it stays reachable), never mutated on a dry run.
    if entry.verb == "preset.apply":
        before_end = entry.before.get("history_end")
        after_end = (entry.after or {}).get("history_end")
        items = (
            after_end - before_end
            if isinstance(before_end, int) and isinstance(after_end, int)
            else "some"
        )
        # Fix round 1 (controller ruling, spec over plan): this text must
        # match spec §8.4 exactly -- no "cannot undo preset '<name>':" prefix
        # (the name still travels in `detail["style"]` below, just not in
        # the sentence itself).
        reason = (
            f"a style application appended {items} history item(s); darktable's Lua API "
            "cannot remove history items. In darktable, click the history item just below "
            "the style's first item and compress history, or press Ctrl-Z"
        )
        if dry_run:
            return UndoResult(
                outcome="dry_run",
                image_id=entry.image_id,
                path=entry.path,
                seq=entry.seq,
                reason=f"{reason} (this entry would be skipped, not undone)",
            )
        journal.mark_undone(job, entry.seq, note="skipped: a style application is not undoable")
        raise _UnsupportedUndo(
            reason, detail={"seq": entry.seq, "style": (entry.after or {}).get("style")}
        )

    plan_entry = {
        "action": "darkroom.undo_last",
        "seq": entry.seq,
        "verb": entry.verb,
        "module": entry.module,
        "param": entry.param,
        "restore_to": entry.before.get("value"),
    }
    if entry.module == "temperature":
        # Should-fix (task 11 fix round 1): a dry run on a temperature entry
        # names the journalled coefficient vector (the one thing every tier
        # would restore from) and predicts WHICH tier a real call would
        # take, without sending anything -- the same "no darktable calls
        # were made" promise every other dry run here keeps.
        before_params = entry.before.get("params")
        plan_entry["coefficients"] = before_params
        if before_params and _temperature_sliders_pinned():
            plan_entry["tier"] = "coefficient_sliders"
        elif before_params:
            camera = _temperature_camera(entry)
            target_k, _ = _temperature_target_kelvin(before_params, camera)
            plan_entry["tier"] = "kelvin_inversion" if target_k is not None else "refuse"
        else:
            plan_entry["tier"] = "refuse"
    plan = [plan_entry]
    if dry_run:
        return UndoResult(
            outcome="dry_run",
            image_id=entry.image_id,
            path=entry.path,
            seq=entry.seq,
            plan=plan,
            reason="dry run: no action was sent to darktable",
        )

    # Blocker 1: verify Scouter is about to act on the image it journaled --
    # by READING, never by force-switching. `darkroom.current` has no side
    # effects; if it reports a different (or no) image loaded, undo_last
    # refuses rather than silently switching the darkroom out from under
    # whatever Pratik is looking at.
    current = dtbridge.call("darkroom.current", {})
    current_id = current.get("image_id")
    if current_id is None:
        raise NotInDarkroom(
            f"no image is loaded in the darkroom (expected image {entry.image_id})",
            detail={"expected_image_id": entry.image_id, "view": current.get("view")},
        )
    _require_job_image(job, int(current_id))
    if int(current_id) != entry.image_id:
        raise NotInDarkroom(
            f"another image is loaded: {int(current_id)}",
            detail={
                "expected_image_id": entry.image_id,
                "loaded_image_id": int(current_id),
                "view": current.get("view"),
            },
        )
    dtbridge.require_darkroom()

    if entry.verb == "module.enable":
        was_enabled_before = entry.before.get("enabled")
        if was_enabled_before:
            # Blocker 9 (should-fix): the module was already enabled before
            # Scouter's own `enable` call -- that call was a no-op, so undoing
            # it is a no-op too, not a blind `disable` of something the user
            # (not Scouter) turned on.
            committed = False
            warnings = [f"{entry.module} was already enabled before this write; nothing to undo"]
            verified = True
        else:
            dtbridge.call("module.disable", {"module": entry.module, "instance": entry.instance})
            committed, warnings, _noop = _commit_with_readback(
                job,
                entry.image_id,
                xmp_path=str(xmp.sidecar_path(entry.path)),
                confirm=lambda: _module_enabled(entry.path, entry.module, entry.instance) is False,
                what=f"{entry.module} (disable)",
            )
            verified = _module_enabled(entry.path, entry.module, entry.instance) is False
            if not verified:
                warnings.append(
                    f"could not verify {entry.module} was disabled after undo (seq {entry.seq})"
                )
        undone = {
            "verb": entry.verb,
            "module": entry.module,
            "param": entry.param,
            "restored_to": None,
            "verified": verified,
        }
        mark_undone_ok = True
    elif entry.module == "temperature":
        # Task 11: tiers 2/3/4 (design 11.3/11.4/11.5), tried in order.
        # `before_params` is `None` for a journal entry written before this
        # task landed (or when the module never decoded) -- that is tier 4
        # too, with no coefficients to hand over.
        before_params = entry.before.get("params")
        if before_params and _temperature_sliders_pinned():
            committed, warnings, undone = _undo_temperature_via_sliders(job, entry, before_params)
            # Brief step 4: a component that does not come back leaves the
            # entry `undone: false` -- unlike every other undo here, a
            # partially-verified coefficient restore is not marked done, so
            # the failure stays visible and reachable on the next call.
            mark_undone_ok = bool(undone["verified"])
        else:
            camera = _temperature_camera(entry)
            via_kelvin = (
                _undo_temperature_via_kelvin(job, entry, before_params, camera)
                if before_params and camera
                else None
            )
            if via_kelvin is None:
                _refuse_temperature_undo(entry, before_params)
                raise AssertionError("unreachable: _refuse_temperature_undo always raises")
            # Fix round 1, blocker 2: `_undo_temperature_via_kelvin` itself
            # raises `_UndoUnverified` (propagating past this whole function,
            # never reaching `mark_undone` below) when the ratio does not
            # verify -- a normal return here is always a verified tier 3.
            committed, warnings, undone = via_kelvin
            mark_undone_ok = True
        _warn_late_not_restored(entry, before_params, undone, warnings)
    else:
        mark_undone_ok = True
        previous = entry.before.get("value")
        if previous is None:
            raise ScouterError(
                f"cannot undo seq {entry.seq}: the previous value of "
                f"{entry.module}.{entry.param} was never readable from the XMP",
                fix="press Ctrl-Z in darktable, or set the value you want explicitly",
                detail={"seq": entry.seq},
            )
        def _restored() -> bool:
            return _close(
                _effective_read(entry.path, entry.module, entry.param, entry.instance),
                float(previous),
            )

        # Row 433, night-1 C (`cov-colorbalancergb.contrast-*-repeat_adjust`):
        # when the sidecar already shows the value being restored (the write
        # being undone never reached it), darktable will write nothing --
        # predict that, and spend one bounce instead of a 5.4 s timeout. Not
        # for a `commit=False` entry: its value is live in darktable while
        # the sidecar is stale, so the sidecar proves nothing.
        predicted = entry.commit is not False and _restored()
        dtbridge.action(
            dtbridge.action_path(entry.module, entry.param),
            instance=entry.instance,
            element="value",
            effect="set",
            value=float(previous),
        )
        committed, warnings, _noop = _commit_with_readback(
            job,
            entry.image_id,
            xmp_path=str(xmp.sidecar_path(entry.path)),
            confirm=_restored,
            what=f"{entry.module}.{entry.param} (undo)",
            predicted_noop=predicted,
        )
        # Blocker 5: verify from the XMP after the commit. `restored_to` is
        # what was actually read back, not the value that was requested.
        actual = _effective_read(entry.path, entry.module, entry.param, entry.instance)
        verified = _close(actual, previous)
        if not verified:
            warnings.append(
                f"could not verify the restore for seq {entry.seq}: read back {actual!r}, "
                f"expected {previous!r}"
            )
        undone = {
            "verb": entry.verb,
            "module": entry.module,
            "param": entry.param,
            "restored_to": actual,
            "verified": verified,
        }

    if mark_undone_ok:
        journal.mark_undone(job, entry.seq)
    return UndoResult(
        image_id=entry.image_id,
        path=entry.path,
        seq=entry.seq,
        undone=undone,
        committed=committed,
        warnings=warnings,
    )


@metrics.instrumented("darkroom.undo_last")
def undo_last(*, single: bool = False, dry_run: bool = False, job: str | None = None) -> UndoResult:
    """Revert the newest Scouter MOVE in this job (phase 3 §6.4).

    A `darkroom.say` phrase is one move of several writes sharing a journal
    `group`; every other write is a move of one, so phase 1/2 behaviour is
    unchanged. `single=True` reverts only the newest write.

    Controller ruling: the group loop must terminate. The group's member
    entries are computed ONCE, up front, and the loop iterates over that
    fixed list -- it never re-queries `journal.latest_open` to decide
    whether to keep going, so a member whose own `_undo_one` call leaves it
    still open (an unverified temperature restore, tier 2, is the real
    case) cannot spin forever. The loop stops the first time that happens
    and reports `outcome="partial"`, naming what did and did not undo.
    """
    job = job or current_job()
    newest = journal.latest_open(job)
    if single or newest is None or newest.group is None:
        return _undo_one(dry_run=dry_run, job=job)
    group = newest.group
    members = [e for e in reversed(journal.read_all(job))
               if e.group == group and not e.undone
               and not (e.outcome or "").startswith("error")]
    if dry_run:
        return UndoResult(
            outcome="dry_run", image_id=newest.image_id, path=newest.path, seq=newest.seq,
            plan=[{"action": "darkroom.undo_last", "seq": e.seq, "module": e.module,
                   "param": e.param, "restore_to": e.before.get("value")} for e in members],
            reason=f"would undo {len(members)} write(s) of move {group}, newest first",
        )
    results: list[dict] = []
    for member in members:
        try:
            result = _undo_one(dry_run=False, job=job)
        except ScouterError as e:
            if not results:
                raise
            return UndoResult(
                outcome="partial", image_id=newest.image_id, path=newest.path, seq=newest.seq,
                undone=results[0].get("undone"), undone_group=results,
                # Fix round 1, controller ruling: report what the members
                # that DID undo actually committed/warned, the same as the
                # success path -- not a hard-coded `False`/`[]` that throws
                # away real information about the partial success.
                committed=all(r.get("committed") for r in results),
                warnings=[w for r in results for w in r.get("warnings", [])],
                reason=f"undid {len(results)} of move {group}, then failed: {e.code}: {e.message}",
            )
        results.append(result.to_dict())
        # Bounded-loop guard (controller ruling): `_undo_one` is trusted to
        # have undone `member` (the newest open entry when it ran, which --
        # unless something else concurrently touched the journal -- is
        # `member` itself). If it somehow left the entry open, re-reading
        # the journal proves it rather than trusting the return value, and
        # the loop stops here instead of retrying the same entry forever.
        still_open = any(e.seq == member.seq and not e.undone for e in journal.read_all(job))
        if still_open:
            return UndoResult(
                outcome="partial", image_id=newest.image_id, path=newest.path, seq=newest.seq,
                undone=results[0].get("undone"), undone_group=results,
                committed=all(r.get("committed") for r in results),
                warnings=[w for r in results for w in r.get("warnings", [])],
                reason=(f"undid {len(results)} of move {group}, then seq {member.seq} "
                        f"({member.module}.{member.param}) stayed open after its own undo"),
            )
    return UndoResult(
        image_id=newest.image_id, path=newest.path, seq=newest.seq,
        undone=results[0].get("undone") if results else None, undone_group=results,
        committed=all(r.get("committed") for r in results),
        warnings=[w for r in results for w in r.get("warnings", [])],
    )


@metrics.instrumented("darkroom.status")
def status(
    module: str | None = None,
    param: str | None = None,
    *,
    instance: int = 0,
    job: str | None = None,
) -> StatusResult:
    """Slider POSITIONS (normalised 0..1), never values (design 7.3)."""
    view = dtbridge.current_view()
    positions: dict[str, float | None] = {}
    nan: dict[str, bool] = {}
    if module is None:
        return StatusResult(
            view=view,
            positions=positions,
            nan=nan,
            warnings=["no module given: only the view was queried"],
        )
    pairs = (
        [(module, param)] if param else [(m, p) for (m, p) in dtbridge.ACTION_PATHS if m == module]
    )
    if not pairs:
        # Fix round 1, minor f: `status(module="nosuchmodule")` with no
        # `param` used to return an EMPTY positions/nan dict and the generic
        # "positions are normalised" warning -- indistinguishable from a
        # module that exists and happens to have nothing readable, and the
        # exact opposite of what the same unpinned module gets when a `param`
        # IS given (`action_path`'s typed refusal naming the pinned modules).
        # One unpinned module, one answer. `action_path` composes the message,
        # so there is no second copy of it here to drift.
        dtbridge.action_path(module, param or "")
    for mod, par in pairs:
        path = dtbridge.action_path(mod, par)
        result = dtbridge.call("darkroom.status", {"path": path, "instance": instance})
        key = f"{mod}.{par}"
        positions[key] = result.get("position")
        nan[key] = bool(result.get("nan"))
    return StatusResult(
        view=view,
        positions=positions,
        nan=nan,
        warnings=[
            "positions are normalised slider positions, not parameter values; "
            "read values with `darkroom get` or `history read` (design 7.3)"
        ],
    )


@metrics.instrumented("darkroom.describe_history")
def describe_history(image: str | int | None = None, *, job: str | None = None) -> HistoryResult:
    """The darkroom image's history, as darktable committed it.

    Delegates to `scouter.core.history.read` (task 8). `.__wrapped__` (falling
    back to the wrapped function if absent) calls the undecorated function so
    this verb still emits exactly one metrics event, not two (blocker 13,
    same seam as `_commit`).
    """
    from scouter.core import history

    read_fn = getattr(history.read, "__wrapped__", history.read)
    target = image if image is not None else _current_image().image_id
    return read_fn(target, job=job)


IMAGE_ARG = ArgSpec(
    "image",
    "image",
    required=True,
    positional=True,
    help="absolute path to the raw, or a darktable image id",
)
MODULE_ARG = ArgSpec(
    "module", "str", required=True, positional=True, help="darktable module name, e.g. exposure"
)
PARAM_ARG = ArgSpec(
    "param",
    "str",
    required=True,
    positional=True,
    help="parameter name, e.g. exposure / contrast / temperature",
)
INSTANCE_ARG = ArgSpec("instance", "int", default=0, help="module instance (multi_priority)")
DRY_RUN_ARG = ArgSpec("dry_run", "bool", default=False, help="plan only, send nothing")
COMMIT_ARG = ArgSpec(
    "commit", "bool", default=True, help="commit history to the XMP after the write"
)

for verb in (
    Verb(
        id="darkroom.open",
        group="darkroom",
        fn=open_image,
        args=(IMAGE_ARG,),
        writes=False,
        gated=False,
        summary="Load an image in the darkroom.",
    ),
    Verb(
        id="darkroom.set",
        group="darkroom",
        fn=set_param,
        args=(
            MODULE_ARG,
            PARAM_ARG,
            ArgSpec(
                "value",
                "float",
                required=True,
                positional=True,
                help="absolute value in the slider's own unit",
            ),
            INSTANCE_ARG,
            COMMIT_ARG,
            DRY_RUN_ARG,
        ),
        writes=True,
        gated=False,
        summary="Set one module parameter to an absolute value.",
    ),
    Verb(
        id="darkroom.nudge",
        group="darkroom",
        fn=nudge,
        args=(
            MODULE_ARG,
            PARAM_ARG,
            ArgSpec("steps", "int", default=1, help="steps up (+) or down (-)"),
            INSTANCE_ARG,
            COMMIT_ARG,
            DRY_RUN_ARG,
        ),
        writes=True,
        gated=False,
        summary="Move a slider by darktable's own default step.",
    ),
    Verb(
        id="darkroom.enable",
        group="darkroom",
        fn=enable,
        args=(MODULE_ARG, INSTANCE_ARG, DRY_RUN_ARG),
        writes=True,
        gated=False,
        summary="Enable a module.",
    ),
    Verb(
        id="darkroom.reset",
        group="darkroom",
        fn=reset,
        args=(
            MODULE_ARG,
            INSTANCE_ARG,
            DRY_RUN_ARG,
            ArgSpec(
                "confirm",
                "bool",
                default=False,
                help="required: reset discards this module's parameters",
            ),
        ),
        writes=True,
        gated=True,
        summary="Reset a module to its defaults.",
    ),
    Verb(
        id="darkroom.undo_last",
        group="darkroom",
        fn=undo_last,
        args=(
            ArgSpec("single", "bool", default=False,
                    help="undo only the newest write, not the whole move it was part of"),
            DRY_RUN_ARG,
        ),
        writes=True,
        gated=False,
        summary="Revert the newest Scouter move in this job (a darkroom_say phrase is one move).",
    ),
    Verb(
        id="darkroom.status",
        group="darkroom",
        fn=status,
        args=(
            ArgSpec("module", "str", positional=True, help="module to query"),
            ArgSpec("param", "str", positional=True, help="parameter to query"),
            INSTANCE_ARG,
        ),
        writes=False,
        gated=False,
        summary="Slider positions and the current view.",
    ),
    Verb(
        id="darkroom.describe_history",
        group="darkroom",
        fn=describe_history,
        args=(ArgSpec("image", "image", help="defaults to the darkroom image", positional=True),),
        writes=False,
        gated=False,
        summary="The darkroom image's committed history.",
    ),
):
    register(verb)
