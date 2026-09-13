"""The live darkroom verbs. Every write is one darktable history item."""

from __future__ import annotations

import time

from scouter import metrics
from scouter.core import dtbridge, journal, xmp
from scouter.errors import CommitTimeout, NotInDarkroom, ScouterError
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


def _commit(job: str, image_id: int, *, strategy: str = "auto") -> CommitResult:
    """Every write's post-action commit, routed through one seam (ruling 21).

    Imports `scouter.core.history` lazily so `darkroom.py` stays importable
    (and `registry.load_verbs()` keeps working) whether or not it has landed
    yet. Calls the *unwrapped* `history.commit` (fix round 1, blocker 13) so
    one `darkroom.*` verb call still produces exactly one metrics event, not
    two -- `history.commit` is itself `@instrumented`. Falls back to the
    wrapped function if `__wrapped__` is ever absent, so this never breaks
    outright if that decorator detail changes.
    """
    from scouter.core import history

    commit_fn = getattr(history.commit, "__wrapped__", history.commit)
    return commit_fn(image_id, timeout_s=5.0, strategy=strategy, job=job)


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


def _read_state(path: str, module: str, param: str, instance: int) -> dict:
    """Everything the journal needs about one slider's current state

    (fix round 1, blocker 7): not just `value`, but `enabled` and
    `history_end` too, so a future reader can tell "this slider was disabled"
    or "this was a stale read" apart from a plain missing value.
    """
    state = _module_state(path, module, instance)
    return {**state, "value": _read_value(path, module, param, instance)}


def _last_journal_write(job: str, image_id: int, module: str, param: str,
                        instance: int) -> journal.Entry | None:
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
        if (entry.verb == "module.reset" and not is_error and entry.image_id == image_id
                and entry.module == module and entry.instance == instance):
            return None
        if entry.undone:
            continue
        if is_error:
            continue
        if (entry.image_id == image_id and entry.module == module and entry.param == param
                and entry.instance == instance and entry.after and "value" in entry.after):
            return entry
    return None


def _before_state(job: str, image_id: int, path: str, module: str, param: str,
                  instance: int) -> dict:
    """The `before` state for a new write on this slider.

    Ordinarily this is whatever the XMP currently shows. But when the last
    (live, landed) write to this exact (image, module, param, instance) used
    `commit=False`, darktable has not written the sidecar for it yet -- the
    XMP still shows whatever was true before THAT write, not Scouter's own
    true current value, so the journal's own `after` is used instead. This
    fallback applies only when that last write's own `commit` flag was False
    (fix round 2, blocker 2 -- not a "does the XMP look stale" guess, which
    fix round 1 used and which a coincidental value match could fool): a
    `commit=True` write's own `history.commit` call already waited for the
    XMP to advance, so its value is trustworthy and no fallback is needed.
    """
    state = _read_state(path, module, param, instance)
    last = _last_journal_write(job, image_id, module, param, instance)
    if last is not None and last.commit is False:
        state = {**state, "value": last.after["value"]}
    return state


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
    key = {"image_id": int(image)} if str(image).isdigit() else {"path": str(image)}
    result = dtbridge.call("darkroom.open", key)
    return OpenResult(
        image_id=int(result["image_id"]),
        path=str(result["path"]),
        view=str(result.get("view", "unknown")),
        opened=bool(result.get("opened")),
        already_open=bool(result.get("already_open")),
    )


