"""Relative moves (phase 3 design §6): read the current value, add, clamp, set.

The engine is unchanged: every write goes through `darkroom.set_param.__wrapped__`
(one metrics event per top-level verb). Values come from the journal, the XMP,
the measured WB table or a declared neutral -- never from a slider position
(P3-R2).
"""

from __future__ import annotations

import time

from scouter import metrics
from scouter.core import darkroom, dtbridge, explain, history, journal, phrases, vocab, wb, xmp
from scouter.errors import CommitTimeout, NotPinned, ScouterError, UsageError, ValueUnknown
from scouter.paths import current_job
from scouter.registry import ArgSpec, Verb, register
from scouter.types import AdjustResult, ResolvedImage, SayResult, ValueResult

#: Only the always-finite coefficients (controller ruling, task 5): the real
#: sidecar decodes `coeff_g2` as `None` (`xmp.latest_params` -> `_json_safe`
#: cleans `inf`/`nan` to `None`, and `coeff_g2` is `inf` on the EM10iii body),
#: so requiring it here would fail every Kelvin read. `wb.kelvin`'s own
#: `_ratio` only ever uses `coeff_r`/`coeff_b` anyway -- the same set
#: `wb._TEMPERATURE_COEFF_NAMES` already uses for exactly this reason.
_TEMP_COEFFS = wb._TEMPERATURE_COEFF_NAMES
_TINT_WARNING = ("Kelvin read from the r/b coefficient ratio against a table measured at "
                 "tint 1.0; at another tint it is approximate")


def require_row(module: str, param: str) -> vocab.ParamSpec:
    s = vocab.spec(module, param)
    if s is None:
        raise UsageError(
            f"{module}.{param} is not in the conversational vocabulary",
            fix="`scouter darkroom set` reaches any pinned pair; `scouter vocab list` shows the words",
            detail={"module": module, "param": param},
        )
    return s


def gate(s: vocab.ParamSpec, *, relative: bool) -> None:
    """Refuse by status BEFORE any bridge call (design §4.2)."""
    st = vocab.status(s)
    if st in ("candidate", "unreachable"):
        alt = vocab.closest_live(s)
        hint = s.note or (f"closest live word: {alt.module}.{alt.param} ({alt.ui})" if alt else
                          "no live alternative yet")
        raise NotPinned(
            f"{s.module}.{s.param} has no pinned darktable action path (status {st}); {hint}",
            detail={"module": s.module, "param": s.param, "status": st,
                    "candidate_paths": list(s.candidate_paths)},
        )
    if relative and st == "absolute_only":
        raise ValueUnknown(
            f"{s.module}.{s.param}'s current value is not readable from the XMP yet",
            fix=(f"set it absolutely: `scouter darkroom set {s.module} {s.param} <value>`; "
                 "relative moves need its XMP slot pinned by a host probe"),
            detail={"module": s.module, "param": s.param, "status": st,
                    "reason": "no named XMP field for this parameter"},
        )


def _camera_for(path: str) -> str | None:
    """Camera slug from EXIF, or None. A test seam: unit tests patch it."""
    from scouter import config
    from scouter.core import exif

    try:
        return exif.camera_slug(exif.read_tags(path), config.load())
    except (ScouterError, OSError):
        return None


def _table_range(camera: str) -> tuple[float, float] | None:
    try:
        table = wb.load_table(camera)
    except ValueError:
        return None
    if not table:
        return None
    ks = [float(p["kelvin"]) for p in table["samples"]]
    return min(ks), max(ks)


