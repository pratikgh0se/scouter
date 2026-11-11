"""The synthetic chart (spec 4.3): 24 ColorChecker patches + an 11-step grey
ramp on an 18% grey surround, as linear Rec.709 float (PFM). Pure stdlib.

Patch reference values are the published post-2009 X-Rite sRGB (D65) 8-bit
values, converted to linear light. The chart is scene-referred input; every
look delta is measured against the untouched render of this same chart."""

# ruff: noqa: N806 - L (Lab lightness) keeps colour-science notation

from __future__ import annotations

from array import array
from dataclasses import dataclass

from gauntlet import color
from gauntlet.raster import Raster8, RasterF

WIDTH, HEIGHT = 1536, 1152
PATCH = 128
SURROUND = 0.18
VERSION = "chart-v1"

#: X-Rite ColorChecker Classic, post-November-2009 formulation, sRGB 8-bit.
COLORCHECKER_SRGB: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("dark_skin", (115, 82, 68)),
    ("light_skin", (194, 150, 130)),
    ("blue_sky", (98, 122, 157)),
    ("foliage", (87, 108, 67)),
    ("blue_flower", (133, 128, 177)),
    ("bluish_green", (103, 189, 170)),
    ("orange", (214, 126, 44)),
    ("purplish_blue", (80, 91, 166)),
    ("moderate_red", (193, 90, 99)),
    ("purple", (94, 60, 108)),
    ("yellow_green", (157, 188, 64)),
    ("orange_yellow", (224, 163, 46)),
    ("blue", (56, 61, 150)),
    ("green", (70, 148, 73)),
    ("red", (175, 54, 60)),
    ("yellow", (231, 199, 31)),
    ("magenta", (187, 86, 149)),
    ("cyan", (8, 133, 161)),
    ("white_9_5", (243, 243, 242)),
    ("neutral_8", (200, 200, 200)),
    ("neutral_6_5", (160, 160, 160)),
    ("neutral_5", (122, 122, 121)),
    ("neutral_3_5", (85, 85, 85)),
    ("black_2", (52, 52, 52)),
)
#: The 18 chromatic patches (the looks' saturation target set).
CHROMATIC = tuple(name for name, _ in COLORCHECKER_SRGB[:18])
#: Grey ramp exponents: 18% x 2^k.
RAMP_K = tuple(range(-5, 6))


@dataclass(frozen=True)
class Patch:
    name: str
    x: int
    y: int
    w: int
    h: int
    linear: tuple[float, float, float]


def layout() -> list[Patch]:
    patches: list[Patch] = []
    for i, (name, rgb8) in enumerate(COLORCHECKER_SRGB):
        row, col = divmod(i, 6)
        lin = tuple(color.srgb_to_linear(c / 255.0) for c in rgb8)
        patches.append(Patch(name, 128 + col * 192, 128 + row * 192, PATCH, PATCH, lin))
    for j, k in enumerate(RAMP_K):
        v = SURROUND * (2.0**k)
        patches.append(Patch(f"ramp_{k:+d}", 24 + j * 136, 896, PATCH, PATCH, (v, v, v)))
    return patches


def build() -> RasterF:
    data = array("f", [SURROUND]) * (WIDTH * HEIGHT * 3)
    for p in layout():
        for y in range(p.y, p.y + p.h):
            base = (y * WIDTH + p.x) * 3
            data[base : base + p.w * 3] = array("f", p.linear) * p.w
    return RasterF(WIDTH, HEIGHT, data)


def _inner(p: Patch, sx: float, sy: float, inner: float) -> tuple[int, int, int, int]:
    mx, my = p.w * (1 - inner) / 2, p.h * (1 - inner) / 2
    x0, y0 = int((p.x + mx) * sx), int((p.y + my) * sy)
    x1, y1 = int((p.x + p.w - mx) * sx), int((p.y + p.h - my) * sy)
    return x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)


def sample(r: Raster8 | RasterF, inner: float = 0.6) -> dict[str, tuple[float, float, float]]:
    """Mean Lab of each patch's centre `inner` fraction, scaled to `r`'s size.
    Raster8 is sRGB-encoded (decoded first); RasterF is linear already."""
    sx, sy = r.width / WIDTH, r.height / HEIGHT
    lut = [color.srgb_to_linear(i / 255.0) for i in range(256)]
    out: dict[str, tuple[float, float, float]] = {}
    for p in layout():
        x0, y0, x1, y1 = _inner(p, sx, sy, inner)
        acc = [0.0, 0.0, 0.0]
        n = 0
        for y in range(y0, y1):
            for x in range(x0, x1):
                px = r.pixel(x, y)
                if isinstance(r, Raster8):
                    px = (lut[px[0]], lut[px[1]], lut[px[2]])
                acc[0] += px[0]
                acc[1] += px[1]
                acc[2] += px[2]
                n += 1
        mean = (acc[0] / n, acc[1] / n, acc[2] / n)
        out[p.name] = color.xyz_to_lab(*color.linear_rgb_to_xyz(*mean))
    return out


def reference_labs() -> dict[str, tuple[float, float, float]]:
    return {p.name: color.xyz_to_lab(*color.linear_rgb_to_xyz(*p.linear)) for p in layout()}


def patch_stats(r: Raster8 | RasterF) -> dict[str, dict[str, float]]:
    """Per-patch regions for the evaluator: `patch:<name>` -> {L, a, b, C, ev}
    (ev = linear Y, so metrics.delta('ev') gives stops), plus
    `chart_chromatic` -> {C: mean C* of the 18 chromatic patches} and
    `chart_tone` -> {slope: L*(ramp_+2) - L*(ramp_-2)}."""
    labs = sample(r)
    out: dict[str, dict[str, float]] = {}
    for name, (L, a, b) in labs.items():
        c, _h = color.lch(a, b)
        _x, y, _z = color.lab_to_xyz(L, a, b)
        out[f"patch:{name}"] = {"L": L, "a": a, "b": b, "C": c, "ev": y}
    out["chart_chromatic"] = {"C": sum(out[f"patch:{n}"]["C"] for n in CHROMATIC) / len(CHROMATIC)}
    out["chart_tone"] = {"slope": out["patch:ramp_+2"]["L"] - out["patch:ramp_-2"]["L"]}
    return out
