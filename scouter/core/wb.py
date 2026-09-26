"""Kelvin <-> coefficients, honestly.

THE FACT (phase 1 design 7.2, verified against the real sidecar): darktable's
`temperature` module stores modversion 4 params as `<4f i` =
coeff_r, coeff_g, coeff_b, coeff_g2, preset -- camera-relative multipliers, with
coeff_g2 = inf on this body. The UI slider is Kelvin. darktable converts between
them inside src/iop/temperature.c using (a) the camera's XYZ->camera matrix from
its own camera-support data and (b) a blackbody/daylight illuminant model.
NEITHER input is in the XMP, and Scouter may not read darktable's internal data
files as an API.

SO: a faithful closed-form conversion from the XMP alone is NOT POSSIBLE, and
nothing here pretends otherwise. What is possible, and is what this module
does (design §11's own tier numbers, `scouter/core/darkroom.py`'s
`undo_last` implements 2-4):
  1. (design 11.2, unconditional) journal the whole coefficient vector
     before every temperature write;
  2. (design 11.3, probe-gated) restore it through the coefficient sliders,
     whose action paths are PINNED by a host probe
     (spike/scouter_wb_probe.lua) -- an exact undo;
  3. (design 11.4, built regardless) invert Kelvin from a MEASURED
     per-camera table (`wb.calibrate`), because the coefficient ratio is
     monotone in Kelvin at fixed tint;
  4. (design 11.5) and when none of that is available, refuse -- handing
     over the exact coefficients, which is a different thing from shrugging.

Table shape (design 11.4, written by `scouter wb calibrate`),
`~/.config/scouter/wb/<camera>.json`:

    {"version": 1, "camera": "em10iii", "camera_model": "E-M10 Mark III",
     "measured_at": "2026-09-14T00:42:42+00:00", "darktable": "5.4.1",
     "tint": 1.0, "source_image": "em10iii.ORF", "synthetic": false,
     "samples": [{"k": 3000, "coeff_r": 1.34, "coeff_g": 1.0, "coeff_b": 2.93},
                 {"k": 4000, "coeff_r": 1.70, "coeff_g": 1.0, "coeff_b": 2.24}]}

`load_table` also reads two OLDER shapes, both from task 9 (merged in from
`phase-2`, which branched off before this module's object shape landed):
task 9's original bare JSON array (`[{"kelvin": int, "coeffs": [r, g, b]},
...]`, no provenance at all -- normalised with `synthetic: true` ASSUMED and
a warning surfaced through `wb.kelvin`'s own `warnings`, since there is no
provenance to trust), and task 9's own `{"synthetic": bool, "note": str |
null, "points": [{"kelvin": int, "coeffs": [r, g, b]}, ...]}` wrapper (an
"object table" with its OWN `synthetic` field, used as-is -- no warning,
since the writer already said what it is).

Inversion bisects the sorted samples on the r/b coefficient ratio (monotone in
Kelvin at fixed tint -- `calibrate` itself refuses to write a table that
isn't, and `load_table` checks it again on every load, so a table hand-edited
or corrupted on disk afterwards is refused rather than silently mis-inverted)
and interpolates linearly between the two bracketing points; within 1% of a
sample counts as `exact`. Outside the sampled range the answer is `None` with
a reason -- extrapolating a blackbody curve past the measurements would be
exactly the invented value this project refuses to produce.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from scouter import metrics, paths
from scouter.core import dtbridge, xmp
from scouter.errors import ScouterError, UsageError
from scouter.paths import current_job
from scouter.registry import ArgSpec, Verb, register
from scouter.types import CalibrateResult, KelvinResult


class WbCalibrationUnreadable(ScouterError):
    """One sweep point's `temperature` coefficients were unusable for the r/b
    ratio: `None` (never decoded, or `inf`/`nan` -- `xmp.latest_params`
    already cleans those to `None`), or a zero `coeff_b`, which would
    divide-by-zero the ratio. Typed (fix round 1, should-fix) so a caller
    can tell "the sweep genuinely produced garbage at this point" apart from
    any other unclassified failure.
    """

    code = "wb_calibration_unreadable"
    fix = "check darkroom.status/history for what actually landed at that Kelvin point and re-run"


class NonMonotonicTable(ScouterError):
    """A camera's calibration table is not monotone in the r/b ratio.

    `wb.calibrate` refuses to WRITE a non-monotone table in the first place,
    so seeing this means the table on disk was hand-edited or corrupted
    after the fact -- `load_table` checks every table's monotonicity on
    every load (merged in from task 9's fix round, `phase-2`), so `kelvin`'s
    bisection never has to silently pick the wrong bracket for a target
    ratio it has already confirmed sits inside the table's own range.
    """

    code = "non_monotone_table"
    fix = (
        "recalibrate: `scouter wb calibrate --confirm` refuses to write a "
        "table whose r/b ratio isn't strictly increasing in Kelvin"
    )


#: How close `target` has to be to a sample's own ratio to call it `exact`
#: rather than interpolated.
_EXACT_TOLERANCE = 0.01
#: W4 (row 437): the table is measured at one tint (the file's `tint`, 1.0).
#: r/b alone is tint-dependent, so a frame at another tint inverts to a wrong
#: Kelvin (5934 K read back for a 5500 K set). When the observed g-normalised
#: (r, b) sits farther than this from the table's own curve at the inverted
#: Kelvin, the Kelvin is not readable: answer None with a reason instead.
_OFF_CURVE_TOLERANCE = 0.03

#: `calibrate`'s own measured cost (task 11 host runs): about how long one
#: Kelvin point (a `darkroom.set` + commit + XMP readback) takes.
_SECONDS_PER_POINT = 1.8

_TEMPERATURE_COEFF_NAMES = ("coeff_r", "coeff_g", "coeff_b")


def _table_path(camera: str) -> Path:
    return paths.wb_dir() / f"{camera}.json"


#: Every field `load_table` carries through from the object shape (design
#: 11.4), besides `samples` itself (normalised separately) and the derived
#: `synthetic`/`note`/`warnings` this module adds.
_TABLE_METADATA_FIELDS = (
    "version",
    "camera",
    "camera_model",
    "measured_at",
    "darktable",
    "tint",
    "source_image",
)


def _validate_object_sample(path: Path, point: object) -> None:
    if (
        not isinstance(point, dict)
        or "k" not in point
        or any(name not in point for name in _TEMPERATURE_COEFF_NAMES)
    ):
        raise ValueError(
            f"{path}: every sample needs 'k', 'coeff_r', 'coeff_g' and 'coeff_b', got {point!r}"
        )


def _validate_legacy_point(path: Path, point: object) -> None:
    if (
        not isinstance(point, dict)
        or "kelvin" not in point
        or "coeffs" not in point
        or len(point["coeffs"]) < 3
    ):
        raise ValueError(
            f"{path}: every point needs 'kelvin' and a 3+ element 'coeffs' list, got {point!r}"
        )


def _check_monotonic(path: Path, samples: list[dict]) -> None:
    """Raise `NonMonotonicTable` unless `samples` (already sorted by Kelvin)
    has a strictly increasing r/b ratio throughout.

    Merged in from task 9's fix round (`phase-2`): `wb.calibrate` refuses to
    WRITE a non-monotone table, but a table can still be hand-edited or
    corrupted on disk afterwards, and a non-monotone table can silently pick
    the WRONG bracket in `_invert` (the first adjacent pair that happens to
    straddle the target, not necessarily the intended one) without ever
    tripping an "unreachable" fallback -- checking it here, once, at load
    time, is what makes that impossible rather than merely unlikely.
    """
    ratios = [_ratio(p["coeffs"]) for p in samples]
    for i in range(len(ratios) - 1):
        if ratios[i + 1] <= ratios[i]:
            a, b = samples[i], samples[i + 1]
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


def load_table(camera: str) -> dict | None:
    """`camera`'s full calibration table, or `None` when no table file exists.

    Reads three shapes: the object shape design 11.4 documents (`{version,
    camera, camera_model, measured_at, darktable, tint, source_image,
    synthetic, samples: [{k, coeff_r, coeff_g, coeff_b}, ...]}`, written by
    this module's own `calibrate`); task 9's own `{synthetic, note, points:
    [{kelvin, coeffs}, ...]}` wrapper (merged in from `phase-2`) -- an
    "object table" that already carries its own `synthetic` field, used
    as-is; and task 9's ORIGINAL bare JSON array (`[{"kelvin": int, "coeffs":
    [r, g, b]}, ...]`, no provenance at all) -- normalised with `synthetic:
    true` ASSUMED and a warning in the returned `warnings` list, never
    silently treated as equally trustworthy as a self-describing table.

    The returned dict's `samples` is always `[{"kelvin": int, "coeffs": [r,
    g, b]}, ...]`, sorted by Kelvin -- `_invert`'s own internal shape,
    regardless of which external shape the file used. Every load also
    re-checks the table's monotonicity (`_check_monotonic`, raising
    `NonMonotonicTable`) -- not just at write time. Raises `ValueError` if
    the file exists but matches none of the three shapes -- a malformed
    table must never be silently ignored.
    """
    path = _table_path(camera)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not valid JSON: {e}") from e

    warnings: list[str] = []
    note: str | None = None
    if isinstance(data, list):
        if not data:
            raise ValueError(f"{path} must hold a non-empty JSON array of calibration points")
        for point in data:
            _validate_legacy_point(path, point)
        samples = [{"kelvin": p["kelvin"], "coeffs": list(p["coeffs"])} for p in data]
        table: dict = dict.fromkeys(_TABLE_METADATA_FIELDS)
        table["version"] = 1
        table["camera"] = camera
        table["synthetic"] = True
        warnings.append(
            f"{path} is the legacy bare-array shape (task 9, no provenance) -- treated as "
            "synthetic; run `scouter wb calibrate` for a real, provenanced table"
        )
    elif isinstance(data, dict) and isinstance(data.get("samples"), list) and data["samples"]:
        version = data.get("version", 1)
        if version != 1:
            raise ValueError(
                f"{path}: calibration table version {version} is not supported "
                "(this Scouter reads version 1); re-run `scouter wb calibrate`"
            )
        for point in data["samples"]:
            _validate_object_sample(path, point)
        samples = [
            {"kelvin": p["k"], "coeffs": [p["coeff_r"], p["coeff_g"], p["coeff_b"]]}
            for p in data["samples"]
        ]
        table = {field: data.get(field) for field in _TABLE_METADATA_FIELDS}
        table["synthetic"] = bool(data.get("synthetic", False))
        note = data.get("note")
    elif isinstance(data, dict) and isinstance(data.get("points"), list) and data["points"]:
        # Task 9's own wrapper shape (`phase-2`, fix round 1): self-describing
        # (carries its own `synthetic`), so no "assumed synthetic" warning --
        # an object table uses its own `synthetic` field, unlike the bare array.
        for point in data["points"]:
            _validate_legacy_point(path, point)
        samples = [{"kelvin": p["kelvin"], "coeffs": list(p["coeffs"])} for p in data["points"]]
        table = dict.fromkeys(_TABLE_METADATA_FIELDS)
        table["version"] = 1
        table["camera"] = camera
        table["synthetic"] = bool(data.get("synthetic", False))
        note = data.get("note")
    else:
        raise ValueError(
            f"{path} must hold a JSON object with a non-empty 'samples' or 'points' array "
            f"(design 11.4 shape, or task 9's {{synthetic, note, points}} wrapper), or a "
            f"legacy bare array of calibration points, got {data!r}"
        )

    samples = sorted(samples, key=lambda p: p["kelvin"])
    _check_monotonic(path, samples)
    table["samples"] = samples
    table["note"] = note
    table["warnings"] = warnings
    return table


def _ratio(coeffs: list[float]) -> float:
    """r/b: monotone in Kelvin at fixed tint (`calibrate`'s own check enforces
    this when a table is written). A zero blue coefficient cannot form a
    ratio at all (merged in from task 9's fix round, `phase-2`) -- a typed
    `UsageError` rather than a `ZeroDivisionError` with no code to match on.
    """
    b = float(coeffs[2])
    if b == 0:
        raise UsageError(
            f"coefficient vector {list(coeffs)!r} has a zero blue coefficient: "
            "cannot form an r/b ratio",
            detail={"coeffs": list(coeffs)},
        )
    return float(coeffs[0]) / b


def _quad_interp(xs: list[float], ys: list[float], x: float) -> float:
    """Lagrange quadratic through exactly 3 points `(xs[i], ys[i])`, evaluated
    at `x`. `xs` must be 3 distinct values (guaranteed here by strictly
    monotonic Kelvin -> strictly monotonic mired)."""
    x0, x1, x2 = xs
    y0, y1, y2 = ys
    l0 = (x - x1) * (x - x2) / ((x0 - x1) * (x0 - x2))
    l1 = (x - x0) * (x - x2) / ((x1 - x0) * (x1 - x2))
    l2 = (x - x0) * (x - x1) / ((x2 - x0) * (x2 - x1))
    return y0 * l0 + y1 * l1 + y2 * l2


def _off_curve(samples: list[dict], k: float, coeffs: list[float] | None) -> float | None:
    """Relative distance between the observed g-normalised (r, b) and the
    table's (r, b) at Kelvin `k`. None when coeffs are absent, or fewer than
    2 samples (a single-point table has no curve to be off of).

    Fix round 1 (I2): a straight chord between the two bracketing samples
    bends away from the table's own (smoothly curved, roughly quadratic in
    mireds) shape enough to false-refuse a correct reading near the low end
    of a 1000 K-step table. With >= 3 samples, interpolates a quadratic in
    mireds (1e6 / K) through the 3 samples nearest `k`; with exactly 2
    samples, falls back to the old linear-in-Kelvin chord (nothing else to
    fit a quadratic through).
    """
    if not coeffs or len(coeffs) < 3 or not coeffs[1]:
        return None
    if len(samples) < 2:
        return None
    r_obs, b_obs = coeffs[0] / coeffs[1], coeffs[2] / coeffs[1]

    def norm(p: dict) -> tuple[float, float]:
        c = p["coeffs"]
        return c[0] / c[1], c[2] / c[1]

    if len(samples) == 2:
        lo, hi = samples[0], samples[1]
        span = hi["kelvin"] - lo["kelvin"]
        f = 0.0 if span == 0 else (k - lo["kelvin"]) / span
        (rl, bl), (rh, bh) = norm(lo), norm(hi)
        r_t, b_t = rl + f * (rh - rl), bl + f * (bh - bl)
    else:
        m = 1e6 / k
        nearest = sorted(samples, key=lambda p: abs(1e6 / p["kelvin"] - m))[:3]
        nearest = sorted(nearest, key=lambda p: p["kelvin"])
        ms = [1e6 / p["kelvin"] for p in nearest]
        rs, bs = zip(*(norm(p) for p in nearest))
        r_t, b_t = _quad_interp(ms, list(rs), m), _quad_interp(ms, list(bs), m)
    return ((r_obs - r_t) ** 2 + (b_obs - b_t) ** 2) ** 0.5 / (r_t ** 2 + b_t ** 2) ** 0.5


def _invert(samples: list[dict], ratio: float, coeffs: list[float] | None = None,
            *, strict: bool = False) -> tuple[float | None, dict]:
    """`strict=True` (the `wb.kelvin` verb's readback) answers None with a
    reason when the coefficients are off the calibrated curve; internal
    callers (relative moves, undo, style) keep the r/b answer and get an
    `approximate` warning, so a warmer/cooler move still works at any tint."""
    k, info = _invert_ratio(samples, ratio)
    if k is None:
        return k, info
    dist = _off_curve(samples, k, coeffs)
    info["off_curve"] = dist
    if dist is not None and dist > _OFF_CURVE_TOLERANCE and not strict:
        info["approximate"] = True
        return k, info
    if dist is not None and dist > _OFF_CURVE_TOLERANCE:
        return None, {**info, "interpolated": False, "exact": False, "reason": (
            f"these coefficients are {100 * dist:.0f}% off the calibrated curve: the frame's "
            "tint is not the table's calibrated tint, and r/b alone cannot give Kelvin at "
            "another tint. Set tint back to 1.0, or re-run `scouter wb calibrate` "
            "for this tint; the slider value itself was applied.")}
    return k, info


def expected_ratio(camera: str, kelvin: float) -> float | None:
    """The r/b ratio `camera`'s measured table predicts at `kelvin` -- the
    forward direction of `_invert_ratio`, linear between the bracketing
    samples. `None` with no table, or outside the measured range (never
    extrapolated). Used to verify a Kelvin write in the coefficient domain,
    where darktable's own write is exact and the table's Kelvin snap
    (`_EXACT_TOLERANCE`) does not apply (row 433)."""
    try:
        table = load_table(camera)
    except ValueError:
        return None
    if not table:
        return None
    samples = table["samples"]
    for lo, hi in zip(samples, samples[1:]):
        k_lo, k_hi = float(lo["kelvin"]), float(hi["kelvin"])
        if k_lo <= kelvin <= k_hi:
            r_lo, r_hi = _ratio(lo["coeffs"]), _ratio(hi["coeffs"])
            frac = (kelvin - k_lo) / (k_hi - k_lo) if k_hi != k_lo else 0.0
            return r_lo + frac * (r_hi - r_lo)
    if len(samples) == 1 and float(samples[0]["kelvin"]) == kelvin:
        return _ratio(samples[0]["coeffs"])
    return None


def _invert_ratio(samples: list[dict], ratio: float) -> tuple[float | None, dict]:
    """Bisect the sorted `samples` on `r = coeff_r / coeff_b`, then interpolate
    linearly between the two bracketing entries.

    Returns `(kelvin, info)`: `info` carries `interpolated`, `bracket`,
    `residual_ratio`, `exact`, `reason` -- everything `kelvin()` folds into
    its result besides `kelvin`/`camera`/`table` themselves. Outside the
    sampled range the answer is `(None, info-with-a-reason)`: extrapolating a
    blackbody curve past the measurements would be exactly the invented value
    this project refuses to produce.
    """
    ratios = [_ratio(p["coeffs"]) for p in samples]

    for point, r in zip(samples, ratios):
        if r != 0 and abs(ratio - r) / abs(r) <= _EXACT_TOLERANCE:
            # Should-fix (task 11 fix round 1): the MEASURED residual, not a
            # hard-coded 0.0 -- `ratio` need only be within `_EXACT_TOLERANCE`
            # of the sample, not bit-identical to it.
            return point["kelvin"], {
                "interpolated": False,
                "bracket": None,
                "residual_ratio": ratio - r,
                "exact": True,
                "reason": None,
            }

    lo_bound, hi_bound = ratios[0], ratios[-1]
    if ratio < lo_bound or ratio > hi_bound:
        return None, {
            "interpolated": False,
            "bracket": None,
            "residual_ratio": None,
            "exact": False,
            "reason": (
                f"r/b ratio {ratio:.4f} is outside the calibrated range "
                f"[{lo_bound:.4f}, {hi_bound:.4f}] "
                f"({samples[0]['kelvin']}-{samples[-1]['kelvin']} K); "
                "extrapolating past the measurements would be an invented value"
            ),
        }

    for i in range(len(samples) - 1):
        lo_r, hi_r = ratios[i], ratios[i + 1]
        if lo_r <= ratio <= hi_r:
            lo, hi = samples[i], samples[i + 1]
            frac = (ratio - lo_r) / (hi_r - lo_r) if hi_r != lo_r else 0.0
            k = lo["kelvin"] + frac * (hi["kelvin"] - lo["kelvin"])
            return k, {
                "interpolated": True,
                "bracket": [lo["kelvin"], hi["kelvin"]],
                "residual_ratio": ratio - lo_r,
                "exact": False,
                "reason": None,
            }

    # Unreachable in practice: `_check_monotonic` already confirmed the
    # table's r/b ratio is strictly increasing before this point ever ran
    # (`load_table` calls it on every load), and the range check above
    # guarantees a bracket exists somewhere in a genuinely monotone, sorted
    # sequence. Kept as a typed refusal rather than a bare assertion anyway
    # (merged in from task 9's fix round) -- belt and braces naming the
    # table, not a crash with no code a caller could match on.
    raise AssertionError(f"{ratio!r} in range but no bracket found -- table not sorted?")


def _reason_with_table_context(camera: str, reason: str) -> str:
    """E12 A7 case (row 407): when `coeffs` could not even be read (no
    committed `temperature` module in the XMP), the reason returned used to
    be ONLY that fact -- so a camera that also has no calibration table
    (the A7's actual state in the E12 run) never got told so, unlike the
    NEX-5N's refusal, which names its missing table outright. Appends the
    same missing-table fact, with the same `wb calibrate` pointer, whenever
    it is also true.
    """
    try:
        table = load_table(camera)
    except ValueError:
        table = None
    if not table:
        return (
            f"{reason}; {camera} also has no wb calibration table -- "
            "run scouter wb calibrate"
        )
    return reason


def _no_table_result(camera: str, reason: str | None = None) -> dict:
    return {
        "kelvin": None,
        "interpolated": False,
        "bracket": None,
        "residual_ratio": None,
        "exact": False,
        "camera": camera,
        "table": None,
        "reason": reason or f"no wb calibration table for {camera}; run scouter wb calibrate",
        "warnings": [],
    }


def _coeffs_and_camera_from_image(
        image: str | int) -> tuple[list[float] | None, str, str | None, str]:
    """Resolve `image`'s active `temperature` coefficients and its camera slug.

    `coeffs` is `None` (with a `reason`) when the image has no sidecar, no
    active `temperature` entry, or the entry did not decode -- never guessed.
    Camera comes from the raw's own EXIF (`scouter.core.exif`, the same path
    `library`/`style` use), not from the job manifest, so this works for an
    image outside any job too.
    """
    from scouter.config import load as load_config
    from scouter.core import darkroom, exif

    resolved = dtbridge.resolve_image(image)
    cfg = load_config()
    tags = exif.read_tags(resolved.path)
    camera = exif.camera_slug(tags, cfg)

    path = resolved.path
    parsed = darkroom._read_sidecar_parsed(path)
    if parsed is None:
        return None, camera, f"no sidecar for {path}: darktable has not committed it yet", path
    params = xmp.latest_params(parsed, "temperature")
    if params is None:
        return None, camera, "no active temperature entry in this image's XMP", path
    try:
        coeffs = [float(params[name]) for name in _TEMPERATURE_COEFF_NAMES]
    except (KeyError, TypeError, ValueError):
        return None, camera, "temperature's coefficients did not decode from the XMP", path
    return coeffs, camera, None, path


def _route_readback(path: str) -> tuple[dict, list[str]]:
    """Which Kelvin route is in effect on `path` (`darkroom.wb_route`), plus the
    warning a reader of `kelvin` needs on a preset-4 image: there `kelvin` is
    the white balance module's as-shot value, which warmer/cooler no longer
    move (route A) -- the illuminant temperature is what they move. Local
    sidecar reads only."""
    from scouter.core import darkroom

    route = darkroom.wb_route(path)
    if route["route"] == "white_balance":
        return route, []
    if route["route"] == "color_calibration":
        return route, [
            "white balance is in camera reference (late) mode: `kelvin` is the white balance "
            "module's as-shot value, which warmer/cooler do not move here; they move color "
            f"calibration's illuminant temperature, now {route.get('illuminant_kelvin')} K "
            "(`route.illuminant_kelvin`)"]
    return route, [
        "white balance is in camera reference (late) mode and Scouter cannot move warmth on "
        f"this image: {route.get('reason')}"]


def kelvin(
    coeffs: list[float] | None = None,
    camera: str | None = None,
    *,
    image: str | int | None = None,
    strict: bool = False,
) -> dict:
    """Invert Kelvin from `coeffs` (r, g, b, ...) against `camera`'s table, or
    from `image`'s own committed XMP (which also supplies `camera`, via EXIF).

    Exactly one of `image`, or `coeffs`+`camera`, is accepted -- positional
    `coeffs`/`camera` first (task 9's own call shape: `wb.kelvin(coeffs,
    camera)`), `image` keyword-only.

    Result shape: `{kelvin, interpolated, bracket, residual_ratio, exact,
    camera, table, warnings, reason}`, plus `route` for an `image` call
    (`_route_readback`: which Kelvin route is in effect). No table -> `kelvin: None` with a
    reason naming `scouter wb calibrate`, `table: None`. Outside the
    calibrated range -> `kelvin: None` with a reason naming the range, never
    extrapolated. `table`, when a table was found, is `{"path": str,
    "synthetic": bool}` (merged in from task 9's fix round, `phase-2`) -- a
    synthetic (placeholder or legacy) table is perfectly usable for testing
    the plumbing, but a caller reading the result can tell it apart from a
    real measured one without reaching into the file itself.
    """
    if image is not None:
        if coeffs is not None or camera is not None:
            raise UsageError(
                "wb.kelvin: pass image=..., or coeffs and camera, not both",
                detail={"image": image, "coeffs": coeffs, "camera": camera},
            )
        coeffs, camera, reason, path = _coeffs_and_camera_from_image(image)
        route, route_warnings = _route_readback(path)
        if coeffs is None:
            out = _no_table_result(camera, _reason_with_table_context(camera, reason))
            return {**out, "route": route, "warnings": out["warnings"] + route_warnings}
        result = _kelvin_from(coeffs, camera, strict=strict)
        return {**result, "route": route, "warnings": result["warnings"] + route_warnings}
    elif coeffs is None or camera is None:
        raise UsageError(
            "wb.kelvin needs image=..., or both coeffs and camera",
            detail={"coeffs": coeffs, "camera": camera},
        )

    return _kelvin_from(coeffs, camera, strict=strict)


def _kelvin_from(coeffs: list[float], camera: str, *, strict: bool) -> dict:
    try:
        table = load_table(camera)
    except ValueError as e:
        return _no_table_result(camera, str(e))
    if not table:
        return _no_table_result(camera)

    table_ref = {"path": str(_table_path(camera)), "synthetic": bool(table.get("synthetic"))}
    target = _ratio(coeffs)
    k, info = _invert(table["samples"], target, list(coeffs), strict=strict)
    return {
        "kelvin": k,
        "camera": camera,
        "table": table_ref,
        "warnings": list(table.get("warnings") or []),
        **info,
    }


@metrics.instrumented("wb.kelvin")
def kelvin_verb(image: str | int, *, job: str | None = None) -> KelvinResult:
    """The registered verb: `wb.kelvin` on an image, end to end.

    A thin wrapper over `kelvin()` (undecorated, and deliberately so --
    `outliers.list`/`style._resolve_kelvin` call `kelvin()` directly, once per
    frame, from inside their OWN top-level verb; if `kelvin()` itself were
    `@instrumented`, every frame in an `outliers.list` run would add its own
    metrics event under someone else's verb, breaking "one event per
    top-level verb"). This wrapper is what a direct CLI/MCP call goes through.
    """
    job = job or current_job()
    result = kelvin(image=image, strict=True)
    return KelvinResult(
        kelvin=result["kelvin"],
        interpolated=result["interpolated"],
        bracket=result["bracket"],
        residual_ratio=result["residual_ratio"],
        exact=result["exact"],
        camera=result["camera"],
        table=result["table"],
        reason=result["reason"],
        warnings=list(result.get("warnings") or []),
        route=result.get("route"),
    )


def _inside_job_raw(path: str) -> bool:
    """True when `path`'s parent directory is a job's `raw/` (config.raw_dir's
    own shape: `<job_path>/raw/<file>`) -- a cheap, dependency-free check that
    needs no job manifest lookup, just the path `calibrate` was already given.
    """
    return Path(path).parent.name == "raw"


def _unwrapped(fn):
    return getattr(fn, "__wrapped__", fn)


#: Matches darktable-cli's own `--version` first line, e.g. "this is
#: darktable 5.4.1". Fix round 2, IMPORTANT 2: only the X.Y.Z token is ever
#: stored -- never the whole multi-line/URL-bearing stdout blob.
_DARKTABLE_VERSION_RE = re.compile(r"darktable\s+(\d+\.\d+\.\d+)")


def _parse_darktable_version(raw: str | None) -> str:
    """The `X.Y.Z` token from darktable-cli's `--version` output's FIRST
    line, or `"unknown"` -- never `None` (the table's `darktable` field is
    always a string) and never the raw blob."""
    first_line = (raw or "").splitlines()[0] if raw else ""
    match = _DARKTABLE_VERSION_RE.search(first_line)
    return match.group(1) if match else "unknown"


def _resolve_darktable_version() -> str:
    """Best-effort darktable version for the table's provenance.

    Fix round 2, IMPORTANT 1: a non-host unit test must NEVER shell out to
    a real `darktable-cli` -- `scout.cli_binary()` falls back to the real
    Mac install path, which exists on a dev host regardless of whether a
    `FakeBridge` is installed. So this refuses to even try
    `CLIWrapper.get_version()` (which calls `subprocess.run`) unless the
    CURRENTLY installed bridge is the real one (`dtbridge.set_bridge` is
    documented as a test-only seam production code never calls) --
    `dtbridge.get_bridge()` under a `FakeBridge` returns `"unknown"`
    immediately, no subprocess anywhere near it. NOT a `calibrate(...,
    darktable_version_resolver=...)` parameter (fix round 2): the registry
    pins every registered verb's Python signature against its `ArgSpec`s
    1:1 (`tests/test_layer_parity.py::test_argument_names_agree_between_
    the_registry_and_the_core_functions`), and a resolver callable has no
    CLI/MCP-representable type -- `mock.patch("scouter.core.wb.
    _resolve_darktable_version", ...)` is the test seam instead, same
    discipline `_commit`/`darkroom._commit` already use elsewhere in this
    codebase.
    """
    from darktable_mcp.bridge.client import Bridge as _RealBridge

    if not isinstance(dtbridge.get_bridge(), _RealBridge):
        return "unknown"
    try:
        from scouter.core import scout

        raw = scout.CLIWrapper(darktable_cli_path=scout.cli_binary()).get_version()
    except Exception:  # doctor-shaped info only, never a reason to refuse the write
        return "unknown"
    return _parse_darktable_version(raw)


#: Folder mode skips non-raw files (a JPEG has no raw white-balance multipliers).
_NOT_RAW = {"JPG", "JPEG", "PNG", "TIF", "TIFF", "XMP"}


def _body_of(path: Path, cfg) -> str:
    """The camera slug for one raw, from its own EXIF (same rule as import)."""
    from scouter.core import exif

    return exif.camera_slug(exif.read_tags(str(path)), cfg)


def _folder_plan(folder: Path, camera: str | None, scratch: str | None) -> dict:
    """Pick one body's frames and the frame to sweep. Local reads only."""
    from scouter.config import load as load_config

    cfg = load_config()
    wanted = {"." + e.lower() for e in cfg.import_.extensions if e.upper() not in _NOT_RAW}
    raws = sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in wanted and not p.name.startswith("."))
    bodies: dict[str, list[Path]] = {}
    unreadable: list[str] = []
    for raw in raws:
        try:
            bodies.setdefault(_body_of(raw, cfg), []).append(raw)
        except (ScouterError, OSError, ValueError):
            unreadable.append(raw.name)
    counts = ", ".join(f"{b}: {len(v)}" for b, v in sorted(bodies.items()))
    if not bodies:
        raise UsageError(
            f"no raw with a readable camera model in {folder}",
            fix="point wb calibrate at a folder of raws (ORF/ARW/DNG) or at one raw file",
            detail={"folder": str(folder), "unreadable": unreadable},
        )
    if camera is None:
        if len(bodies) > 1:
            raise UsageError(
                f"{folder} holds {len(bodies)} camera bodies ({counts}); calibrate one at a "
                "time: pass --camera <body>",
                detail={"folder": str(folder), "bodies": sorted(bodies)},
            )
        camera = next(iter(bodies))
    if camera not in bodies:
        raise UsageError(
            f"no {camera!r} frames in {folder}; bodies present: {counts}",
            detail={"folder": str(folder), "bodies": sorted(bodies), "camera": camera},
        )
    frames = bodies[camera]
    chosen = frames[0]
    root = Path(scratch).expanduser() if scratch else paths.cache_root() / "wb-scratch"
    return {
        "camera": camera,
        "frames": [{"path": str(p), "camera": camera} for p in frames],
        "chosen": chosen,
        "copy": root / camera / chosen.name,
        "unreadable": unreadable,
    }