def read_current(resolved: ResolvedImage, s: vocab.ParamSpec, instance: int,
                 job: str) -> tuple[float | None, str, str | None]:
    """(value, source, reason). Source order: journal, xmp, wb_table, neutral."""
    if s.value_source == "wb_table":
        parsed = darkroom._read_sidecar_parsed(resolved.path)
        if parsed is None:
            return None, "wb_table", "no sidecar yet: commit history first"
        params = xmp.latest_params(parsed, "temperature", instance)
        if not params:
            return None, "wb_table", "no active temperature entry in the XMP"
        try:
            coeffs = [float(params[k]) for k in _TEMP_COEFFS]
        except (KeyError, TypeError, ValueError):
            return None, "wb_table", "temperature's coefficients did not decode"
        camera = _camera_for(resolved.path)
        if camera is None:
            return None, "wb_table", "camera model unreadable from EXIF; run scouter wb calibrate"
        result = wb.kelvin(coeffs, camera)
        return result.get("kelvin"), "wb_table", result.get("reason")
    raw, before, pending = darkroom._raw_and_before_state(
        job, resolved.image_id, resolved.path, s.module, s.param, instance)
    if pending and before.get("value") is not None:
        return float(before["value"]), "journal", None
    # Fix round 1, important: `_module_state`'s `enabled: None` is ambiguous --
    # it means BOTH "no sidecar at all" and "a sidecar exists, but this module
    # has no active entry in it" (darkroom.py's `_module_state`/`_read_sidecar_
    # parsed`, including an OSError read). Spec §6.1 row 4 only ever allows the
    # neutral fallback for the second case; `history_end` is `None` only when
    # there is no sidecar to parse (or a torn read never settled), so checking
    # it here is what tells the two apart -- the wb_table branch above already
    # refuses outright when there is no sidecar, and this branch must agree
    # rather than silently guessing a neutral value nobody has confirmed yet.
    if raw.get("history_end") is None:
        return None, "xmp", "no sidecar yet: commit history first"
    if raw.get("value") is not None:
        return float(raw["value"]), "xmp", None
    if raw.get("enabled") is None and s.neutral is not None:
        return s.neutral, "neutral", None
    return None, "xmp", f"{s.module}.{s.param} did not decode from the XMP"


def route_tone(raw_path: str) -> str:
    """`@tone` -> filmicrgb only when filmic is enabled+active and sigmoid is not (§5.4)."""
    parsed = darkroom._read_sidecar_parsed(raw_path)
    if parsed is None:
        return "sigmoid"
    enabled = {e.operation for e in xmp.active_entries(parsed) if e.enabled}
    return "filmicrgb" if "filmicrgb" in enabled and "sigmoid" not in enabled else "sigmoid"


def _fmt(s: vocab.ParamSpec, v: float | None) -> str:
    return "?" if v is None else explain.with_unit(s, f"{vocab.to_display(s, v):.4g}")


def _summary(s: vocab.ParamSpec, delta: float, prev: float, shown: float | None,
             clamped: bool, target: float) -> str:
    d = explain.with_unit(s, f"{vocab.to_display(s, delta):+.3g}")
    line = f"{s.ui}: {d} ({_fmt(s, prev)} → {_fmt(s, shown if shown is not None else target)})"
    if clamped:
        line += f" [clamped at {_fmt(s, target)}]"
    return line


