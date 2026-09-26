"""Relative moves (phase 3 design §6): read the current value, add, clamp, set.

The engine is unchanged: every write goes through `darkroom.set_param.__wrapped__`
(one metrics event per top-level verb). Values come from the journal, the XMP,
the measured WB table or a declared neutral -- never from a slider position
(P3-R2).
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from scouter import metrics
from scouter.core import darkroom, dtbridge, explain, history, journal, phrases, vocab, wb, xmp
from scouter.errors import (
    CommitTimeout,
    NotPinned,
    ScouterError,
    UsageError,
    ValueUnknown,
    WriteUnverified,
)
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
    t1 = time.monotonic()
    wb_before = _wb_ratio(resolved, instance) if s.value_source == "wb_table" and commit else None
    # Fix round 1, controller ruling, load-bearing: `set_param`'s own
    # `before_state` never heard about the neutral fallback (it reads only
    # the XMP/journal) and journals `before.value: None` for this move --
    # `_set_recording_before` patches the entry it lands to record the
    # neutral it actually started from, including when the write landed
    # and the commit then timed out (final review #1b).
    result = _set_recording_before(
        module, param, target, instance=instance, commit=commit, job=job,
        fallback=(previous, "neutral") if source == "neutral" else None)
    applied = result.applied
    if s.value_source == "wb_table":
        applied, _, _ = read_current(resolved, s, instance, job) if commit else (None, "", None)
        if commit:
            warnings.extend(_verify_kelvin(resolved, s, instance, previous, target, applied,
                                           wb_before))
    clamped = clamped or result.clamped
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


#: Row 433: how far the measured r/b ratio may sit from the one the table
#: predicts at the target Kelvin. darktable's own Kelvin model and Scouter's
#: measured table disagree by up to ~2.4% in r/b on the EM10iii (night 1:
#: 5225 K set reads back 5124 K through the table, every time); three times
#: the table's own "exact" tolerance (`wb._EXACT_TOLERANCE`, 1%) covers that
#: with margin while still failing a 300 K move that did not happen (7%).
#: Upper bound, from the table's resolution: the shipped measured table
#: (`scouter/data/wb/em10iii.json`, 1000 K steps) changes r/b by at least
#: ~3.9% between adjacent samples (11000 -> 12000 K), so 3% never verifies a
#: landing a whole table step away. Pinned by
#: `test_wb_verify_tolerance_is_pinned_between_the_model_offset_and_one_table_step`.
_WB_VERIFY_TOLERANCE = 3 * wb._EXACT_TOLERANCE


def _wb_ratio(resolved: ResolvedImage, instance: int) -> float | None:
    params = darkroom._temperature_extra(resolved.path, instance).get("params")
    return darkroom._rb_ratio(params)


def _verify_kelvin(resolved: ResolvedImage, s: vocab.ParamSpec, instance: int, previous: float,
                   target: float, applied: float | None,
                   before_ratio: float | None) -> list[str]:
    """Row 433, night-1 A (`cov-phrase-warmer-olympus-bare`: 4925 K -> 5225 K
    reported ok, coefficients untouched). A Kelvin move is verified in the
    coefficient domain, where darktable's write is exact: the r/b ratio must
    have changed at all, and must sit within `_WB_VERIFY_TOLERANCE` of the
    ratio the measured table predicts at `target`. Raises `WriteUnverified`;
    returns warnings when it cannot check (no table, no decodable ratio)."""
    after_ratio = _wb_ratio(resolved, instance)
    camera = _camera_for(resolved.path)
    expected = wb.expected_ratio(camera, target) if camera else None
    detail = {"module": s.module, "param": s.param, "requested": target, "expected": target,
              "applied": applied, "previous": previous, "ratio_before": before_ratio,
              "ratio_after": after_ratio, "ratio_expected": expected,
              "tolerance": _WB_VERIFY_TOLERANCE}
    if after_ratio is None:
        return [f"{s.ui}: the coefficients did not decode after the write, so the move could "
                "not be verified"]
    if before_ratio is not None and abs(after_ratio - before_ratio) <= 1e-6 * abs(before_ratio):
        raise WriteUnverified(
            f"{s.module}.{s.param}: darktable was sent {target:.0f} K and the commit reported "
            f"done, but the white-balance coefficients in the sidecar did not change "
            f"(r/b {after_ratio:.4f}; still {previous:.0f} K)",
            detail=detail,
        )
    if expected is None:
        return [f"{s.ui}: {target:.0f} K is outside the measured table, so the landed "
                "coefficients could not be checked against it"]
    if abs(after_ratio - expected) / abs(expected) > _WB_VERIFY_TOLERANCE:
        raise WriteUnverified(
            f"{s.module}.{s.param}: darktable was sent {target:.0f} K, but the sidecar's r/b "
            f"ratio {after_ratio:.4f} is {100 * abs(after_ratio / expected - 1):.1f}% from the "
            f"{expected:.4f} the measured table predicts there (reads as "
            f"{'?' if applied is None else f'{applied:.0f}'} K)",
            detail=detail,
        )
    return []


def before_fallback(resolved: ResolvedImage, s: vocab.ParamSpec, instance: int,
                    job: str) -> tuple[float, str] | None:
    """What a `set` move's journal entry must record as `before` (final review #1a).

    `darkroom.set_param` journals the XMP/journal value it reads
    (`_raw_and_before_state`). When that is readable, nothing is needed:
    `None`. When it is not, and the sidecar exists but the module has no
    active history entry (spec §6.1 row 4's condition), the row's declared
    `neutral` -- or, failing that, its darktable `default` -- is what the
    module really holds: `(value, "neutral" | "default")`. Anything else
    (no sidecar, an entry that does not decode, or a row declaring neither)
    raises `ValueUnknown` BEFORE any write, so a move never creates an entry
    `undo_last` can only refuse ("never readable"). Local reads only; no
    bridge call. `wb_table` rows are left alone: their undo restores the
    journalled coefficients, not `before.value`.
    """
    if s.value_source == "wb_table":
        return None
    raw, before, _ = darkroom._raw_and_before_state(
        job, resolved.image_id, resolved.path, s.module, s.param, instance)
    if before.get("value") is not None:
        return None
    if raw.get("history_end") is None:
        reason = "no sidecar yet: commit history first"
    elif raw.get("enabled") is None:
        if s.neutral is not None:
            return float(s.neutral), "neutral"
        if s.default is not None:
            return float(s.default), "default"
        reason = (f"{s.module} has no active history entry and the vocabulary row declares "
                  "no neutral or darktable default, so the move could not be undone")
    else:
        reason = f"{s.module}.{s.param} did not decode from the XMP"
    raise ValueUnknown(
        f"cannot read {s.module}.{s.param}'s current value, so this move could not be undone",
        fix=(f"Scouter refuses rather than make an edit it could not undo: set it in "
             f"darktable, or with `scouter darkroom set {s.module} {s.param} <value>` "
             "(undo_last cannot restore the value before it)"),
        detail={"module": s.module, "param": s.param, "reason": reason},
    )


def _patch_before(job: str, seq: int, module: str, param: str,
                  fallback: tuple[float, str]) -> None:
    """Record `fallback` as entry `seq`'s before -- only if that entry exists,
    is this module/param, actually landed (`after` set) and still has no
    readable before. `journal.set_before_neutral` re-checks and refuses."""
    entry = next((e for e in journal.read_all(job) if e.seq == seq), None)
    if entry is None or entry.after is None or (entry.module, entry.param) != (module, param):
        return
    if entry.before.get("value") is not None:
        return
    journal.set_before_neutral(job, seq, module=module, param=param, value=fallback[0],
                               source=fallback[1])


def _set_recording_before(module: str, param: str, target: float, *, instance: int,
                          commit: bool, job: str, fallback: tuple[float, str] | None):
    """`darkroom.set_param.__wrapped__`, then record `fallback` as the landed
    entry's before. The engine is untouched: this only patches the journal.

    Final review #1b: `set_param` can land the write (`journal.complete`)
    and THEN raise `CommitTimeout` (or, task 10, any ScouterError) from its commit. The entry exists and is
    undoable -- so the patch must happen on that path too, before the
    timeout is re-raised; skipping it wedged every later `undo_last` on
    "seq N ... never readable". `seq` is taken before the call, so a no-op
    shortcut (which begins no entry) or a write that failed before `begin`
    patches nothing.
    """
    set_fn = getattr(darkroom.set_param, "__wrapped__", darkroom.set_param)
    seq = journal.next_seq(job)
    try:
        result = set_fn(module, param, target, instance=instance, commit=commit, job=job)
    except ScouterError:
        # Phase 3 task 10 (parked final-review issue): not only CommitTimeout
        # -- ANY Scouter error raised after the write landed (a BridgeTimeout
        # or WrongView from `history.commit`) left `before.value` None and
        # wedged every later `undo_last`. `_patch_before` only touches an
        # entry whose `after` is set, so a write that never landed is left
        # exactly as the engine wrote it.
        if fallback is not None:
            _patch_before(job, seq, module, param, fallback)
        raise
    if fallback is not None:
        # Row 433: a readback-confirmed no-op (`noop` with a write sent)
        # began and landed an entry too; only the zero-bridge shortcut
        # begins none, and `_patch_before` then finds nothing to patch.
        _patch_before(job, seq, module, param, fallback)
    return result


def _precommit(job: str, image_id: int, *, expect_module: str | None = None) -> str | None:
    """One expect-no-change bounce (P3-R3). A timeout is a warning, not a failure.

    Controller ruling, fix round 1: ONLY `CommitTimeout` (spec §6.2 step 4) is
    downgraded to a warning here -- darktable simply not settling within one
    bounce is an expected, honest outcome, not a reason to refuse the whole
    move. Every other `ScouterError` (a wrong view, a dead bridge, a locked
    library, ...) is a real failure and propagates untouched: swallowing it
    here would silently continue a move whose pre-flight already failed.

    `expect_module` (row 461 follow-up): the module the caller (`get`,
    `adjust`) is about to read or write, forwarded to `_commit` exactly like
    every other writer's commit call (`_try_commit`, `_commit_with_readback`)
    already does. Without it, a stale rewrite whose newest history item
    belongs to some OTHER module was indistinguishable from a fresh flush of
    THIS one -- the same hole row 434/461 closed for the real commit, just
    left open on this pre-flight bounce.
    """
    try:
        darkroom._commit(job, image_id, strategy="auto", attempts=1, expect_change=False,
                         expect_module=expect_module)
    except CommitTimeout as e:
        return f"pre-commit did not confirm ({e.code}); the value read may predate darktable's live state"
    return None


def _contradiction() -> None:
    raise UsageError("bounce and trust_sidecar contradict each other; pass one",
                     detail={"bounce": True, "trust_sidecar": True})


def precommit_decision(job: str, resolved: ResolvedImage, *, bounce: bool = False,
                       trust_sidecar: bool = False) -> tuple[bool, str]:
    """Does this read need the pre-read bounce? `(bounce_it, reason)`.

    Product close, row 427 item 4: the bounce is ~2.7 s of a 4.1 s one-move
    `say` (row 415), and it only ever buys something when the sidecar may be
    older than what darktable holds. Trusting the sidecar is the default; the
    staleness guard bounces only when Scouter cannot show the sidecar is
    current:

    - there is no sidecar yet;
    - the image changed: the journal's last move was on another image, or
      this session has no move yet (unless Scouter's own watermark proves the
      sidecar is exactly what Scouter last committed);
    - the sidecar's mtime is older than the journal's last move on this image
      (a `commit=False` move darktable has not written yet).

    What it cannot see: a slider Pratik moved BY HAND in darktable since the
    last commit (darktable writes nothing until the darkroom is left). That
    is what `bounce=True` is for. Local reads only; no bridge call.
    """
    if bounce and trust_sidecar:
        _contradiction()
    if bounce:
        return True, "forced (bounce)"
    if trust_sidecar:
        return False, "trust_sidecar: pre-read bounce skipped unconditionally"
    try:
        mtime = Path(resolved.xmp_path).stat().st_mtime
    except OSError:
        return True, "no sidecar yet"
    entries = journal.read_all(job)
    last = entries[-1] if entries else None
    if last is None or int(last.image_id) != int(resolved.image_id):
        fresh, _why = history.watermark_fresh(job, resolved.image_id, resolved.xmp_path)
        if fresh:
            return False, "sidecar matches Scouter's own last commit (watermark)"
        if last is None:
            return True, "first move on this image in this session"
        return True, f"image changed since the last move (was image {int(last.image_id)})"
    try:
        moved_at = datetime.fromisoformat(last.ts).timestamp()
    except (TypeError, ValueError):
        return True, "the last move's time is unreadable"
    if mtime < moved_at:
        return True, "sidecar is older than the last move (not committed yet)"
    return False, "sidecar is newer than the last move on this image"


def _maybe_precommit(job: str, resolved: ResolvedImage, *, bounce: bool,
                     trust_sidecar: bool, expect_module: str | None = None) -> tuple[str | None, dict]:
    """Run the guard, and the bounce if it asks for one. `(warning, plan_row)`."""
    do_it, reason = precommit_decision(job, resolved, bounce=bounce, trust_sidecar=trust_sidecar)
    warning = None
    t0 = time.monotonic()
    if do_it:
        warning = _precommit(job, resolved.image_id, expect_module=expect_module)
    ms = int((time.monotonic() - t0) * 1000) if do_it else 0
    return warning, {"action": "precommit", "bounced": do_it, "reason": reason, "ms": ms}


@metrics.instrumented("darkroom.get")
def get(module: str, param: str, *, instance: int = 0, bounce: bool = False,
        job: str | None = None) -> ValueResult:
    """A parameter's current VALUE. Never commits or moves the view unless
    `bounce=True`, which first asks darktable to write pending edits."""
    job = job or current_job()
    s = require_row(module, param)
    resolved = darkroom._current_image()
    pre_warning = None
    if bounce:
        dtbridge.require_darkroom()
        pre_warning = _precommit(job, resolved.image_id, expect_module=module)
    value, source, reason = read_current(resolved, s, instance, job)
    fresh, why = history.watermark_fresh(job, resolved.image_id, resolved.xmp_path)
    warnings: list[str] = [pre_warning] if pre_warning else []
    if not fresh and not bounce:
        warnings.append(f"sidecar not known-fresh ({why}): darktable may hold newer, uncommitted "
                        "changes; pass bounce to commit before reading")
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
           trust_sidecar: bool = False, bounce: bool = False, educative: str | None = None,
           dry_run: bool = False, job: str | None = None) -> AdjustResult:
    """Move one parameter by `delta` (internal units), relative to its current value."""
    job = job or current_job()
    s = require_row(module, param)
    delta = darkroom._require_finite("delta", delta)
    if delta == 0:
        raise UsageError("delta must be non-zero", detail={"argument": "delta"})
    level = explain.resolve_level(educative)
    gate(s, relative=True)
    if bounce and trust_sidecar:
        _contradiction()
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
    # Route A: a Kelvin adjust on a preset-4 white balance moves color
    # calibration's illuminant temperature instead (`darkroom.wb_route`).
    # The route is decided on the sidecar as it stands; if the pre-read
    # bounce runs, it is decided AGAIN on the sidecar the bounce wrote, so a
    # preset 4 that only the bounce reveals still routes (or refuses) before
    # any value is read. A relative move never reads the WB table's Kelvin
    # and then lands it on the illuminant (`darkroom.NO_WB_REROUTE`).
    asked = (module, param, instance)

    def _route() -> dict | None:
        m, p, i = asked
        if (m, p) == ("temperature", "temperature"):
            route = darkroom.wb_route(resolved.path, i)
            if route["route"] == "color_calibration":
                return route
        return None

    def _target(route: dict | None) -> tuple[str, str, int]:
        return (*darkroom.COLORCAL_WB, 0) if route is not None else asked

    routed = _route()
    module, param, instance = _target(routed)
    # Refused with nothing sent when the sidecar already shows preset 4 and
    # the route is unavailable; re-checked after the bounce below.
    darkroom.refuse_wb_late(resolved.path, module, [param], instance,
                            what=f"darkroom adjust {module}.{param}")
    dtbridge.require_darkroom()
    warning, pre = _maybe_precommit(job, resolved, bounce=bounce, trust_sidecar=trust_sidecar,
                                    expect_module=module)
    if pre["bounced"]:
        routed = _route()
        module, param, instance = _target(routed)
        darkroom.refuse_wb_late(resolved.path, module, [param], instance,
                                what=f"darkroom adjust {module}.{param}")
    token = darkroom.NO_WB_REROUTE.set(True)
    try:
        result = _adjust_core(module, param, delta, resolved=resolved, instance=instance,
                              commit=commit, level=level, job=job, ms_precommit=pre["ms"])
    finally:
        darkroom.NO_WB_REROUTE.reset(token)
    result.plan[0]["precommit"] = {"bounced": pre["bounced"], "reason": pre["reason"]}
    if routed is not None:
        result.warnings.append(darkroom.wb_routed_note(routed))
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


def _route_wb(resolved: ResolvedImage, concrete: list[tuple[str, phrases.Move]]
              ) -> tuple[list[tuple[str, phrases.Move]], dict | None]:
    """Route A (decision rows 458/461): on a preset-4 white balance whose
    color calibration route is usable, every Kelvin move of the phrase goes
    to `channelmixerrgb.temperature` -- same notch, same sign, same unit.
    Tint moves are left alone (the guard refuses them). Local reads only."""
    if not any(m == "temperature" and mv.param == "temperature" for m, mv in concrete):
        return concrete, None
    route = darkroom.wb_route(resolved.path, 0)
    if route["route"] != "color_calibration":
        return concrete, None
    module, param = darkroom.COLORCAL_WB
    return [(module, phrases.Move(module, param, mv.kind, mv.notches, mv.value))
            if (m, mv.param) == ("temperature", "temperature") else (m, mv)
            for m, mv in concrete], route


def _refuse_wb_late(phrase: str, key: str, resolved: ResolvedImage,
                    concrete: list[tuple[str, phrases.Move]]) -> None:
    """The whole phrase refuses, before any of its moves lands, when one of
    them would move a white balance in camera reference (late) mode (gauntlet
    §5.4 point 4: never a half-applied edit)."""
    wb_params = [mv.param for module, mv in concrete if module == "temperature"]
    if not wb_params:
        return
    darkroom.refuse_wb_late(
        resolved.path, "temperature", wb_params, 0, what=f"'{phrase}'",
        extra={"phrase": phrase, "key": key,
               "refused": [{"module": "temperature", "param": p, "code": "module_unavailable"}
                           for p in wb_params]})


def _raise_refused(phrase: str, key: str, refused: list[dict], total: int,
                   why: str = "are not reachable") -> None:
    first = refused[0]
    cls = {"not_pinned": NotPinned, "value_unknown": ValueUnknown}.get(first["code"], UsageError)
    raise cls(
        f"'{phrase}' cannot run: {len(refused)} of {total} move(s) {why} "
        f"({', '.join(r['module'] + '.' + r['param'] for r in refused)})",
        detail={"phrase": phrase, "key": key, "refused": refused},
    )


@metrics.instrumented("darkroom.say")
def say(phrase: str, *, amount: float | None = None, commit: bool = True,
        trust_sidecar: bool = False, bounce: bool = False, educative: str | None = None,
        dry_run: bool = False, job: str | None = None) -> SayResult:
    """A plain-words edit: every move of the phrase, as one undoable group (§6.3)."""
    job = job or current_job()
    res = phrases.resolve(phrase, amount)
    level = explain.resolve_level(educative)
    if bounce and trust_sidecar:
        _contradiction()
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
    tone = route_tone(resolved.path)
    base = [(tone if mv.module == phrases.TONE else mv.module, mv) for mv in res.moves]
    concrete, wb_routed = _route_wb(resolved, base)
    # Local sidecar read only: refused with nothing sent, not even the
    # view check or the pre-read bounce.
    _refuse_wb_late(phrase, res.key, resolved, concrete)
    dtbridge.require_darkroom()
    refused = [r for r in (_refusal(m, mv.param, mv.kind) for m, mv in concrete) if r]
    if refused:
        _raise_refused(phrase, res.key, refused, len(concrete))
    group = f"say-{journal.next_seq(job)}"
    warning, pre = _maybe_precommit(job, resolved, bounce=bounce, trust_sidecar=trust_sidecar)
    if pre["bounced"]:
        # The bounce can be what first writes the sidecar (or flushes a hand
        # edit): re-route and re-check before the first move, so a later WB
        # move can never refuse after an earlier move of the same phrase has
        # landed, and a preset 4 it reveals still goes to color calibration.
        concrete, wb_routed = _route_wb(resolved, base)
        _refuse_wb_late(phrase, res.key, resolved, concrete)
        refused = [r for r in (_refusal(m, mv.param, mv.kind) for m, mv in concrete) if r]
        if refused:
            _raise_refused(phrase, res.key, refused, len(concrete))
    # Final review #1a ruling, all-or-nothing: a `set` move whose before
    # value is unreadable and whose row declares no neutral/default would
    # journal an entry `undo_last` can only refuse. Refuse the whole phrase
    # now -- after the pre-commit (so the read matches what `set_param` will
    # see) and before any write.
    unreadable = []
    for module, mv in concrete:
        if mv.kind != "set":
            continue
        try:
            before_fallback(resolved, vocab.spec(module, mv.param), 0, job)
        except ValueUnknown as e:
            unreadable.append({"module": module, "param": mv.param, "code": e.code,
                               "status": vocab.status(vocab.spec(module, mv.param)),
                               "message": e.message, "reason": e.detail.get("reason")})
    if unreadable:
        _raise_refused(phrase, res.key, unreadable, len(concrete),
                       why="have no readable current value to undo to")
    landed: list[dict] = []
    notes: list[explain.MoveNote] = []
    failures: list[dict] = []
    token = journal.GROUP.set(group)
    no_reroute = darkroom.NO_WB_REROUTE.set(True)
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
                    # Re-read per move: an earlier move of this phrase may
                    # have given the module a history entry since the gate.
                    fallback = before_fallback(resolved, s, 0, job)
                    r = _set_recording_before(
                        module, mv.param, target, instance=0, commit=commit, job=job,
                        fallback=fallback)
                    if r.previous is None and fallback is not None and not r.noop:
                        r.previous = fallback[0]
                    notes.append(explain.MoveNote(s, r.previous, target, "set"))
                    landed.append({**r.to_dict(), "target": target,
                                   "summary": f"{s.ui}: set to {explain.with_unit(s, f'{vocab.to_display(s, target):.4g}')}"})
            except ScouterError as e:
                if not landed:
                    raise
                # Row 433 / spec 5.4 point 4: a move that was SENT but did
                # not verify is reported with what the sidecar really says
                # (`detail`), so every write the phrase made is accounted for.
                failures.append({"module": module, "param": mv.param, "code": e.code,
                                 "message": e.message, "detail": e.detail})
                break
    finally:
        darkroom.NO_WB_REROUTE.reset(no_reroute)
        journal.GROUP.reset(token)
    return SayResult(
        outcome="partial" if failures else "ok",
        image_id=resolved.image_id, path=resolved.path, phrase=phrase, key=res.key,
        intensity=res.intensity, group=group, moves=landed, refused=failures,
        summary="; ".join(m.get("summary", "") for m in landed),
        explain=explain.lines(notes, level=level, job=job),
        warnings=[w for w in [warning, wb_routed and darkroom.wb_routed_note(wb_routed)] if w],
        plan=[pre],
    )


EDUCATIVE_ARG = ArgSpec("educative", "str", default=None, choices=("off", "brief", "full"),
                        help="explanation level for this call (default: config's educative)")
TRUST_ARG = ArgSpec("trust_sidecar", "bool", default=False,
                    help="never bounce before reading, even when the staleness guard would")
BOUNCE_ARG = ArgSpec("bounce", "bool", default=False,
                     help="force the pre-read commit bounce (after moving sliders by hand)")

for verb in (
    Verb(
        id="darkroom.get",
        group="darkroom",
        fn=get,
        args=(darkroom.MODULE_ARG, darkroom.PARAM_ARG, darkroom.INSTANCE_ARG, BOUNCE_ARG),
        writes=False,
        gated=False,
        summary="Read one parameter's current value from the XMP (moves the view only with "
        "--bounce).",
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
            BOUNCE_ARG,
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
            BOUNCE_ARG,
            EDUCATIVE_ARG,
            darkroom.DRY_RUN_ARG,
        ),
        writes=True,
        gated=False,
        summary="Edit by a plain-words phrase; its moves undo together.",
    ),
):
    register(verb)