@metrics.instrumented("darkroom.set")
def set_param(module: str, param: str, value: float, *, instance: int = 0,
              commit: bool = True, dry_run: bool = False,
              job: str | None = None) -> SetResult:
    """Set one module parameter to an absolute value (phase 0 finding 1).

    A dry run makes no darktable calls at all -- not even to learn which image
    is in the darkroom (fix round 1, blocker 14) -- so it reports only the
    action that would be sent, never a value or an image id.
    """
    job = job or current_job()
    if dry_run:
        return SetResult(
            outcome="dry_run", module=module, param=param, instance=instance, requested=value,
            plan=[{"action": "darkroom.set", "path": dtbridge.action_path(module, param),
                   "effect": "set", "value": value, "instance": instance}],
            reason="dry run: no darktable calls were made; the darkroom image was not resolved",
        )
    resolved = _current_image()
    dtbridge.require_darkroom()
    before_state = _before_state(job, resolved.image_id, resolved.path, module, param, instance)
    entry = journal.begin(
        job, verb="darkroom.set", image_id=resolved.image_id, path=resolved.path,
        module=module, param=param, instance=instance, before=before_state, commit=commit,
    )
    try:
        dtbridge.action(
            dtbridge.action_path(module, param),
            instance=instance, element="value", effect="set", value=value,
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
        commit_result = None
        commit_error: CommitTimeout | None = None
        try:
            commit_result = _commit(job, resolved.image_id, strategy="auto")
        except CommitTimeout as e:
            commit_error = e
        applied = _read_value(resolved.path, module, param, instance)
        if commit_result is not None:
            committed = commit_result.committed
            warnings.extend(commit_result.warnings)
        elif _close(applied, value):
            committed = True
            warnings.append(
                f"darktable wrote no new sidecar for {module}.{param}: the value requested "
                f"({value:g}) is the one the XMP already carried, so there was no history "
                "change to write. Confirmed by reading the value back from the sidecar."
            )
        else:
            assert commit_error is not None
            raise commit_error
        if applied is None:
            warnings.append(
                f"{module}.{param} committed but could not be decoded from the XMP; "
                "`requested` is what was sent, `applied` is unknown"
            )
    return SetResult(
        image_id=resolved.image_id, path=resolved.path, module=module, param=param,
        instance=instance, requested=value, applied=applied, previous=before_state.get("value"),
        committed=committed, warnings=warnings,
    )


@metrics.instrumented("darkroom.nudge")
def nudge(module: str, param: str, steps: int = 1, *, instance: int = 0,
          commit: bool = True, dry_run: bool = False,
          job: str | None = None) -> SetResult:
    """Move a slider by `steps` of darktable's own default step (+/-).

    Journals *before* the loop, not after (fix round 1, blocker 2): a crash
    partway through would otherwise leave a write darktable actually applied
    with no journal trace at all. `steps_landed` records how many of `steps`
    actually reached the bridge; `undo_last` always reverts to `before`
    regardless of how many landed, so a partial nudge is still fully undoable.
    """
    job = job or current_job()
    path_ = dtbridge.action_path(module, param)
    effect = "up" if steps >= 0 else "down"
    if dry_run:
        return SetResult(
            outcome="dry_run", module=module, param=param, instance=instance,
            plan=[{"action": "darkroom.nudge", "path": path_, "effect": effect,
                   "repeats": abs(steps), "instance": instance}],
            reason="dry run: no darktable calls were made; the darkroom image was not resolved",
        )
    resolved = _current_image()
    dtbridge.require_darkroom()
    before_state = _before_state(job, resolved.image_id, resolved.path, module, param, instance)
    entry = journal.begin(
        job, verb="darkroom.nudge", image_id=resolved.image_id, path=resolved.path,
        module=module, param=param, instance=instance, before=before_state, commit=commit,
    )
    landed = 0
    last_status: float | None = None
    try:
        for _ in range(abs(steps)):
            result = dtbridge.action(path_, instance=instance, element="value",
                                     effect=effect, value=1)
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
        commit_result = _commit(job, resolved.image_id, strategy="auto")
        committed, warnings = commit_result.committed, list(commit_result.warnings)
        applied = _read_value(resolved.path, module, param, instance)
    return SetResult(
        image_id=resolved.image_id, path=resolved.path, module=module, param=param,
        instance=instance, requested=None, applied=applied, previous=before_state.get("value"),
        committed=committed, warnings=warnings,
    )


def _do_module_action(action_name: str, method: str, *, module: str, instance: int,
                      dry_run: bool, confirm: bool, job: str | None) -> ModuleResult:
    job = job or current_job()
    gated = action_name == "reset"
    plan = [{"action": f"module.{action_name}", "module": module, "instance": instance}]
    if dry_run or (gated and not confirm):
        return ModuleResult(
            outcome="dry_run", module=module, action=action_name, instance=instance, plan=plan,
            reason=(
                "reset discards this module's parameters and cannot be undone by "
                "Scouter (design 8.2); re-run with --yes / confirm=true"
                if gated and not confirm
                else "dry run: no darktable calls were made; the darkroom image was not resolved"
            ),
        )
    resolved = _current_image()
    dtbridge.require_darkroom()
    before_state = _module_state(resolved.path, module, instance)
    entry = journal.begin(
        job, verb=f"module.{action_name}", image_id=resolved.image_id, path=resolved.path,
        module=module, param="", instance=instance, before=before_state,
    )
    try:
        dtbridge.call(method, {"module": module, "instance": instance})
    except Exception as e:
        code = e.code if isinstance(e, ScouterError) else "error"
        journal.fail(job, entry.seq, outcome=f"error:{code}")
        raise
    commit_result = _commit(job, resolved.image_id, strategy="auto")
    after_state = _module_state(resolved.path, module, instance)
    journal.complete(
        job, entry.seq, after={"action": action_name, "enabled": after_state["enabled"]}
    )
    return ModuleResult(
        image_id=resolved.image_id, path=resolved.path, module=module, action=action_name,
        instance=instance, enabled=after_state["enabled"],
        committed=commit_result.committed, warnings=list(commit_result.warnings),
    )


@metrics.instrumented("darkroom.enable")
def enable(module: str, *, instance: int = 0, dry_run: bool = False,
          job: str | None = None) -> ModuleResult:
    """Enable a module. Not gated -- `reset` is the only destructive verb here."""
    return _do_module_action("enable", "module.enable", module=module, instance=instance,
                             dry_run=dry_run, confirm=True, job=job)


@metrics.instrumented("darkroom.reset")
def reset(module: str, *, instance: int = 0, confirm: bool = False,
         dry_run: bool = False, job: str | None = None) -> ModuleResult:
    """Reset a module to its defaults. Gated: discards params, needs `confirm`."""
    return _do_module_action("reset", "module.reset", module=module, instance=instance,
                             dry_run=dry_run, confirm=confirm, job=job)


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
                outcome="dry_run" if dry_run else "error", seq=failed.seq,
                reason=(
                    f"nothing to undo: seq {failed.seq} ({failed.verb} on "
                    f"{failed.module}.{failed.param or ''}) failed ({failed.outcome}) "
                    "and never took effect"
                ),
            )
        return UndoResult(
            outcome="dry_run", reason=f"nothing to undo: journal for {job} is empty",
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
                outcome="dry_run", image_id=entry.image_id, path=entry.path, seq=entry.seq,
                reason=f"{reason} (this entry would be skipped, not undone)",
            )
        journal.mark_undone(job, entry.seq, note="skipped: reset is not undoable")
        raise _UnsupportedUndo(reason, detail={"seq": entry.seq, "module": entry.module})

    plan = [{"action": "darkroom.undo_last", "seq": entry.seq, "verb": entry.verb,
             "module": entry.module, "param": entry.param,
             "restore_to": entry.before.get("value")}]
    if dry_run:
        return UndoResult(outcome="dry_run", image_id=entry.image_id, path=entry.path,
                          seq=entry.seq, plan=plan,
                          reason="dry run: no action was sent to darktable")

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
    if int(current_id) != entry.image_id:
        raise NotInDarkroom(
            f"another image is loaded: {int(current_id)}",
            detail={"expected_image_id": entry.image_id, "loaded_image_id": int(current_id),
                    "view": current.get("view")},
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
            commit_result = _commit(job, entry.image_id, strategy="auto")
            committed = commit_result.committed
            warnings = list(commit_result.warnings)
            verified = _module_enabled(entry.path, entry.module, entry.instance) is False
            if not verified:
                warnings.append(
                    f"could not verify {entry.module} was disabled after undo (seq {entry.seq})"
                )
        undone = {"verb": entry.verb, "module": entry.module, "param": entry.param,
                  "restored_to": None, "verified": verified}
    else:
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
            instance=entry.instance, element="value", effect="set", value=float(previous),
        )
        commit_result = _commit(job, entry.image_id, strategy="auto")
        committed = commit_result.committed
        warnings = list(commit_result.warnings)
        # Blocker 5: verify from the XMP after the commit. `restored_to` is
        # what was actually read back, not the value that was requested.
        actual = _read_value(entry.path, entry.module, entry.param, entry.instance)
        verified = _close(actual, previous)
        if not verified:
            warnings.append(
                f"could not verify the restore for seq {entry.seq}: read back {actual!r}, "
                f"expected {previous!r}"
            )
        undone = {"verb": entry.verb, "module": entry.module, "param": entry.param,
                  "restored_to": actual, "verified": verified}

    journal.mark_undone(job, entry.seq)
    return UndoResult(
        image_id=entry.image_id, path=entry.path, seq=entry.seq,
        undone=undone, committed=committed, warnings=warnings,
    )