def _adjust_core(module: str, param: str, delta: float, *, resolved: ResolvedImage,
                 instance: int, commit: bool, level: str, job: str,
                 ms_precommit: int = 0) -> AdjustResult:
    """Steps 5-11 of §6.2. The caller has gated, resolved and (maybe) pre-committed.

    `ms_precommit` (spec §16): the caller's own pre-commit bounce time, folded
    into `plan[0]` alongside `ms_read`/`ms_set` -- it defaults to 0 for a
    caller that skipped the pre-commit (`trust_sidecar`, a dry run, or a
    future caller like `say` that pre-commits once for a whole group).
    """
    s = vocab.spec(module, param)
    assert s is not None
    t0 = time.monotonic()
    previous, source, reason = read_current(resolved, s, instance, job)
    if previous is None:
        raise ValueUnknown(
            f"cannot read {module}.{param}'s current value",
            detail={"module": module, "param": param, "source_tried": source,
                    "reason": reason or "unknown"},
        )
    warnings: list[str] = []
    target, clamped = vocab.clamp(s, previous + delta)
    if s.value_source == "wb_table":
        warnings.append(_TINT_WARNING)
        camera = _camera_for(resolved.path)
        rng = _table_range(camera) if camera else None
        if rng and not (rng[0] <= target <= rng[1]):
            target, clamped = min(max(target, rng[0]), rng[1]), True
    if clamped and abs(target - previous) < 1e-6:
        # Fix round 1, controller ruling: the no-op path still reports
        # plan[0] with ms_precommit/ms_read/ms_set (spec §16) -- ms_set is
        # always 0 here, since no set_fn call was ever made.
        return AdjustResult(
            image_id=resolved.image_id, path=resolved.path, module=module, param=param,
            instance=instance, unit=s.unit, delta=delta, previous=previous, target=target,
            applied=previous, clamped=True, source=source, committed=False, noop=True,
            summary=f"{s.ui}: already at the limit ({_fmt(s, previous)}); nothing sent",
            warnings=warnings,
            plan=[{"action": "darkroom.adjust", "ms_precommit": ms_precommit,
                   "ms_read": int((time.monotonic() - t0) * 1000), "ms_set": 0}],
        )
    set_fn = getattr(darkroom.set_param, "__wrapped__", darkroom.set_param)
    t1 = time.monotonic()
    result = set_fn(module, param, target, instance=instance, commit=commit, job=job)
    if source == "neutral" and not result.noop:
        # Fix round 1, controller ruling, load-bearing: `set_param`'s own
        # `before_state` never heard about the neutral fallback (it reads
        # only the XMP/journal) and journalled `before.value: None` for
        # this move -- undoable. Patch the entry it just landed to record
        # the neutral it actually started from. `result.noop` is excluded:
        # the zero-bridge-write shortcut never calls `journal.begin` at
        # all, so there is no entry here to patch.
        journal.set_before_neutral(job, journal.next_seq(job) - 1, value=previous)
    applied = result.applied
    if s.value_source == "wb_table":
        applied, _, _ = read_current(resolved, s, instance, job) if commit else (None, "", None)
    warnings.extend(result.warnings)
    explain_lines = explain.lines([explain.MoveNote(s, previous, target)], level=level, job=job)
    return AdjustResult(
        image_id=resolved.image_id, path=resolved.path, module=module, param=param,
        instance=instance, unit=s.unit, delta=delta, previous=previous, target=target,
        applied=applied, clamped=clamped, source=source, committed=result.committed,
        noop=result.noop, summary=_summary(s, delta, previous, applied, clamped, target),
        explain=explain_lines, warnings=warnings,
        plan=[{"action": "darkroom.adjust", "ms_precommit": ms_precommit,
               "ms_read": int((t1 - t0) * 1000),
               "ms_set": int((time.monotonic() - t1) * 1000)}],
    )


def _precommit(job: str, image_id: int) -> str | None:
    """One expect-no-change bounce (P3-R3). A timeout is a warning, not a failure.

    Controller ruling, fix round 1: ONLY `CommitTimeout` (spec §6.2 step 4) is
    downgraded to a warning here -- darktable simply not settling within one
    bounce is an expected, honest outcome, not a reason to refuse the whole
    move. Every other `ScouterError` (a wrong view, a dead bridge, a locked
    library, ...) is a real failure and propagates untouched: swallowing it
    here would silently continue a move whose pre-flight already failed.
    """
    try:
        darkroom._commit(job, image_id, strategy="auto", attempts=1, expect_change=False)
    except CommitTimeout as e:
        return f"pre-commit did not confirm ({e.code}); the value read may predate darktable's live state"
    return None


@metrics.instrumented("darkroom.get")
def get(module: str, param: str, *, instance: int = 0, job: str | None = None) -> ValueResult:
    """A parameter's current VALUE. Never commits, never moves the view."""
    job = job or current_job()
    s = require_row(module, param)
    resolved = darkroom._current_image()
    value, source, reason = read_current(resolved, s, instance, job)
    fresh, why = history.watermark_fresh(job, resolved.image_id, resolved.xmp_path)
    warnings: list[str] = []
    if not fresh:
        warnings.append(f"sidecar not known-fresh ({why}): darktable may hold newer, uncommitted "
                        "changes; `darkroom adjust` commits before it reads")
    if value is None:
        warnings.append(reason or f"{module}.{param} is not readable")
    return ValueResult(
        image_id=resolved.image_id, path=resolved.path, module=module, param=param,
        instance=instance, value=value,
        display_value=None if value is None else vocab.to_display(s, value),
        unit=s.unit, source=source, enabled=darkroom._module_enabled(resolved.path, module, instance),
        fresh=bool(fresh), status=vocab.status(s), warnings=warnings,
    )


