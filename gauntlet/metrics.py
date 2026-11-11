# ruff: noqa: N806 - L, Ls keep CIELAB lightness (L*) notation
"""The measurement contract (spec 5, measurement JSON M1-M10). Pure stdlib.

Stats are computed on 8-bit sRGB rasters (a PPM export) over named regions.
Deltas are always after-minus-before on the SAME pixel set: region masks are
built from the before image and reused for the after image."""

from __future__ import annotations

import math
from dataclasses import dataclass

from gauntlet import color
from gauntlet.raster import Raster8

REGIONS = ("frame", "shadows", "highlights", "top", "centre", "corners")
STATS = ("ev", "L", "p01L", "p99L", "spread", "clip_high", "clip_low",
         "rb", "rg", "bg", "gm", "a", "b", "C", "noise", "sharp")
CLIP_HIGH, CLIP_LOW = 253, 2

#: Spec 5.2 minimum floors (never below these).
FLOOR_MIN = {"ev": 0.02, "L": 0.5, "p01L": 0.5, "p99L": 0.5, "spread": 0.5,
             "rb": 0.002, "gm": 0.002, "a": 0.5, "b": 0.5, "C": 0.5,
             "clip_high": 0.05, "clip_low": 0.05, "noise": 0.05, "sharp": 0.05,
             "de00": 0.2}

#: Spec 5.1 M1 tolerances by unit.
def tolerance(unit: str, value: float) -> float:
    if unit == "EV":
        return 0.02
    if unit == "K":
        return max(50.0, abs(value) * 0.01)
    if unit == "%":
        return 0.005  # 0.5 percentage points in internal (fraction) units
    if unit in ("°", "deg"):
        return 1.0
    if unit == "bool":
        return 0.0
    return 0.01  # ratio and anything unitless


def param_equal(unit: str, a: float | None, b: float | None, tol: float | None = None) -> bool:
    if a is None or b is None:
        return a is b
    t = tolerance(unit, b) if tol is None else tol
    if unit in ("°", "deg"):
        d = abs((a - b + 180.0) % 360.0 - 180.0)
        return d <= t
    return abs(a - b) <= t


@dataclass(frozen=True)
class Masks:
    """Pixel index lists per region, built once from the before image."""
    width: int
    height: int
    step: int
    regions: dict[str, list[int]]


_LUT = [color.srgb_to_linear(i / 255.0) for i in range(256)]


def _grid(width: int, height: int, max_samples: int) -> int:
    return max(1, int(math.sqrt(width * height / max_samples)))