@metrics.instrumented("darkroom.status")
def status(module: str | None = None, param: str | None = None, *, instance: int = 0,
          job: str | None = None) -> StatusResult:
    """Slider POSITIONS (normalised 0..1), never values (design 7.3)."""
    view = dtbridge.current_view()
    positions: dict[str, float | None] = {}
    nan: dict[str, bool] = {}
    if module is None:
        return StatusResult(view=view, positions=positions, nan=nan,
                            warnings=["no module given: only the view was queried"])
    pairs = [(module, param)] if param else [
        (m, p) for (m, p) in dtbridge.ACTION_PATHS if m == module
    ]
    for mod, par in pairs:
        path = dtbridge.action_path(mod, par)
        result = dtbridge.call("darkroom.status", {"path": path, "instance": instance})
        key = f"{mod}.{par}"
        positions[key] = result.get("position")
        nan[key] = bool(result.get("nan"))
    return StatusResult(
        view=view, positions=positions, nan=nan,
        warnings=["positions are normalised slider positions, not parameter values; "
                  "read values with `history read` (design 7.3)"],
    )


@metrics.instrumented("darkroom.describe_history")
def describe_history(image: str | int | None = None, *,
                     job: str | None = None) -> HistoryResult:
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


IMAGE_ARG = ArgSpec("image", "image", required=True, positional=True,
                    help="absolute path to the raw, or a darktable image id")
