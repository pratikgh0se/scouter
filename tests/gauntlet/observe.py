"""What a case observes (spec 2.4): decoded XMP params (M1) and rendered stats
(M2-M8). The snapshot reuses the benchmark's snapshot_xmp; every op with a
decoder is kept (SNAPSHOT_OPS extended to all of them)."""

from __future__ import annotations

import re
from pathlib import Path

from gauntlet import metrics
from gauntlet.raster import parse_ppm

_UNNAMED = re.compile(r"^f\d+$")
PREVIEW_PX = 1024


def units() -> dict[str, str]:
    from scouter.core import vocab

    out = {f"{s.module}.{s.param}": s.unit for s in vocab.PARAMS}
    out.update({k: "°" for k, v in out.items() if v == "°"})
    return {k: ("ratio" if v in ("—",) else v) for k, v in out.items()}


#: The stored white-balance keys temperature.temperature (Kelvin, via the body's
#: WB table) and temperature.tint are decoded from (bug 4 of the night-1
#: evaluator fixes): a move reported on either owns all of them.
WB_STORED = ("temperature.coeff_r", "temperature.coeff_g", "temperature.coeff_b",
             "temperature.coeff_g2", "temperature.preset")


def defaults() -> dict[str, float]:
    """{key: darktable module default} from vocab (`default`, else `neutral`);
    rows declaring neither are left out (the evaluator then never treats them
    as at-default)."""
    from scouter.core import vocab

    out = {}
    for s in vocab.PARAMS:
        v = s.default if s.default is not None else s.neutral
        if v is not None:
            out[f"{s.module}.{s.param}"] = v
    return out


def param_meta() -> dict:
    """Product param knowledge the evaluator reads off each snapshot
    (gauntlet/evaluate.py docstring): defaults, derived keys, null equivalents.
    `coeff_g2` is inf/unused on these bodies: it decodes 0.0 on a fresh
    history and None (inf) after a WB write, which is not a change."""
    return {"defaults": defaults(),
            "derived": {"temperature.temperature": [*WB_STORED, "temperature.tint"],
                        "temperature.tint": [*WB_STORED, "temperature.temperature"]},
            "null_equiv": {"temperature.coeff_g2": 0.0}}


def flatten(snap: dict, kelvin=None) -> dict:
    """{fingerprint, history_end, params: {"op.param": value}, units}. Unnamed
    float slots (f0..fN) are dropped; `op.__enabled` is 1/0; the temperature
    coefficients add `temperature.temperature` through `kelvin(coeffs)` when a
    WB table can invert them."""
    params: dict[str, float] = {}
    for op, mod in (snap.get("modules") or {}).items():
        params[f"{op}.__enabled"] = 1 if mod.get("enabled") else 0
        for name, v in (mod.get("decoded") or {}).items():
            if _UNNAMED.match(name) or name.startswith("_") or not isinstance(v, (int, float)):
                continue
            params[f"{op}.{name}"] = v
    if kelvin is not None and "temperature.coeff_r" in params:
        k = kelvin([params["temperature.coeff_r"], params["temperature.coeff_g"],
                    params["temperature.coeff_b"]])
        if k is not None:
            params["temperature.temperature"] = k
    u = units()
    u.update({k: "bool" for k in params if k.endswith(".__enabled")})
    return {"fingerprint": snap.get("fingerprint", ""), "history_end": snap.get("history_end", 0),
            "params": params, "units": u, **param_meta()}


def snapshot(raw_path: Path, kelvin=None) -> dict:
    from tests.gauntlet.benchmark.run_editor_benchmark import snapshot_xmp

    return flatten(snapshot_xmp(raw_path), kelvin)


def kelvin_for(camera: str | None):
    """A coeffs -> Kelvin inverter against the body's table, or None."""
    if not camera:
        return None
    from scouter.core import wb

    def inv(coeffs):
        try:
            return wb.kelvin(coeffs, camera).get("kelvin")
        except Exception:  # noqa: BLE001 - no table, or out of range
            return None

    return inv


def render_raster(raw_path: Path, out: Path, px: int = PREVIEW_PX):
    """darktable-cli render of raw + its sidecar to PPM (scout.render: own
    throwaway configdir per call), parsed by the engine."""
    from scouter.core import scout

    out.parent.mkdir(parents=True, exist_ok=True)
    scout.render(str(raw_path), out, px, "ppm")
    return parse_ppm(out.read_bytes())


def render_preview(raw_path: Path, out: Path, px: int = 512) -> Path:
    from scouter.core import scout

    out.parent.mkdir(parents=True, exist_ok=True)
    scout.render(str(raw_path), out, px, "png")
    return out


def stats(raster, masks: metrics.Masks | None = None):
    masks = masks or metrics.build_masks(raster)
    return metrics.region_stats(raster, masks), masks


def noise_floors(raw_path: Path, work: Path) -> tuple[dict, bool]:
    """Spec 5.2: export the untouched frame twice; floor = 3 x the difference."""
    a = render_raster(raw_path, work / "floor-a.ppm")
    b = render_raster(raw_path, work / "floor-b.ppm")
    masks = metrics.build_masks(a)
    return metrics.noise_floor(metrics.region_stats(a, masks, ("frame",)),
                               metrics.region_stats(b, masks, ("frame",)))
