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

Table shape (written by `scouter wb calibrate`, task 11): a JSON object
`{"synthetic": bool, "note": str | null, "points": [{"kelvin": int,
"coeffs": [r, g, b]}, ...]}` -- `synthetic: true` marks a placeholder table
(fix round 1: `tests/fixtures/wb-em10iii.json` is one, until task 11 commits
the real measured points under the same name with `synthetic: false`). A
bare JSON array of points (no `synthetic`/`note` wrapper) is also accepted,
read as `synthetic: false`, for forward compatibility with a table written
before this wrapper existed. Inversion bisects the sorted points on the r/b
coefficient ratio (monotone in Kelvin at fixed tint, task 11's own
calibration check enforces this when the table is written; this module
checks it again on every load, so a table hand-edited or corrupted after the
fact is refused rather than silently mis-inverted) and interpolates linearly
between the two bracketing points; within 1% of a point counts as `exact`.
Outside the sampled range the answer is `None` with a reason -- extrapolating
a blackbody curve past the measurements would be exactly the invented value
this project refuses to produce.
"""

from __future__ import annotations

import json
from pathlib import Path

from scouter import paths
from scouter.errors import ScouterError, UsageError

#: How close `target` has to be to a sample's own ratio to call it `exact`
#: rather than interpolated.
_EXACT_TOLERANCE = 0.01


class NonMonotonicTable(ScouterError):
    """A camera's calibration table is not monotone in the r/b ratio.

    `wb.calibrate` (task 11) refuses to WRITE a non-monotone table in the
    first place, so seeing this means the table on disk was hand-edited or
    corrupted after the fact -- `kelvin`'s bisection cannot honestly pick a
    bracket for a target ratio it has already confirmed sits inside the
    table's own range.
    """

    code = "non_monotone_table"
    fix = (
        "recalibrate: `scouter wb calibrate --confirm` refuses to write a "
        "table whose r/b ratio isn't strictly increasing in Kelvin"
    )


def _table_path(camera: str) -> Path:
    return paths.wb_dir() / f"{camera}.json"


def _ratio(coeffs: list[float]) -> float:
    """r/b: monotone in Kelvin at fixed tint (task 11's calibration check)."""
    b = float(coeffs[2])
    if b == 0:
        raise UsageError(
            f"coefficient vector {list(coeffs)!r} has a zero blue coefficient: "
            "cannot form an r/b ratio",
            detail={"coeffs": list(coeffs)},
        )
    return float(coeffs[0]) / b


def _validate_points(path: Path, points: object) -> list[dict]:
    """Shape-check `points`, sort by Kelvin, and confirm the r/b ratio is
    strictly increasing across the sorted points.

    The monotonicity check is proactive, not just a defensive fallback:
    `wb.calibrate` (task 11) refuses to WRITE a non-monotone table, but a
    table can still be hand-edited or corrupted on disk afterwards, and a
    non-monotone table can silently pick the WRONG bracket in `kelvin`'s
    interpolation (the first adjacent pair that happens to straddle the
    target, not necessarily the intended one) without ever tripping an
    "unreachable" fallback -- checking it here, once, at load time, is what
    makes that impossible rather than merely unlikely.
    """
    if not isinstance(points, list) or not points:
        raise ValueError(f"{path} must hold a non-empty array of calibration points")
    for point in points:
        if (
            not isinstance(point, dict)
            or "kelvin" not in point
            or "coeffs" not in point
            or len(point["coeffs"]) < 3
        ):
            raise ValueError(
                f"{path}: every point needs 'kelvin' and a 3+ element 'coeffs' list, got {point!r}"
            )
    sorted_points = sorted(points, key=lambda p: p["kelvin"])
    ratios = [_ratio(p["coeffs"]) for p in sorted_points]
    for i in range(len(ratios) - 1):
        if ratios[i + 1] <= ratios[i]:
            a, b = sorted_points[i], sorted_points[i + 1]
            raise NonMonotonicTable(
                f"{path}: r/b ratio is not strictly increasing between "
                f"{a['kelvin']} K (ratio {ratios[i]:.4f}) and {b['kelvin']} K "
                f"(ratio {ratios[i + 1]:.4f}) -- a non-monotone table cannot be inverted",
                detail={
                    "table": str(path),
                    "kelvin_a": a["kelvin"],
                    "ratio_a": ratios[i],
                    "kelvin_b": b["kelvin"],
                    "ratio_b": ratios[i + 1],
                },
            )
    return sorted_points


def _read_table_file(camera: str) -> dict | None:
    """The raw parsed table file: `{"synthetic": bool, "note": str | None,
    "points": [...]}` (points sorted by Kelvin), or `None` when no table file
    exists for `camera`. Raises `ValueError` if the file exists but is not
    the documented shape -- a malformed table must never be silently ignored.

    Internal: `load_table` (the public, task-9-frozen interface) returns just
    the points, unchanged; `kelvin` uses this directly so it can surface
    `synthetic` in its own result without changing `load_table`'s shape.
    """
    path = _table_path(camera)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not valid JSON: {e}") from e
    if isinstance(data, list):
        # Forward-compat: a bare points array, written before the
        # synthetic/note wrapper existed.
        return {"synthetic": False, "note": None, "points": _validate_points(path, data)}
    if isinstance(data, dict) and "points" in data:
        return {
            "synthetic": bool(data.get("synthetic", False)),
            "note": data.get("note"),
            "points": _validate_points(path, data["points"]),
        }
    raise ValueError(f"{path} must hold either a points array, or an object with a 'points' array")


def load_table(camera: str) -> list[dict] | None:
    """`camera`'s calibration points, sorted by Kelvin, or `None` when no
    table file exists for it. Task 9's frozen public shape: just the points,
    same as before the synthetic/note wrapper -- use `kelvin`'s own `table`
    field to learn whether the table is synthetic."""
    parsed = _read_table_file(camera)
    return parsed["points"] if parsed is not None else None


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
    extrapolated. `table`, when a table was found, is `{"path": str,
    "synthetic": bool}` (fix round 1) -- a synthetic (placeholder) table is
    perfectly usable for testing the plumbing, but a caller reading the
    result can tell it apart from a real measured one.
    """
    try:
        parsed = _read_table_file(camera)
    except ValueError as e:
        result = _no_table_result(camera)
        result["reason"] = str(e)
        return result
    if parsed is None:
        return _no_table_result(camera)

    table = parsed["points"]
    table_ref = {"path": str(_table_path(camera)), "synthetic": parsed["synthetic"]}
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
                "table": table_ref,
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
            "table": table_ref,
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
                "table": table_ref,
                "reason": None,
            }

    # Unreachable in practice: `_validate_points` already confirmed the
    # table's r/b ratio is strictly increasing before this point ever ran,
    # and the range check above guarantees a bracket exists somewhere in a
    # genuinely monotone, sorted sequence. Kept as a typed refusal rather
    # than a bare assertion anyway (fix round 1) -- belt and braces naming
    # the table, not a crash with no code a caller could match on.
    raise NonMonotonicTable(
        f"{_table_path(camera)}: r/b ratio {target:.4f} is inside the table's own range "
        f"[{lo_bound:.4f}, {hi_bound:.4f}] but no consecutive pair of points brackets it -- "
        "the table is not monotone in r/b",
        detail={"camera": camera, "table": str(_table_path(camera)), "target_ratio": target},
    )
