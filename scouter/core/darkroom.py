"""The live darkroom verbs. Every write is one darktable history item."""

from __future__ import annotations

from scouter import metrics
from scouter.core import dtbridge, journal, xmp
from scouter.errors import ScouterError
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


def _commit(job: str, image_id: int, *, strategy: str = "auto") -> CommitResult:
    """Every write's post-action commit, routed through one seam (ruling 21).

    `scouter.core.history` is task 8's module and does not exist yet. Importing
    it here, inside the function body, keeps `darkroom.py` importable -- and
    `registry.load_verbs()` working -- before it lands: `load_verbs()` already
    tolerates a missing core module (`registry._CORE_MODULES`'s docstring), and
    this gives `darkroom.py`'s own call sites the same tolerance for the one
    history call they need. Once task 8 adds `scouter/core/history.py`, this
    import simply resolves -- nothing here changes.
    """
    from scouter.core import history

    return history.commit(image_id, timeout_s=5.0, strategy=strategy, job=job)


def _read_value(resolved: dtbridge.ResolvedImage, module: str, param: str,
                instance: int) -> float | None:
    """Current value from the XMP, or None when it is not decodable."""
    try:
        text, _ = xmp.read_sidecar(resolved.path)
    except OSError:
        return None
    return xmp.latest_value(xmp.parse(text), module, param, instance)


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


def _apply_set(resolved, module: str, param: str, instance: int, value: float,
               commit: bool, job: str, verb: str) -> SetResult:
    dtbridge.require_darkroom(resolved.image_id)
    before = _read_value(resolved, module, param, instance)
    dtbridge.action(
        dtbridge.action_path(module, param),
        instance=instance, element="value", effect="set", value=value,
    )
    journal.record(
        job, verb=verb, image_id=resolved.image_id, path=resolved.path,
        module=module, param=param, instance=instance,
        before={"source": "xmp", "value": before},
        after={"value": value},
    )
    committed = False
    applied = None
    warnings: list[str] = []
    if commit:
        commit_result = _commit(job, resolved.image_id, strategy="auto")
        committed = commit_result.committed
        warnings.extend(commit_result.warnings)
        applied = _read_value(resolved, module, param, instance)
        if applied is None:
            warnings.append(
                f"{module}.{param} committed but could not be decoded from the XMP; "
                "`requested` is what was sent, `applied` is unknown"
            )
    return SetResult(
        image_id=resolved.image_id, path=resolved.path, module=module, param=param,
        instance=instance, requested=value, applied=applied, previous=before,
        committed=committed, warnings=warnings,
    )


@metrics.instrumented("darkroom.set")
def set_param(module: str, param: str, value: float, *, instance: int = 0,
              commit: bool = True, dry_run: bool = False,
              job: str | None = None) -> SetResult:
    """Set one module parameter to an absolute value (phase 0 finding 1)."""
    job = job or current_job()
    resolved = dtbridge.resolve_image(_darkroom_image_hint())
    if dry_run:
        return SetResult(
            outcome="dry_run", image_id=resolved.image_id, path=resolved.path,
            module=module, param=param, instance=instance, requested=value,
            previous=_read_value(resolved, module, param, instance),
            plan=[{"action": "darkroom.set", "path": dtbridge.action_path(module, param),
                   "effect": "set", "value": value, "instance": instance}],
            reason="dry run: no action was sent to darktable",
        )
    return _apply_set(resolved, module, param, instance, value, commit, job, "darkroom.set")


@metrics.instrumented("darkroom.nudge")
def nudge(module: str, param: str, steps: int = 1, *, instance: int = 0,
          commit: bool = True, dry_run: bool = False,
          job: str | None = None) -> SetResult:
    """Move a slider by `steps` of darktable's own default step (+/-)."""
    job = job or current_job()
    resolved = dtbridge.resolve_image(_darkroom_image_hint())
    path = dtbridge.action_path(module, param)
    effect = "up" if steps >= 0 else "down"
    if dry_run:
        return SetResult(
            outcome="dry_run", image_id=resolved.image_id, path=resolved.path,
            module=module, param=param, instance=instance,
            plan=[{"action": "darkroom.nudge", "path": path, "effect": effect,
                   "repeats": abs(steps), "instance": instance}],
            reason="dry run: no action was sent to darktable",
        )
    dtbridge.require_darkroom(resolved.image_id)
    before = _read_value(resolved, module, param, instance)
    for _ in range(abs(steps)):
        dtbridge.action(path, instance=instance, element="value", effect=effect, value=1)
    journal.record(
        job, verb="darkroom.nudge", image_id=resolved.image_id, path=resolved.path,
        module=module, param=param, instance=instance,
        before={"source": "xmp", "value": before}, after={"steps": steps},
    )
    committed, applied, warnings = False, None, []
    if commit:
        commit_result = _commit(job, resolved.image_id, strategy="auto")
        committed, warnings = commit_result.committed, list(commit_result.warnings)
        applied = _read_value(resolved, module, param, instance)
    return SetResult(
        image_id=resolved.image_id, path=resolved.path, module=module, param=param,
        instance=instance, requested=None, applied=applied, previous=before,
        committed=committed, warnings=warnings,
    )


