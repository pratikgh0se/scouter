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

`load_table` also still reads task 9's original bare-array shape
(`[{"kelvin": int, "coeffs": [r, g, b]}, ...]`, no provenance at all) --
normalised to the same internal `samples` list, `synthetic: true` assumed,
and a warning surfaced through `wb.kelvin`'s own `warnings`.

Inversion bisects the sorted samples on the r/b coefficient ratio (monotone in
Kelvin at fixed tint -- `calibrate` itself refuses to write a table that
isn't) and interpolates linearly between the two bracketing points; within 1%
of a sample counts as `exact`. Outside the sampled range the answer is `None`
with a reason -- extrapolating a blackbody curve past the measurements would
be exactly the invented value this project refuses to produce.
"""

from __future__ import annotations

import json
import os
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


#: How close `target` has to be to a sample's own ratio to call it `exact`
#: rather than interpolated.
_EXACT_TOLERANCE = 0.01

#: `calibrate`'s own measured cost (task 11 host runs): about how long one
#: Kelvin point (a `darkroom.set` + commit + XMP readback) takes.
_SECONDS_PER_POINT = 1.8

_TEMPERATURE_COEFF_NAMES = ("coeff_r", "coeff_g", "coeff_b")


def _table_path(camera: str) -> Path:
    return paths.wb_dir() / f"{camera}.json"


#: Every field `load_table` carries through from the object shape (design
#: 11.4), besides `samples` itself (normalised separately) and the derived
#: `synthetic`/`warnings` this module adds.
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


def load_table(camera: str) -> dict | None:
    """`camera`'s full calibration table, or `None` when no table file exists.

    Reads two shapes (fix round 1, blocker 1): the object shape design 11.4
    documents (`{version, camera, camera_model, measured_at, darktable,
    tint, source_image, synthetic, samples: [{k, coeff_r, coeff_g,
    coeff_b}, ...]}`, written by this module's own `calibrate`), and task 9's
    original bare JSON array (`[{"kelvin": int, "coeffs": [r, g, b]}, ...]`,
    no provenance at all) -- the latter is normalised into the same shape
    with `synthetic: true` assumed and a warning in the returned `warnings`
    list, never silently treated as equally trustworthy.

    The returned dict's `samples` is always `[{"kelvin": int, "coeffs": [r,
    g, b]}, ...]`, sorted by Kelvin -- `_invert`'s own internal shape,
    regardless of which external shape the file used. Raises `ValueError` if
    the file exists but matches neither shape -- a malformed table must
    never be silently ignored.
    """
    path = _table_path(camera)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not valid JSON: {e}") from e

    warnings: list[str] = []
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
    elif isinstance(data, dict):
        if not isinstance(data.get("samples"), list) or not data["samples"]:
            raise ValueError(
                f"{path}: object shape needs a non-empty 'samples' array, got {data!r}"
            )
        for point in data["samples"]:
            _validate_object_sample(path, point)
        samples = [
            {
                "kelvin": p["k"],
                "coeffs": [p["coeff_r"], p["coeff_g"], p["coeff_b"]],
            }
            for p in data["samples"]
        ]
        table = {field: data.get(field) for field in _TABLE_METADATA_FIELDS}
        table["synthetic"] = bool(data.get("synthetic", False))
    else:
        raise ValueError(
            f"{path} must hold a JSON object (design 11.4 shape) or a legacy array of "
            f"calibration points, got {type(data).__name__}"
        )

    table["samples"] = sorted(samples, key=lambda p: p["kelvin"])
    table["warnings"] = warnings
    return table


def _ratio(coeffs: list[float]) -> float:
    """r/b: monotone in Kelvin at fixed tint (`calibrate`'s own check enforces
    this when a table is written)."""
    return float(coeffs[0]) / float(coeffs[2])


def _invert(samples: list[dict], ratio: float) -> tuple[float | None, dict]:
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

    # Unreachable: the range check above already guarantees a bracket exists
    # between two consecutive samples somewhere in the sorted list.
    raise AssertionError(f"{ratio!r} in range but no bracket found -- table not sorted?")


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


def _coeffs_and_camera_from_image(image: str | int) -> tuple[list[float] | None, str, str | None]:
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

    parsed = darkroom._read_sidecar_parsed(resolved.path)
    if parsed is None:
        return None, camera, f"no sidecar for {resolved.path}: darktable has not committed it yet"
    params = xmp.latest_params(parsed, "temperature")
    if params is None:
        return None, camera, "no active temperature entry in this image's XMP"
    try:
        coeffs = [float(params[name]) for name in _TEMPERATURE_COEFF_NAMES]
    except (KeyError, TypeError, ValueError):
        return None, camera, "temperature's coefficients did not decode from the XMP"
    return coeffs, camera, None


def kelvin(
    coeffs: list[float] | None = None,
    camera: str | None = None,
    *,
    image: str | int | None = None,
) -> dict:
    """Invert Kelvin from `coeffs` (r, g, b, ...) against `camera`'s table, or
    from `image`'s own committed XMP (which also supplies `camera`, via EXIF).

    Exactly one of `image`, or `coeffs`+`camera`, is accepted -- positional
    `coeffs`/`camera` first (task 9's own call shape: `wb.kelvin(coeffs,
    camera)`), `image` keyword-only.

    Result shape: `{kelvin, interpolated, bracket, residual_ratio, exact,
    camera, table, reason}`. No table -> `kelvin: None` with a reason naming
    `scouter wb calibrate`. Outside the calibrated range -> `kelvin: None`
    with a reason naming the range, never extrapolated.
    """
    if image is not None:
        if coeffs is not None or camera is not None:
            raise UsageError(
                "wb.kelvin: pass image=..., or coeffs and camera, not both",
                detail={"image": image, "coeffs": coeffs, "camera": camera},
            )
        coeffs, camera, reason = _coeffs_and_camera_from_image(image)
        if coeffs is None:
            return _no_table_result(camera, reason)
    elif coeffs is None or camera is None:
        raise UsageError(
            "wb.kelvin needs image=..., or both coeffs and camera",
            detail={"coeffs": coeffs, "camera": camera},
        )

    try:
        table = load_table(camera)
    except ValueError as e:
        return _no_table_result(camera, str(e))
    if not table:
        return _no_table_result(camera)

    target = _ratio(coeffs)
    k, info = _invert(table["samples"], target)
    return {
        "kelvin": k,
        "camera": camera,
        "table": str(_table_path(camera)),
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
    result = kelvin(image=image)
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
    )


def _inside_job_raw(path: str) -> bool:
    """True when `path`'s parent directory is a job's `raw/` (config.raw_dir's
    own shape: `<job_path>/raw/<file>`) -- a cheap, dependency-free check that
    needs no job manifest lookup, just the path `calibrate` was already given.
    """
    return Path(path).parent.name == "raw"


def _unwrapped(fn):
    return getattr(fn, "__wrapped__", fn)


@metrics.instrumented("wb.calibrate")
def calibrate(
    image: str | int,
    k_from: int = 2000,
    k_to: int = 12000,
    step: int = 500,
    *,
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
        set_fn("temperature", "temperature", float(k), job=job)
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
    try:
        from scouter.core import scout

        darktable_version = scout.CLIWrapper(darktable_cli_path=scout.cli_binary()).get_version()
    except Exception:  # doctor-shaped info only, never a reason to refuse the write
        darktable_version = None

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
            IMAGE_ARG,
            ArgSpec("k_from", "int", default=2000, positional=True, help="first Kelvin point"),
            ArgSpec("k_to", "int", default=12000, positional=True, help="last Kelvin point"),
            ArgSpec("step", "int", default=500, help="Kelvin step between points"),
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