@metrics.instrumented("darkroom.adjust")
def adjust(module: str, param: str, delta: float, *, instance: int = 0, commit: bool = True,
           trust_sidecar: bool = False, educative: str | None = None, dry_run: bool = False,
           job: str | None = None) -> AdjustResult:
    """Move one parameter by `delta` (internal units), relative to its current value."""
    job = job or current_job()
    s = require_row(module, param)
    delta = darkroom._require_finite("delta", delta)
    if delta == 0:
        raise UsageError("delta must be non-zero", detail={"argument": "delta"})
    level = explain.resolve_level(educative)
    gate(s, relative=True)
    if dry_run:
        return AdjustResult(
            outcome="dry_run", module=module, param=param, instance=instance, unit=s.unit,
            delta=delta,
            plan=[{"action": "darkroom.adjust", "path": dtbridge.ACTION_PATHS[(module, param)],
                   "delta": delta, "clamp": [s.lo, s.hi], "wraps": s.wraps}],
            reason="dry run: no darktable calls were made; the current value was not read",
        )
    resolved = darkroom._current_image()
    darkroom._require_job_image(job, resolved.image_id)
    dtbridge.require_darkroom()
    ms_precommit = 0
    warning = None
    if not trust_sidecar:
        t_pre = time.monotonic()
        warning = _precommit(job, resolved.image_id)
        ms_precommit = int((time.monotonic() - t_pre) * 1000)
    result = _adjust_core(module, param, delta, resolved=resolved, instance=instance,
                          commit=commit, level=level, job=job, ms_precommit=ms_precommit)
    if warning:
        result.warnings.insert(0, warning)
    return result


def _refusal(mv_module: str, param: str, kind: str) -> dict | None:
    s = vocab.spec(mv_module, param)
    if s is None:
        return {"module": mv_module, "param": param, "code": "usage", "status": "missing"}
    try:
        gate(s, relative=(kind == "adjust"))
    except ScouterError as e:
        return {"module": mv_module, "param": param, "code": e.code, "status": vocab.status(s),
                "message": e.message}
    return None


def _raise_refused(phrase: str, key: str, refused: list[dict], total: int) -> None:
    first = refused[0]
    cls = {"not_pinned": NotPinned, "value_unknown": ValueUnknown}.get(first["code"], UsageError)
    raise cls(
        f"'{phrase}' cannot run: {len(refused)} of {total} move(s) are not reachable "
        f"({', '.join(r['module'] + '.' + r['param'] for r in refused)})",
        detail={"phrase": phrase, "key": key, "refused": refused},
    )


