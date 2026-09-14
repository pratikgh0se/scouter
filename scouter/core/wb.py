"""Kelvin <-> coefficients, table lookup only.

Ruling R19 (controller, task 9): `outliers.list` needs `wb.kelvin` to turn a
frame's `temperature` coefficients into a Kelvin value it can diff against
the hero's, but the real white-balance machinery -- `wb.calibrate`, the
coefficient-slider probe, and the journalled `temperature` undo -- is task
11's job (it needs a host probe this task is not scoped to run). This module
is deliberately the minimal slice task 9 needs and nothing more: read a
per-camera calibration table from `paths.wb_dir()` and invert Kelvin from it.
No `wb.calibrate` verb, no probe, no registry entry here -- task 11 adds
those, plus `wb.kelvin` as a registered verb of its own.

Table shape (written by `scouter wb calibrate`, task 11): a JSON array of
`{"kelvin": int, "coeffs": [r, g, b]}` points. Inversion bisects the sorted
samples on the r/b coefficient ratio (monotone in Kelvin at fixed tint,
task 11's own calibration check enforces this when the table is written) and
interpolates linearly between the two bracketing points; within 1% of a
sample counts as `exact`. Outside the sampled range the answer is `None`
with a reason -- extrapolating a blackbody curve past the measurements would
be exactly the invented value this project refuses to produce.
"""

from __future__ import annotations

import json
from pathlib import Path

from scouter import paths

#: How close `target` has to be to a sample's own ratio to call it `exact`
#: rather than interpolated.
_EXACT_TOLERANCE = 0.01


def _table_path(camera: str) -> Path:
    return paths.wb_dir() / f"{camera}.json"


def load_table(camera: str) -> list[dict] | None:
    """`camera`'s calibration table, sorted by Kelvin, or `None` when no table
    file exists for it. Raises `ValueError` if the file exists but is not the
    documented shape -- a malformed table must never be silently ignored."""
    path = _table_path(camera)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(data, list) or not data:
        raise ValueError(f"{path} must hold a non-empty JSON array of calibration points")
    for point in data:
        if (
            not isinstance(point, dict)
            or "kelvin" not in point
            or "coeffs" not in point
            or len(point["coeffs"]) < 3
        ):
            raise ValueError(
                f"{path}: every point needs 'kelvin' and a 3+ element 'coeffs' list, got {point!r}"
            )
    return sorted(data, key=lambda p: p["kelvin"])


def _ratio(coeffs: list[float]) -> float:
    """r/b: monotone in Kelvin at fixed tint (task 11's calibration check)."""
    return float(coeffs[0]) / float(coeffs[2])


def _no_table_result(camera: str) -> dict:
    return {
        "kelvin": None,
        "interpolated": False,
        "bracket": None,
        "residual_ratio": None,
        "exact": False,
        "camera": camera,
        "table": None,
        "reason": f"no wb calibration table for {camera}; run scouter wb calibrate",
    }


def kelvin(coeffs: list[float], camera: str) -> dict:
    """Invert Kelvin from `coeffs` (r, g, b, ...) against `camera`'s table.

    Result shape (task 11's own): `{kelvin, interpolated, bracket,
    residual_ratio, exact, camera, table, reason}`. No table -> `kelvin: None`
    with a reason naming `scouter wb calibrate`. Outside the calibrated
    range -> `kelvin: None` with a reason naming the range, never
    extrapolated.
    """
    try:
        table = load_table(camera)
    except ValueError as e:
        result = _no_table_result(camera)
        result["reason"] = str(e)
        return result
    if not table:
        return _no_table_result(camera)

    table_path = str(_table_path(camera))
    target = _ratio(coeffs)
    ratios = [_ratio(p["coeffs"]) for p in table]

    for point, r in zip(table, ratios):
        if r != 0 and abs(target - r) / abs(r) <= _EXACT_TOLERANCE:
            return {
                "kelvin": point["kelvin"],
                "interpolated": False,
                "bracket": None,
                "residual_ratio": 0.0,
                "exact": True,
                "camera": camera,
                "table": table_path,
                "reason": None,
            }

    lo_bound, hi_bound = ratios[0], ratios[-1]
    if target < lo_bound or target > hi_bound:
        return {
            "kelvin": None,
            "interpolated": False,
            "bracket": None,
            "residual_ratio": None,
            "exact": False,
            "camera": camera,
            "table": table_path,
            "reason": (
                f"r/b ratio {target:.4f} is outside {camera}'s calibrated range "
                f"[{lo_bound:.4f}, {hi_bound:.4f}] ({table[0]['kelvin']}-{table[-1]['kelvin']} K); "
                "extrapolating past the measurements would be an invented value"
            ),
        }

    for i in range(len(table) - 1):
        lo_r, hi_r = ratios[i], ratios[i + 1]
        if lo_r <= target <= hi_r:
            lo, hi = table[i], table[i + 1]
            frac = (target - lo_r) / (hi_r - lo_r) if hi_r != lo_r else 0.0
            k = lo["kelvin"] + frac * (hi["kelvin"] - lo["kelvin"])
            return {
                "kelvin": k,
                "interpolated": True,
                "bracket": [lo["kelvin"], hi["kelvin"]],
                "residual_ratio": target - lo_r,
                "exact": False,
                "camera": camera,
                "table": table_path,
                "reason": None,
            }

    # Unreachable: the range check above already guarantees a bracket exists
    # between two consecutive samples somewhere in the sorted list.
    raise AssertionError(f"{camera}: {target!r} in range but no bracket found -- table not sorted?")
