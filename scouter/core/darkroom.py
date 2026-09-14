"""The live darkroom verbs. Every write is one darktable history item."""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from scouter import metrics, paths
from scouter.core import dtbridge, journal, xmp
from scouter.errors import CommitTimeout, NotInDarkroom, ScouterError, UsageError
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
            "re-open the image you meant (`scouter darkroom open <image>`), or "
            "start a new job (--job / $SCOUTER_JOB)"
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
) -> tuple[bool, list[str]]:
    """`_commit`, with a commit timeout resolved by reading the XMP back.

    Final review, should-fix 6: `darkroom.set` already did this, and every
    other writer did not -- so a write whose journal entry says `completed`
    and whose value is already on disk came back as `commit_timeout`.
    darktable writes a sidecar only when the history actually CHANGES, so a
    write that asks for the state the image already has legitimately produces
    no write at all. `confirm()` re-reads the sidecar; if it agrees with what
    was asked for, the request is satisfied and says so in a warning. If it
    does not, the timeout is re-raised untouched -- a write that never landed
    is never reported as committed.

    `xmp_path` (fix round 1, should-fix 3): the CALLER's already-resolved
    sidecar path, so watermarking a readback-resolved commit costs no extra
    `darkroom.current` round-trip -- every caller already has this path from
    resolving its own image.
    """
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
        from scouter.core import history

        history.watermark_write(job, image_id, xmp_path)
        return True, [
            f"darktable wrote no new sidecar for {what}: the state requested is the "
            "one the XMP already carried, so there was no history change to write. "
            "Confirmed by reading it back from the sidecar."
        ]
    return result.committed, list(result.warnings)


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
        source="xmp",
        warnings=[
            f"no-op: {module}.{param} is already {raw_state.get('value')} in the sidecar, "
            "which Scouter last committed; nothing was sent to darktable"
        ],
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

    A dry run makes no darktable calls at all -- not even to learn which image
    is in the darkroom (fix round 1, blocker 14) -- so it reports only the
    action that would be sent, never a value or an image id.
    """
    job = job or current_job()
    value = _require_finite("value", value)
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
                    "path": dtbridge.action_path(module, param),
                    "effect": "set",
                    "value": value,
                    "instance": instance,
                }
            ],
            reason="dry run: no darktable calls were made; the darkroom image was not resolved",
        )
    resolved = _current_image()
    _require_job_image(job, resolved.image_id)
    dtbridge.require_darkroom()
    raw_state, before_state, pending_uncommitted = _raw_and_before_state(
        job, resolved.image_id, resolved.path, module, param, instance
    )
    shortcut = _noop_shortcut(
        job,
        resolved,
        module,
        param,
        instance,
        None if pending_uncommitted else value,
        raw_state,
        requested=value,
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
        and _close(value, raw_state.get("value"))
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
            dtbridge.action_path(module, param),
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
    applied = None
    warnings: list[str] = []
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
        # as committed.
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
            applied = _read_value(resolved.path, module, param, instance)
            if _close(applied, value):
                from scouter.core import history as _history

                _history.watermark_write(job, resolved.image_id, resolved.xmp_path)
                committed = True
                warnings.extend(commit_result.warnings)
                warnings.append(
                    f"readback confirmed {module}.{param} already carries {value:g}: "
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
                    applied = _read_value(resolved.path, module, param, instance)
                if _close(applied, value):
                    committed = True
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
            applied = _read_value(resolved.path, module, param, instance)
        if applied is None:
            warnings.append(
                f"{module}.{param} committed but could not be decoded from the XMP; "
                "`requested` is what was sent, `applied` is unknown"
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

        committed, warnings = _commit_with_readback(
            job,
            resolved.image_id,
            xmp_path=resolved.xmp_path,
            confirm=_confirm_nudge,
            what=f"{module}.{param}",
        )
        applied = _read_value(resolved.path, module, param, instance)
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
        if expected_enabled is None:
            return False
        return _module_enabled(resolved.path, module, instance) is expected_enabled

    committed, warnings = _commit_with_readback(
        job,
        resolved.image_id,
        xmp_path=resolved.xmp_path,
        confirm=_confirm_module,
        what=f"{module} ({action_name})",
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

    `None` when either coefficient is missing, or when blue is zero or
    non-finite: the ratio does not exist, and 0.0 would be a lie about it.
    """
    if not params:
        return None
    r, b = params.get("coeff_r"), params.get("coeff_b")
    if r is None or not b:
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

    committed, warnings = _commit_with_readback(
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

    committed, warnings = _commit_with_readback(
        job,
        entry.image_id,
        xmp_path=str(xmp.sidecar_path(entry.path)),
        confirm=_confirm,
        what="temperature (undo via Kelvin inversion)",
    )
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


@metrics.instrumented("darkroom.undo_last")
def undo_last(*, dry_run: bool = False, job: str | None = None) -> UndoResult:
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
        return UndoResult(
            outcome="dry_run",
            reason=f"nothing to undo: journal for {job} is empty",
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
            committed, warnings = _commit_with_readback(
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
        dtbridge.action(
            dtbridge.action_path(entry.module, entry.param),
            instance=entry.instance,
            element="value",
            effect="set",
            value=float(previous),
        )
        committed, warnings = _commit_with_readback(
            job,
            entry.image_id,
            xmp_path=str(xmp.sidecar_path(entry.path)),
            confirm=lambda: _close(
                _read_value(entry.path, entry.module, entry.param, entry.instance), float(previous)
            ),
            what=f"{entry.module}.{entry.param} (undo)",
        )
        # Blocker 5: verify from the XMP after the commit. `restored_to` is
        # what was actually read back, not the value that was requested.
        actual = _read_value(entry.path, entry.module, entry.param, entry.instance)
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
            "read values with `history read` (design 7.3)"
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
        args=(DRY_RUN_ARG,),
        writes=True,
        gated=False,
        summary="Revert the newest Scouter write in this job.",
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