@metrics.instrumented("darkroom.say")
def say(phrase: str, *, amount: float | None = None, commit: bool = True,
        trust_sidecar: bool = False, educative: str | None = None, dry_run: bool = False,
        job: str | None = None) -> SayResult:
    """A plain-words edit: every move of the phrase, as one undoable group (§6.3)."""
    job = job or current_job()
    res = phrases.resolve(phrase, amount)
    level = explain.resolve_level(educative)
    if dry_run:
        return SayResult(
            outcome="dry_run", phrase=phrase, key=res.key, intensity=res.intensity,
            moves=[phrases.move_view(mv) for mv in res.moves],
            reason="dry run: no darktable calls; @tone was not routed against the image",
        )
    # Controller ruling: refuse before touching the bridge at all. Every
    # non-@tone move can be gated with no darktable call (`_refusal` never
    # calls the bridge); @tone needs `route_tone`, which needs the darkroom
    # image resolved, so it is checked in a second pass below. This is what
    # makes `say("teal and orange")` refuse (not_pinned) with zero bridge
    # calls, including no `darkroom.current` lookup.
    pre_refused = [r for r in (_refusal(mv.module, mv.param, mv.kind) for mv in res.moves
                                if mv.module != phrases.TONE) if r]
    if pre_refused:
        _raise_refused(phrase, res.key, pre_refused, len(res.moves))
    resolved = darkroom._current_image()
    darkroom._require_job_image(job, resolved.image_id)
    dtbridge.require_darkroom()
    tone = route_tone(resolved.path)
    concrete = [(tone if mv.module == phrases.TONE else mv.module, mv) for mv in res.moves]
    refused = [r for r in (_refusal(m, mv.param, mv.kind) for m, mv in concrete) if r]
    if refused:
        _raise_refused(phrase, res.key, refused, len(concrete))
    group = f"say-{journal.next_seq(job)}"
    warning = None if trust_sidecar else _precommit(job, resolved.image_id)
    landed: list[dict] = []
    notes: list[explain.MoveNote] = []
    failures: list[dict] = []
    token = journal.GROUP.set(group)
    try:
        for module, mv in concrete:
            s = vocab.spec(module, mv.param)
            try:
                if mv.kind == "adjust":
                    delta = mv.value if mv.notches is None else mv.notches * s.step
                    r = _adjust_core(module, mv.param, delta, resolved=resolved, instance=0,
                                     commit=commit, level="off", job=job)
                    notes.append(explain.MoveNote(s, r.previous, r.target))
                    landed.append(r.to_dict())
                else:
                    target, _ = vocab.clamp(s, float(mv.value))
                    set_fn = getattr(darkroom.set_param, "__wrapped__", darkroom.set_param)
                    r = set_fn(module, mv.param, target, commit=commit, job=job)
                    notes.append(explain.MoveNote(s, r.previous, target, "set"))
                    landed.append({**r.to_dict(), "target": target,
                                   "summary": f"{s.ui}: set to {explain.with_unit(s, f'{vocab.to_display(s, target):.4g}')}"})
            except ScouterError as e:
                if not landed:
                    raise
                failures.append({"module": module, "param": mv.param, "code": e.code,
                                 "message": e.message})
                break
    finally:
        journal.GROUP.reset(token)
    return SayResult(
        outcome="partial" if failures else "ok",
        image_id=resolved.image_id, path=resolved.path, phrase=phrase, key=res.key,
        intensity=res.intensity, group=group, moves=landed, refused=failures,
        summary="; ".join(m.get("summary", "") for m in landed),
        explain=explain.lines(notes, level=level, job=job),
        warnings=[w for w in [warning] if w],
    )


EDUCATIVE_ARG = ArgSpec("educative", "str", default=None, choices=("off", "brief", "full"),
                        help="explanation level for this call (default: config's educative)")
TRUST_ARG = ArgSpec("trust_sidecar", "bool", default=False,
                    help="skip the pre-read commit bounce (scripts that own the darkroom)")

for verb in (
    Verb(
        id="darkroom.get",
        group="darkroom",
        fn=get,
        args=(darkroom.MODULE_ARG, darkroom.PARAM_ARG, darkroom.INSTANCE_ARG),
        writes=False,
        gated=False,
        summary="Read one parameter's current value from the XMP (never moves the view).",
    ),
    Verb(
        id="darkroom.adjust",
        group="darkroom",
        fn=adjust,
        args=(
            darkroom.MODULE_ARG,
            darkroom.PARAM_ARG,
            ArgSpec("delta", "float", required=True, positional=True,
                    help="relative change in internal units (EV, K, or a fraction: 0.05 = 5%%)"),
            darkroom.INSTANCE_ARG,
            darkroom.COMMIT_ARG,
            TRUST_ARG,
            EDUCATIVE_ARG,
            darkroom.DRY_RUN_ARG,
        ),
        writes=True,
        gated=False,
        summary="Change one parameter by a relative amount, clamped to a safe range.",
    ),
    Verb(
        id="darkroom.say",
        group="darkroom",
        fn=say,
        args=(
            ArgSpec("phrase", "str", required=True, positional=True,
                    help="plain words, e.g. 'a touch warmer' (quote it)"),
            ArgSpec("amount", "float", default=None,
                    help="multiplier on the phrase's step (0-5]"),
            darkroom.COMMIT_ARG,
            TRUST_ARG,
            EDUCATIVE_ARG,
            darkroom.DRY_RUN_ARG,
        ),
        writes=True,
        gated=False,
        summary="Edit by a plain-words phrase; its moves undo together.",
    ),
):
    register(verb)