MODULE_ARG = ArgSpec("module", "str", required=True, positional=True,
                     help="darktable module name, e.g. exposure")
PARAM_ARG = ArgSpec("param", "str", required=True, positional=True,
                    help="parameter name, e.g. exposure / contrast / temperature")
INSTANCE_ARG = ArgSpec("instance", "int", default=0, help="module instance (multi_priority)")
DRY_RUN_ARG = ArgSpec("dry_run", "bool", default=False, help="plan only, send nothing")
COMMIT_ARG = ArgSpec("commit", "bool", default=True,
                     help="commit history to the XMP after the write")

for verb in (
    Verb(id="darkroom.open", group="darkroom", fn=open_image, args=(IMAGE_ARG,),
         writes=False, gated=False, summary="Load an image in the darkroom."),
    Verb(id="darkroom.set", group="darkroom", fn=set_param,
         args=(MODULE_ARG, PARAM_ARG,
               ArgSpec("value", "float", required=True, positional=True,
                       help="absolute value in the slider's own unit"),
               INSTANCE_ARG, COMMIT_ARG, DRY_RUN_ARG),
         writes=True, gated=False,
         summary="Set one module parameter to an absolute value."),
    Verb(id="darkroom.nudge", group="darkroom", fn=nudge,
         args=(MODULE_ARG, PARAM_ARG,
               ArgSpec("steps", "int", default=1, help="steps up (+) or down (-)"),
               INSTANCE_ARG, COMMIT_ARG, DRY_RUN_ARG),
         writes=True, gated=False,
         summary="Move a slider by darktable's own default step."),
    Verb(id="darkroom.enable", group="darkroom", fn=enable,
         args=(MODULE_ARG, INSTANCE_ARG, DRY_RUN_ARG),
         writes=True, gated=False, summary="Enable a module."),
    Verb(id="darkroom.reset", group="darkroom", fn=reset,
         args=(MODULE_ARG, INSTANCE_ARG, DRY_RUN_ARG,
               ArgSpec("confirm", "bool", default=False,
                       help="required: reset discards this module's parameters")),
         writes=True, gated=True, summary="Reset a module to its defaults."),
    Verb(id="darkroom.undo_last", group="darkroom", fn=undo_last, args=(DRY_RUN_ARG,),
         writes=True, gated=False,
         summary="Revert the newest Scouter write in this job."),
    Verb(id="darkroom.status", group="darkroom", fn=status,
         args=(ArgSpec("module", "str", positional=True, help="module to query"),
               ArgSpec("param", "str", positional=True, help="parameter to query"),
               INSTANCE_ARG),
         writes=False, gated=False, summary="Slider positions and the current view."),
    Verb(id="darkroom.describe_history", group="darkroom", fn=describe_history,
         args=(ArgSpec("image", "image", help="defaults to the darkroom image",
                       positional=True),),
         writes=False, gated=False, summary="The darkroom image's committed history."),
):
    register(verb)