def build_masks(before: Raster8, max_samples: int = 60_000) -> Masks:
    w, h = before.width, before.height
    step = _grid(w, h, max_samples)
    frame: list[int] = []
    top: list[int] = []
    centre: list[int] = []
    corners: list[int] = []
    Ls: list[tuple[float, int]] = []
    cw, ch = w * 0.1, h * 0.1
    for y in range(0, h, step):
        for x in range(0, w, step):
            idx = y * w + x
            frame.append(idx)
            if y < h / 3:
                top.append(idx)
            if w * 0.25 <= x < w * 0.75 and h * 0.25 <= y < h * 0.75:
                centre.append(idx)
            if (x < cw or x >= w - cw) and (y < ch or y >= h - ch):
                corners.append(idx)
            r, g, b = before.pixel(x, y)
            Ls.append((color.y_to_lstar(color.luminance(_LUT[r], _LUT[g], _LUT[b])), idx))
    Ls.sort()
    q = max(1, len(Ls) // 4)
    shadows = sorted(i for _, i in Ls[:q])
    highlights = sorted(i for _, i in Ls[-q:])
    return Masks(w, h, step, {"frame": frame, "shadows": shadows, "highlights": highlights,
                              "top": top, "centre": centre, "corners": corners})


def _pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    k = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def region_stats(r: Raster8, masks: Masks, regions: tuple[str, ...] = REGIONS) -> dict:
    """{region: {stat: value}} for the requested regions."""
    if (r.width, r.height) != (masks.width, masks.height):
        raise ValueError(f"raster {r.width}x{r.height} does not match masks "
                         f"{masks.width}x{masks.height}")
    out: dict[str, dict[str, float]] = {}
    data = r.data
    for name in regions:
        idxs = masks.regions[name]
        n = len(idxs)
        if n == 0:
            out[name] = {s: float("nan") for s in STATS}
            continue
        sr = sg = sb = sy = sa = sbb = sc = 0.0
        hi = lo = 0
        Ls: list[float] = []
        for idx in idxs:
            i = idx * 3
            r8, g8, b8 = data[i], data[i + 1], data[i + 2]
            lr, lg, lb = _LUT[r8], _LUT[g8], _LUT[b8]
            sr += lr
            sg += lg
            sb += lb
            yv = color.luminance(lr, lg, lb)
            sy += yv
            L, a, b = color.xyz_to_lab(*color.linear_rgb_to_xyz(lr, lg, lb))
            Ls.append(L)
            sa += a
            sbb += b
            sc += math.hypot(a, b)
            if r8 >= CLIP_HIGH or g8 >= CLIP_HIGH or b8 >= CLIP_HIGH:
                hi += 1
            if r8 <= CLIP_LOW and g8 <= CLIP_LOW and b8 <= CLIP_LOW:
                lo += 1
        Ls.sort()
        out[name] = {
            "ev": sy / n,
            "L": sum(Ls) / n,
            "p01L": _pct(Ls, 0.01),
            "p99L": _pct(Ls, 0.99),
            "spread": _pct(Ls, 0.95) - _pct(Ls, 0.05),
            "clip_high": 100.0 * hi / n,
            "clip_low": 100.0 * lo / n,
            "rb": (sr / sb) if sb else float("nan"),
            "rg": (sr / sg) if sg else float("nan"),
            "bg": (sb / sg) if sg else float("nan"),
            "gm": (sg / ((sr + sb) / 2)) if (sr + sb) else float("nan"),
            "a": sa / n,
            "b": sbb / n,
            "C": sc / n,
        }
    if "frame" in out:
        out["frame"]["sharp"] = sharpness(r)
        out["frame"]["noise"] = noise(r)
    return out


#: Linear-mean floor an `ev` patch clipped to black is measured at (2^-16).
EV_FLOOR = 2.0 ** -16


def delta(stat: str, before: float, after: float) -> float:
    """after - before; for `ev` the stops ratio log2(after/before) of linear means.
    L4: an `after` clipped to black (<= 0) is measured at EV_FLOOR, a finite
    LOWER BOUND on the real drop (the caller flags it as clipped); a `before`
    already at black has no ratio (nan, the caller reads it as unmeasurable)."""
    if stat == "ev":
        if before <= 0:
            return float("nan")
        return math.log2(max(after, EV_FLOOR) / before)
    return after - before


def _green(r: Raster8, x: int, y: int) -> int:
    return r.data[(y * r.width + x) * 3 + 1]


def sharpness(r: Raster8, step: int = 4) -> float:
    """M8: variance of the 3x3 discrete Laplacian of green, sampled every `step`."""
    vals: list[float] = []
    for y in range(1, r.height - 1, step):
        for x in range(1, r.width - 1, step):
            lap = (4 * _green(r, x, y) - _green(r, x - 1, y) - _green(r, x + 1, y)
                   - _green(r, x, y - 1) - _green(r, x, y + 1))
            vals.append(float(lap))
    if not vals:
        return 0.0
    m = sum(vals) / len(vals)
    return sum((v - m) ** 2 for v in vals) / len(vals)


def noise(r: Raster8, block: int = 16) -> float:
    """M7: median std-dev of green over the flattest 10% of `block`x`block` tiles."""
    sds: list[tuple[float, float]] = []
    for by in range(0, r.height - block + 1, block):
        for bx in range(0, r.width - block + 1, block):
            vals = [_green(r, x, y) for y in range(by, by + block, 2)
                    for x in range(bx, bx + block, 2)]
            m = sum(vals) / len(vals)
            sd = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))
            grad = abs(_green(r, bx, by) - _green(r, bx + block - 1, by + block - 1))
            sds.append((grad + sd, sd))
    if not sds:
        return 0.0
    sds.sort()
    flat = [sd for _, sd in sds[: max(1, len(sds) // 10)]]
    flat.sort()
    return flat[len(flat) // 2]


def noise_floor(first: dict, second: dict) -> tuple[dict[str, float], bool]:
    """Spec 5.2: floor = max(3 x |repeat difference|, minimum) per stat, from two
    exports of the untouched frame. Returns (floors, deterministic)."""
    floors: dict[str, float] = {}
    deterministic = True
    for stat, minimum in FLOOR_MIN.items():
        if stat == "de00":
            continue
        a = first.get("frame", {}).get(stat)
        b = second.get("frame", {}).get(stat)
        if a is None or b is None or any(map(math.isnan, (a, b))):
            floors[stat] = minimum
            continue
        observed = abs(delta(stat, a, b)) * 3.0
        floors[stat] = max(observed, minimum)
        if floors[stat] > 2 * minimum:
            deterministic = False
    floors["de00"] = FLOOR_MIN["de00"]
    return floors, deterministic


def de_verdict(values: list[float]) -> str:
    """Spec 5.1 'should not change': max < 2.3 and mean < 2.3/3 -> pass;
    max <= 5 -> drift; else fail."""
    if not values:
        return "pass"
    mx, mean = max(values), sum(values) / len(values)
    if mx < 2.3 and mean < 2.3 / 3:
        return "pass"
    if mx <= 5.0:
        return "drift"
    return "fail"


def patch_de(before: dict, after: dict) -> dict[str, float]:
    return {k: color.delta_e2000(before[k], after[k]) for k in before if k in after}