@metrics.instrumented("wb.calibrate")
def calibrate(
    image: str | int,
    k_from: int = 2000,
    k_to: int = 12000,
    step: int = 500,
    *,
    camera: str | None = None,
    scratch: str | None = None,
    allow_in_job: bool = False,
    confirm: bool = False,
    dry_run: bool = False,
    job: str | None = None,
) -> CalibrateResult:
    """Sweep the Kelvin slider from `k_from` to `k_to` (inclusive) by `step`,
    reading the landed coefficients back from the XMP after every commit, and
    write the measured `~/.config/scouter/wb/<camera>.json` table.

    Refuses on a job's own `raw/` frame unless `allow_in_job=True`: this adds
    roughly one history item per Kelvin point (~21 for the brief's own sweep)
    to whatever image is passed, which is fine for a scratch/calibration copy
    and not something to do to a client frame by accident -- this check runs
    even without `confirm` (it is not the confirm gate, `allow_in_job` is its
    own escape hatch).

    Gated (design's own table, "wb.calibrate: gated yes"): `confirm` is
    required for any real run, same shape as every other gated verb here
    (`dry_run or not confirm` -> a `dry_run` result, never a raised error) --
    this also means an existing table is never overwritten without it, since
    nothing runs without it. The EXIF/camera lookup that decides the table
    path happens AFTER this gate, not before: a caller previewing without
    `confirm` must never need a readable raw on disk.

    Refuses (raising `UsageError`, naming the breaking pair) rather than
    write a non-monotone table -- `wb.kelvin`'s bisection assumes r/b is
    strictly increasing in K, and a table that violates that would silently
    produce wrong answers.

    The table's `darktable` provenance field comes from
    `_resolve_darktable_version` (fix round 2), which never shells out to a
    real `darktable-cli` unless the currently installed bridge is the real
    one -- a test with a `FakeBridge` installed gets `"unknown"` with no
    subprocess call at all.
    """
    job = job or current_job()
    if step <= 0:
        raise UsageError(
            f"step must be a positive number of Kelvin, got {step!r}", detail={"step": step}
        )
    if k_to < k_from:
        raise UsageError(
            f"k_to ({k_to}) must be >= k_from ({k_from})", detail={"k_from": k_from, "k_to": k_to}
        )
    ks = list(range(int(k_from), int(k_to) + 1, int(step)))
    if not ks:
        raise UsageError(
            f"no Kelvin points between {k_from} and {k_to} step {step}",
            detail={"k_from": k_from, "k_to": k_to, "step": step},
        )
    estimated_s = round(len(ks) * _SECONDS_PER_POINT, 1)

    folder = Path(str(image)).expanduser()
    if not str(image).isdigit() and folder.is_dir():
        return _calibrate_folder(folder, ks, k_from, k_to, step, camera=camera, scratch=scratch,
                                 allow_in_job=allow_in_job, confirm=confirm, dry_run=dry_run,
                                 job=job, estimated_s=estimated_s)
    if camera is not None or scratch is not None:
        raise UsageError(
            "camera and scratch apply to folder mode only (image is a folder of raws)",
            detail={"image": str(image), "camera": camera, "scratch": scratch},
        )

    resolved = dtbridge.resolve_image(image)
    if _inside_job_raw(resolved.path) and not allow_in_job:
        raise UsageError(
            f"{resolved.path} is inside a job's raw/: wb.calibrate would add ~{len(ks)} history "
            "items to a client frame. Pass allow_in_job=True to calibrate on it anyway, or point "
            "this at a scratch copy instead",
            detail={"path": resolved.path, "points": len(ks)},
        )

    plan = [
        {"action": "darkroom.set", "module": "temperature", "param": "temperature", "value": k}
        for k in ks
    ]
    if dry_run or not confirm:
        return CalibrateResult(
            outcome="dry_run",
            image_id=resolved.image_id,
            path=resolved.path,
            ks=ks,
            estimated_s=estimated_s,
            plan=plan,
            reason=(
                f"dry run: would set {len(ks)} Kelvin points ({ks[0]}-{ks[-1]}, step {step}); "
                f"estimated {estimated_s}s; nothing was written to darktable"
                if dry_run
                else (
                    f"would sweep {len(ks)} Kelvin points ({ks[0]}-{ks[-1]}, step {step}, "
                    f"~{estimated_s}s) and write (or overwrite) a wb calibration table; "
                    "re-run with --yes / confirm=true"
                )
            ),
        )

    from scouter.config import load as load_config
    from scouter.core import darkroom, exif

    cfg = load_config()
    tags = exif.read_tags(resolved.path)
    camera = exif.camera_slug(tags, cfg)

    table_path = _table_path(camera)
    # `confirm` is guaranteed True past the gate above, so this is never a
    # silent overwrite -- just a fact worth reporting in the result.
    overwritten = table_path.exists()

    open_fn = _unwrapped(darkroom.open_image)
    open_fn(image, job=job)

    set_fn = _unwrapped(darkroom.set_param)
    points: list[dict] = []
    for k in ks:
        # Exempt from the late-WB guard (`darkroom.refuse_wb_late`): this sweep
        # measures the Kelvin->coefficient mapping; the late D65 correction
        # does not enter that measurement.
        exempt = darkroom.WB_LATE_EXEMPT.set(True)
        try:
            set_fn("temperature", "temperature", float(k), job=job)
        finally:
            darkroom.WB_LATE_EXEMPT.reset(exempt)
        parsed = darkroom._read_sidecar_parsed(resolved.path)
        params = xmp.latest_params(parsed, "temperature") if parsed is not None else None
        if params is None:
            raise ScouterError(
                f"wb.calibrate: temperature did not decode from the XMP at {k} K",
                fix="check darkroom.status/history for what actually landed and re-run",
                detail={"kelvin": k, "path": resolved.path},
            )
        coeff_r, coeff_g, coeff_b = (
            params.get("coeff_r"),
            params.get("coeff_g"),
            params.get("coeff_b"),
        )
        # Should-fix (fix round 1): a typed refusal for a sample the sweep
        # cannot use -- None (never decoded, OR inf/nan, since
        # `xmp.latest_params` already cleans those to None) or a zero
        # `coeff_b` (which would divide-by-zero the r/b ratio below). Never
        # let a garbage sample silently ride into the table or crash later.
        if coeff_r is None or coeff_g is None or coeff_b is None or coeff_b == 0:
            raise WbCalibrationUnreadable(
                f"wb.calibrate: temperature's coefficients at {k} K are not usable for the "
                f"r/b ratio (coeff_r={coeff_r!r}, coeff_g={coeff_g!r}, coeff_b={coeff_b!r})",
                detail={
                    "kelvin": k,
                    "path": resolved.path,
                    "coeff_r": coeff_r,
                    "coeff_g": coeff_g,
                    "coeff_b": coeff_b,
                },
            )
        points.append({"k": k, "coeff_r": coeff_r, "coeff_g": coeff_g, "coeff_b": coeff_b})

    ratios = [p["coeff_r"] / p["coeff_b"] for p in points]
    for i in range(len(ratios) - 1):
        if ratios[i + 1] <= ratios[i]:
            raise UsageError(
                f"calibration is not monotone between {points[i]['k']} K (r/b={ratios[i]:.4f}) and "
                f"{points[i + 1]['k']} K (r/b={ratios[i + 1]:.4f}); refusing to write a table that "
                "cannot be inverted",
                detail={
                    "camera": camera,
                    "breaking_pair": [points[i]["k"], points[i + 1]["k"]],
                    "ratios": [ratios[i], ratios[i + 1]],
                },
            )

    # Fix round 1, blocker 1: the object shape design 11.4 documents, not
    # task 9's bare array -- provenance (camera model, when, which darktable,
    # which source image) travels WITH the numbers from here on.
    measured_at = datetime.now(timezone.utc).isoformat()
    darktable_version = _resolve_darktable_version()

    table_doc = {
        "version": 1,
        "camera": camera,
        "camera_model": tags.model,
        "measured_at": measured_at,
        "darktable": darktable_version,
        # Never touched by this sweep (only "temperature" is set) -- darktable's
        # own default, recorded rather than measured. See task-11-report.md.
        "tint": 1.0,
        "source_image": Path(resolved.path).name,
        "synthetic": False,
        "samples": [
            {"k": p["k"], "coeff_r": p["coeff_r"], "coeff_g": p["coeff_g"], "coeff_b": p["coeff_b"]}
            for p in points
        ],
    }
    paths.ensure(table_path)
    fd, tmp_name = tempfile.mkstemp(dir=str(table_path.parent), prefix=".wb-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(table_doc, fh, indent=2, sort_keys=True)
        os.replace(tmp_name, table_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    return CalibrateResult(
        image_id=resolved.image_id,
        path=resolved.path,
        camera=camera,
        table_path=str(table_path),
        ks=ks,
        estimated_s=estimated_s,
        points=points,
        written=True,
        overwritten=overwritten,
    )


def _calibrate_folder(folder: Path, ks: list[int], k_from: int, k_to: int, step: int, *,
                      camera: str | None, scratch: str | None, allow_in_job: bool,
                      confirm: bool, dry_run: bool, job: str,
                      estimated_s: float) -> CalibrateResult:
    """Folder mode: one body, one scratch copy, then the ordinary sweep."""
    import shutil

    plan_info = _folder_plan(folder, camera, scratch)
    cam = plan_info["camera"]
    chosen: Path = plan_info["chosen"]
    copy: Path = plan_info["copy"]
    table_path = _table_path(cam)
    extra = {"frames": plan_info["frames"], "source_image": str(chosen),
             "scratch_copy": str(copy)}
    warnings = ([f"camera model unreadable, skipped: {', '.join(plan_info['unreadable'])}"]
                if plan_info["unreadable"] else [])
    if dry_run or not confirm:
        plan = [{"action": "copy", "from": str(chosen), "to": str(copy)}] + [
            {"action": "darkroom.set", "module": "temperature", "param": "temperature",
             "value": k} for k in ks]
        return CalibrateResult(
            outcome="dry_run", camera=cam, table_path=str(table_path), ks=ks,
            estimated_s=estimated_s, plan=plan, warnings=warnings, **extra,
            reason=(
                f"{'dry run' if dry_run else 'confirm required'}: {cam} has "
                f"{len(plan_info['frames'])} frame(s) in {folder}; would copy {chosen.name} to "
                f"{copy} (the original is never touched), sweep {len(ks)} Kelvin points "
                f"({ks[0]}-{ks[-1]}, step {step}, ~{estimated_s}s) on the copy, and write "
                f"{table_path}{' (overwriting it)' if table_path.exists() else ''}. darktable "
                "must be able to show the copy in the darkroom: if it cannot switch to a frame "
                "imported this session, launch darktable on the copy and re-run"
                + ("" if dry_run else "; re-run with --yes / confirm=true")
            ),
        )
    copy.parent.mkdir(parents=True, exist_ok=True)
    if not copy.exists() or copy.stat().st_size != chosen.stat().st_size:
        shutil.copy2(chosen, copy)
    inner = _unwrapped(calibrate)
    result = inner(str(copy), k_from, k_to, step, allow_in_job=allow_in_job, confirm=True,
                   job=job)
    result.frames = extra["frames"]
    result.source_image = extra["source_image"]
    result.scratch_copy = extra["scratch_copy"]
    result.warnings = warnings + list(result.warnings)
    if result.camera and result.camera != cam:
        result.warnings.append(
            f"the copy's EXIF resolved to {result.camera!r}, not {cam!r}; the table was "
            f"written as {result.camera}.json")
    return result


IMAGE_ARG = ArgSpec(
    "image",
    "image",
    required=True,
    positional=True,
    help="absolute path to the raw, or an image id",
)

for verb in (
    Verb(
        id="wb.kelvin",
        group="wb",
        fn=kelvin_verb,
        args=(IMAGE_ARG,),
        writes=False,
        gated=False,
        summary="Invert an image's temperature coefficients to Kelvin, from its measured table.",
    ),
    Verb(
        id="wb.calibrate",
        group="wb",
        fn=calibrate,
        args=(
            ArgSpec("image", "image", required=True, positional=True,
                    help="a raw file, an image id, or a folder of raws (one body at a time)"),
            ArgSpec("k_from", "int", default=2000, positional=True, help="first Kelvin point"),
            ArgSpec("k_to", "int", default=12000, positional=True, help="last Kelvin point"),
            ArgSpec("step", "int", default=500, help="Kelvin step between points"),
            ArgSpec("camera", "str", default=None,
                    help="folder mode: which body to calibrate when the folder is mixed"),
            ArgSpec("scratch", "str", default=None,
                    help="folder mode: where the scratch copy goes (default <cache>/wb-scratch)"),
            ArgSpec(
                "allow_in_job",
                "bool",
                default=False,
                help="required: image is inside a job's raw/",
            ),
            ArgSpec("confirm", "bool", default=False, help="required: overwrite an existing table"),
            ArgSpec("dry_run", "bool", default=False, help="plan only, send nothing"),
        ),
        writes=True,
        gated=True,
        summary="Measure a per-camera Kelvin<->coefficient calibration table.",
    ),
):
    register(verb)
