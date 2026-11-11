"""Layer 4 executors: `chart_golden` (P7 + per-run calibration) and
`look_plan` (a look's reachable targets -> absolute Scouter moves)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from gauntlet.adapter import CallRecord
from gauntlet.case import case_id
from tests.gauntlet import chart_render
from tests.gauntlet.execute import VIRTUAL
from tests.gauntlet.session import SessionBlocked

CAL_FILE = "calibration.json"
GOLDEN_ID = case_id("lk", "golden", "chart")
#: Parallel workers: how long a look case waits for the calibration the golden
#: case (in another shard) is writing. Waited at most once per process; after
#: that a missing calibration blocks at once.
CAL_WAIT_ENV = "GAUNTLET_CAL_WAIT_S"
CAL_WAIT_DEFAULT_S = 900.0
_waited = False


def _cal_path(session) -> Path:
    return session.run_root / "chart" / CAL_FILE


class CalibrationMissing(Exception):
    """The chart calibration a look needs is absent or unreadable: a harness
    problem (ordering / environment), never a Scouter result."""


def _read_cal(session) -> dict | None:
    try:
        cal = json.loads(_cal_path(session).read_text())
    except (OSError, ValueError):
        return None
    return cal if isinstance(cal, dict) and "_base_slope" in cal and "_base_C" in cal else None


def _golden_pending(session) -> bool:
    """True when the golden (calibration) case is scheduled in a parallel shard
    of this run and has not produced a result yet."""
    root = session.run_root
    if (root / "cases" / f"{GOLDEN_ID}.json").exists():
        return False
    try:
        return any(
            GOLDEN_ID in p.read_text().split() for p in (root / "workers").glob("w*/cases.txt")
        )
    except OSError:
        return False


def load_calibration(session, *, sleep=time.sleep, clock=time.monotonic) -> dict:
    """The run's chart calibration, or `CalibrationMissing`. Under `--workers N`
    the golden case runs first in whichever shard owns it; a look case in another
    shard waits (bounded, once per process) for the shared run-dir file."""
    global _waited
    cal = _read_cal(session)
    if cal is not None:
        return cal
    if os.environ.get("GAUNTLET_SHARD") and not _waited and _golden_pending(session):
        _waited = True
        deadline = clock() + float(os.environ.get(CAL_WAIT_ENV, CAL_WAIT_DEFAULT_S))
        while clock() < deadline and _golden_pending(session):
            cal = _read_cal(session)
            if cal is not None:
                return cal
            sleep(2.0)
        cal = _read_cal(session)
        if cal is not None:
            return cal
    raise CalibrationMissing(
        f"chart calibration missing or unreadable ({_cal_path(session)}); run the "
        f"{GOLDEN_ID} case first (it measures it)"
    )


def _probe_base(module: str, param: str) -> tuple[float | None, str | None]:
    """The value the calibration probe sets FROM, and where it came from:
    `(value, source)` with `source` one of `"neutral"`, `"default"`,
    `"default_candidate"`, or `None` (nothing declared). Read from
    `scouter.core.vocab`, never a number picked here.

    The PFM chart's pipeline is colorin/colorout/gamma only (row
    LK-calibration-holes): sigmoid, exposure and temperature never have an
    active history entry on it, so a RELATIVE `darkroom_adjust` has no
    current value to add its delta to and refuses `value_unknown` -- for
    every row here, not only the ones with no declared neutral/default
    (`gate`/`read_current`, `scouter/core/moves.py`). An ABSOLUTE
    `darkroom_set` needs no current value at all, so probing with
    `base + amount` (an absolute target) instead of a bare `amount` (a
    delta) reaches the module regardless.

    A row may also declare only `default_candidate` (row 474: a value
    believed correct but not yet host-verified -- §4.3 leaves its `default`
    column blank and no host probe has pinned it; `exposure.black` was one
    such row until row 495/496's host probe promoted it to a cited
    `default`, so this function returns it via the `"default"` branch above
    now, ahead of this one). This function still returns a candidate when it
    is all a row has, so `chart_golden` can probe from it and measure a real
    slope -- but the caller must record the result as unverified
    (`calibration_errors[key] = "default_unverified"`), never as a settled
    measurement, until the row is promoted to `default`.

    When neither `neutral`, `default`, nor `default_candidate` is declared
    for a row (still true for `temperature.temperature` -- the PFM chart has
    no white-balance module and no camera EXIF, so there is no honest Kelvin
    to probe from), there is nothing to build an absolute target from
    either; the caller must skip probing that key and record it as a
    calibration failure, never silently a zero slope.
    """
    from scouter.core import vocab

    row = vocab.spec(module, param)
    if row is None:
        return None, None
    if row.neutral is not None:
        return float(row.neutral), "neutral"
    if row.default is not None:
        return float(row.default), "default"
    if row.default_candidate is not None:
        return float(row.default_candidate), "default_candidate"
    return None, None


def chart_golden(session, case, step, obs) -> CallRecord:
    try:
        frame = session.ensure_frame("chart")
        work = session.root / "render"
        first = chart_render.render_stats(frame, work / "golden-a.ppm")
        second = chart_render.render_stats(frame, work / "golden-b.ppm")
        repeat = chart_render.compare_golden(second, golden=_tmp_golden(session, first))
        golden = chart_render.compare_golden(first)
        chart_render.write_golden_candidate(session.run_root, first)
        obs.extra["patch_de"] = golden["patch_de"] or repeat["patch_de"]
        obs.extra["golden"] = golden
        cal: dict[str, float | None] = {}
        cal_errors: dict[str, str] = {}
        for key, amount in chart_render.PROBE.items():
            module, param = key.split(".")
            base, source = _probe_base(module, param)
            if base is None:
                cal[key] = None
                cal_errors[key] = (
                    f"{key} has no declared neutral, darktable default, or "
                    "default candidate in the vocabulary to probe from"
                )
                continue
            rec = session.call(
                "darkroom_set",
                {"module": module, "param": param, "value": base + amount},
                8.0,
                write=True,
            )
            if rec.error or not (rec.envelope or {}).get("ok"):
                cal[key] = None
                cal_errors[key] = rec.error or "darkroom_set did not report ok"
                continue
            after = chart_render.render_stats(frame, work / f"cal-{key}.ppm")
            cal[key] = chart_render.slope(first, after, key, amount)
            session.call("darkroom_undo_last", {}, 8.0, write=True)
            if source == "default_candidate":
                # Row 474: the probe DID reach the module and measured a real
                # slope, but from a candidate base no host probe has pinned
                # yet (see vocab.ParamSpec.default_candidate) -- record it as
                # unverified so a look depending on it is treated exactly
                # like a skipped calibration (never `verified`), not folded
                # in as a settled measurement.
                cal_errors[key] = "default_unverified"
        cal["_base_slope"] = first["chart_tone"]["slope"]
        cal["_base_C"] = first["chart_chromatic"]["C"]
        if cal_errors:
            cal["_errors"] = cal_errors
        (session.run_root / "chart").mkdir(parents=True, exist_ok=True)
        _cal_path(session).write_text(json.dumps(cal, indent=2, sort_keys=True) + "\n")
        obs.extra["calibration"] = cal
        if cal_errors:
            # Reported, never silently folded into a falsy 0.0 slope: a
            # calibration hole this run hit, distinct from a genuinely flat
            # (zero) measured slope.
            obs.extra["calibration_errors"] = cal_errors
        #: L6: a key with no declared neutral/default to probe from is a declared
        #: gap (W3/C3), not a calibration failure: it stays in calibration_errors
        #: but does not make calibration_ok false. Every other error still does.
        gap_keys = {k for k, e in cal_errors.items() if "has no declared neutral" in e}
        obs.extra["calibration_ok"] = all(k in gap_keys for k in cal_errors) and all(
            v not in (None, 0.0)
            for k, v in cal.items()
            if not k.startswith("_") and k not in gap_keys
        )
        return CallRecord(
            "chart_golden", {}, {"ok": True, "outcome": "ok", "data": {"golden": golden["verdict"]}}
        )
    except SessionBlocked as e:
        obs.blocked = f"session: {e}"
        return CallRecord("chart_golden", {}, error=f"blocked: {e}")


def _tmp_golden(session, stats) -> Path:
    p = session.root / "render" / "repeat-golden.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"labs": chart_render.labs(stats)}))
    return p


def plan_moves(
    look: dict, cal: dict, current: dict
) -> tuple[list[tuple[str, str, float]], list[str]]:
    """[(module, param, absolute value)], notes. Linear per-param solve."""
    from scouter.core import vocab

    t = look["targets"]
    moves: list[tuple[str, str, float]] = []
    notes: list[str] = []

    def add(key: str, delta_metric: float) -> None:
        s = cal.get(key)
        error = cal.get("_errors", {}).get(key)
        if not s or error:
            # Row 474: a key whose calibration came back unverified (e.g.
            # `exposure.black` probed from an un-pinned `default_candidate`)
            # is planned exactly like a key with no calibration at all --
            # never a move built from a measurement no host probe has
            # confirmed.
            notes.append(
                f"no calibration for {key}" if not s else f"{key} calibration unverified: {error}"
            )
            return
        module, param = key.split(".")
        row = vocab.spec(module, param)
        # A key absent from the frame's history (the PFM chart never has sigmoid
        # active) starts from its declared neutral, else its verified default,
        # never 0.0 (round 2b: 0.0 + delta clamped sigmoid.contrast to its 0.5 floor).
        declared = row.neutral if row.neutral is not None else row.default
        base = current.get(key, declared if declared is not None else 0.0)
        target, _clamped = vocab.clamp(row, base + delta_metric / s)
        moves.append((module, param, round(target, 4)))

    tone = t.get("tone") or {}
    for k, key in (
        ("shadows_ev", "colorbalancergb.shadows_luminance"),
        ("mids_ev", "colorbalancergb.midtones_luminance"),
        ("highlights_ev", "colorbalancergb.highlights_luminance"),
    ):
        if tone.get(k):
            add(key, tone[k])
    if tone.get("contrast"):
        if "_base_slope" in cal:  # contrast is a fraction of the untouched slope
            add("sigmoid.contrast", tone["contrast"] * cal["_base_slope"])
        else:
            notes.append("no base slope measured")
    sat = t.get("saturation_delta") or 0
    if sat == -1:
        moves.append(("colorbalancergb", "global_saturation", -0.5))
        notes.append("monochrome approximated by global saturation at its lower bound")
    elif sat:
        if "_base_C" in cal:  # saturation is a fraction of the untouched mean C*
            add("colorbalancergb.global_chroma", sat * cal["_base_C"])
        else:
            notes.append("no base chroma measured")
    gc = t.get("grey_cast") or {}
    if gc.get("b"):
        add("temperature.temperature", gc["b"])
    if gc.get("a"):
        moves.append(("temperature", "tint", 0.9 if gc["a"] > 0 else 1.1))
    st = t.get("split_toning") or {}
    for end, zone in (("shadow", "shadows"), ("highlight", "highlights")):
        if st.get(f"{end}_hue") is not None and st.get(f"{end}_sat"):
            moves.append(("colorbalancergb", f"{zone}_hue", float(st[f"{end}_hue"]) % 360))
            moves.append(("colorbalancergb", f"{zone}_chroma", min(0.3, float(st[f"{end}_sat"]))))
    if t.get("black_lift"):
        add("exposure.black", t["black_lift"])
    return moves, notes


def look_plan(session, case, step, obs) -> CallRecord:
    try:
        from tests.gauntlet.taxonomy import load

        look = next(x for x in load().looks if x["id"] == step.args["look_id"])
        cal = load_calibration(session)
        moves, notes = plan_moves(look, cal, obs.before.get("params", {}))
        sub = []
        for module, param, value in moves:
            r = session.call(
                "darkroom_set", {"module": module, "param": param, "value": value}, 8.0, write=True
            )
            sub.append(
                {
                    "tool": "darkroom_set",
                    "module": module,
                    "param": param,
                    "value": value,
                    "ok": bool((r.envelope or {}).get("ok")),
                    "error": r.error,
                }
            )
        obs.extra["look_moves"] = sub
        obs.extra["look_notes"] = notes
        ok = all(m["ok"] for m in sub)
        return CallRecord(
            "look_plan",
            step.args,
            {"ok": ok, "outcome": "ok" if ok else "error:partial", "data": {"moves": sub}},
            write=True,
        )
    except CalibrationMissing as e:
        obs.blocked = f"harness_error: look_plan: {e}"
        return CallRecord("look_plan", step.args, error=f"blocked: {e}")
    except SessionBlocked as e:
        obs.blocked = f"session: {e}"
        return CallRecord("look_plan", step.args, error=f"blocked: {e}")


VIRTUAL["chart_golden"] = chart_golden
VIRTUAL["look_plan"] = look_plan
