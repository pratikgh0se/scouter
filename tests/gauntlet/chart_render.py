"""The synthetic chart in darktable (spec 4.3, P7): write chart-v1.pfm into
the run, render it untouched through darktable-cli, compare to the committed
golden per-patch Lab, and measure the per-run look calibration."""

from __future__ import annotations

import json
from pathlib import Path

from gauntlet import chart, metrics
from gauntlet.raster import encode_pfm, encode_tiff16
from gauntlet.store import atomic_write_text

GOLDEN = Path(__file__).resolve().parent / "golden" / "chart-v1.stats.json"
RENDER_PX = 1536
#: live params the look plan uses -> (metric region, stat) measured per unit.
CALIBRATE = {
    "colorbalancergb.shadows_luminance": ("patch:ramp_-4", "ev"),
    "colorbalancergb.midtones_luminance": ("patch:ramp_+0", "ev"),
    "colorbalancergb.highlights_luminance": ("patch:ramp_+2", "ev"),
    "sigmoid.contrast": ("chart_tone", "slope"),
    "colorbalancergb.global_chroma": ("chart_chromatic", "C"),
    "temperature.temperature": ("patch:ramp_+0", "b"),
    "exposure.black": ("patch:ramp_-5", "L"),
}
#: probe amounts per param (internal units) used to measure the slope.
PROBE = {
    "colorbalancergb.shadows_luminance": 0.1,
    "colorbalancergb.midtones_luminance": 0.1,
    "colorbalancergb.highlights_luminance": 0.1,
    "sigmoid.contrast": 0.3,
    "colorbalancergb.global_chroma": 0.2,
    "temperature.temperature": 600.0,
    "exposure.black": -0.01,
}


def write_chart(run_root: Path, fmt: str = "pfm") -> Path:
    """chart/chart-v1.pfm (or .tif, the fallback if darktable rejects PFM)."""
    img = chart.build()
    out = run_root / "chart" / f"{chart.VERSION}.{'pfm' if fmt == 'pfm' else 'tif'}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(encode_pfm(img) if fmt == "pfm" else encode_tiff16(img, clip=4.0))
    return out


def render_stats(src: Path, out_ppm: Path) -> dict:
    from tests.gauntlet import observe

    return chart.patch_stats(observe.render_raster(src, out_ppm, RENDER_PX))


def labs(stats: dict) -> dict[str, tuple[float, float, float]]:
    return {
        k.removeprefix("patch:"): (v["L"], v["a"], v["b"])
        for k, v in stats.items()
        if k.startswith("patch:")
    }


def compare_golden(stats: dict, golden: Path = GOLDEN) -> dict:
    if not golden.exists():
        return {"golden": None, "verdict": "no_golden", "patch_de": []}
    ref = json.loads(golden.read_text(encoding="utf-8"))["labs"]
    de = metrics.patch_de({k: tuple(v) for k, v in ref.items()}, labs(stats))
    return {
        "golden": str(golden),
        "verdict": metrics.de_verdict(list(de.values())),
        "patch_de": sorted(de.values(), reverse=True),
        "worst": max(de, key=de.get),
    }


def write_golden_candidate(run_root: Path, stats: dict) -> Path:
    """Written every run; promoted to tests/gauntlet/golden/ only by an
    explicit, Fable-reviewed --rebaseline commit (spec 4.3)."""
    out = run_root / "chart" / "chart-v1.stats.candidate.json"
    atomic_write_text(
        out,
        json.dumps({"version": chart.VERSION, "labs": labs(stats)}, indent=2, sort_keys=True)
        + "\n",
    )
    return out


def slope(before: dict, after: dict, key: str, amount: float) -> float | None:
    region, stat = CALIBRATE[key]
    b, a = before[region][stat], after[region][stat]
    d = metrics.delta(stat, b, a)
    return None if amount == 0 else d / amount