def _module_enabled(resolved, module: str, instance: int) -> bool | None:
    try:
        text, _ = xmp.read_sidecar(resolved.path)
    except OSError:
        return None
    parsed = xmp.parse(text)
    for entry in sorted(parsed.entries, key=lambda e: e.num, reverse=True):
        if entry.operation == module and entry.active and entry.multi_priority == instance:
            return entry.enabled
    return None


def _do_module_action(action_name: str, method: str, *, module: str, instance: int,
                      dry_run: bool, confirm: bool, job: str | None) -> ModuleResult:
    job = job or current_job()
    resolved = dtbridge.resolve_image(_darkroom_image_hint())
    gated = action_name == "reset"
    plan = [{"action": f"module.{action_name}", "module": module,
             "instance": instance, "image_id": resolved.image_id}]
    if dry_run or (gated and not confirm):
        return ModuleResult(
            outcome="dry_run", image_id=resolved.image_id, path=resolved.path,
            module=module, action=action_name, instance=instance, plan=plan,
            reason=(
                "reset discards this module's parameters and cannot be undone by "
                "Scouter (design 8.2); re-run with --yes / confirm=true"
                if gated and not confirm
                else "dry run: no action was sent to darktable"
            ),
        )
    dtbridge.require_darkroom(resolved.image_id)
    before_enabled = _module_enabled(resolved, module, instance)
    dtbridge.call(method, {"module": module, "instance": instance})
    journal.record(
        job, verb=f"module.{action_name}", image_id=resolved.image_id,
        path=resolved.path, module=module, param="", instance=instance,
        before={"source": "xmp", "enabled": before_enabled},
        after={"action": action_name},
    )
    commit_result = _commit(job, resolved.image_id, strategy="auto")
    return ModuleResult(
        image_id=resolved.image_id, path=resolved.path, module=module,
        action=action_name, instance=instance,
        enabled=_module_enabled(resolved, module, instance),
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
        return UndoResult(
            outcome="dry_run", reason=f"nothing to undo: journal for {job} is empty",
        )
    plan = [{"action": "darkroom.undo_last", "seq": entry.seq, "verb": entry.verb,
             "module": entry.module, "param": entry.param,
             "restore_to": entry.before.get("value")}]
    if dry_run:
        return UndoResult(outcome="dry_run", image_id=entry.image_id, path=entry.path,
                          seq=entry.seq, plan=plan,
                          reason="dry run: no action was sent to darktable")

    if entry.verb == "module.reset":
        raise ScouterError(
            f"cannot undo the reset of {entry.module}: its parameters were discarded. "
            "darktable's own Ctrl-Z is the only way back",
            fix="press Ctrl-Z in darktable; Scouter cannot restore discarded params",
            detail={"seq": entry.seq, "module": entry.module},
        )

    dtbridge.require_darkroom(entry.image_id)
    if entry.verb == "module.enable":
        dtbridge.call("module.disable", {"module": entry.module,
                                         "instance": entry.instance})
        restored: float | None = None
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
        restored = float(previous)

    commit_result = _commit(job, entry.image_id, strategy="auto")
    journal.mark_undone(job, entry.seq)
    return UndoResult(
        image_id=entry.image_id, path=entry.path, seq=entry.seq,
        undone={"verb": entry.verb, "module": entry.module, "param": entry.param,
                "restored_to": restored},
        committed=commit_result.committed, warnings=list(commit_result.warnings),
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

    Delegates to `scouter.core.history.read` (task 8), imported lazily for the
    same reason `_commit` is (ruling 21): `darkroom.py` must import cleanly,
    and stay registered, before `history.py` exists. `.__wrapped__` calls the
    undecorated function so this verb still emits exactly one metrics event,
    not two.
    """
    from scouter.core import history

    target = image if image is not None else _darkroom_image_hint()
    return history.read.__wrapped__(target, job=job)


def _darkroom_image_hint() -> int:
    """The image the darkroom currently has loaded.

    Every darkroom verb operates on it, so it is resolved once here rather than
    made an argument: asking the caller to repeat the image id would let the
    agent address an image that is not the one on screen, which is exactly the
    silent-failure class the honesty pass removed.
    """
    result = dtbridge.call("darkroom.open", {})
    return int(result["image_id"])


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
         args=(ArgSpec("image", "image", help="defaults to the darkroom image"),),
         writes=False, gated=False, summary="The darkroom image's committed history."),
):
    register(verb)
