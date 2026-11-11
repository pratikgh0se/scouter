"""Colour science for the evaluator (spec 2.4). Pure stdlib.

sRGB <-> linear, Rec.709 luminance, XYZ (D65), CIELAB, LCh and CIEDE2000
(Sharma, Wu and Dalal 2005)."""

# ruff: noqa: N803, N806 - L*, C*, kL, C1p etc. keep Sharma et al.'s notation

from __future__ import annotations

import math

#: D65 reference white, Y = 1 scale.
WHITE_D65 = (0.95047, 1.0, 1.08883)
_M_RGB_XYZ = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)
_M_XYZ_RGB = (
    (3.2404542, -1.5371385, -0.4985314),
    (-0.9692660, 1.8760108, 0.0415560),
    (0.0556434, -0.2040259, 1.0572252),
)


def srgb_to_linear(v: float) -> float:
    """One sRGB-encoded channel in 0..1 to linear light."""
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def linear_to_srgb(v: float) -> float:
    v = min(max(v, 0.0), 1.0)
    return 12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055


def luminance(r: float, g: float, b: float) -> float:
    """Rec.709 relative luminance of LINEAR rgb."""
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _f(t: float) -> float:
    d = 6 / 29
    return t ** (1 / 3) if t > d**3 else t / (3 * d * d) + 4 / 29


def _finv(t: float) -> float:
    d = 6 / 29
    return t**3 if t > d else 3 * d * d * (t - 4 / 29)


def y_to_lstar(y: float) -> float:
    return 116 * _f(max(y, 0.0)) - 16


def linear_rgb_to_xyz(r: float, g: float, b: float) -> tuple[float, float, float]:
    m = _M_RGB_XYZ
    return (
        m[0][0] * r + m[0][1] * g + m[0][2] * b,
        m[1][0] * r + m[1][1] * g + m[1][2] * b,
        m[2][0] * r + m[2][1] * g + m[2][2] * b,
    )


def xyz_to_linear_rgb(x: float, y: float, z: float) -> tuple[float, float, float]:
    m = _M_XYZ_RGB
    return (
        m[0][0] * x + m[0][1] * y + m[0][2] * z,
        m[1][0] * x + m[1][1] * y + m[1][2] * z,
        m[2][0] * x + m[2][1] * y + m[2][2] * z,
    )


def xyz_to_lab(x: float, y: float, z: float) -> tuple[float, float, float]:
    xn, yn, zn = WHITE_D65
    fx, fy, fz = _f(x / xn), _f(y / yn), _f(z / zn)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def lab_to_xyz(L: float, a: float, b: float) -> tuple[float, float, float]:
    xn, yn, zn = WHITE_D65
    fy = (L + 16) / 116
    return xn * _finv(fy + a / 500), yn * _finv(fy), zn * _finv(fy - b / 200)


def srgb8_to_lab(r8: int, g8: int, b8: int) -> tuple[float, float, float]:
    r, g, b = (srgb_to_linear(c / 255.0) for c in (r8, g8, b8))
    return xyz_to_lab(*linear_rgb_to_xyz(r, g, b))


def lab_to_linear_rgb(L: float, a: float, b: float) -> tuple[float, float, float]:
    return xyz_to_linear_rgb(*lab_to_xyz(L, a, b))


def lch(a: float, b: float) -> tuple[float, float]:
    """(C*, h in degrees 0..360)."""
    return math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360.0


def delta_e2000(
    lab1: tuple[float, float, float],
    lab2: tuple[float, float, float],
    kL: float = 1.0,
    kC: float = 1.0,
    kH: float = 1.0,
) -> float:
    """CIEDE2000 per Sharma, Wu and Dalal (2005), including their hue rules."""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    C1, C2 = math.hypot(a1, b1), math.hypot(a2, b2)
    Cb = (C1 + C2) / 2
    G = 0.5 * (1 - math.sqrt(Cb**7 / (Cb**7 + 25.0**7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = 0.0 if (a1p == 0 and b1 == 0) else math.degrees(math.atan2(b1, a1p)) % 360
    h2p = 0.0 if (a2p == 0 and b2 == 0) else math.degrees(math.atan2(b2, a2p)) % 360
    dLp = L2 - L1
    dCp = C2p - C1p
    if C1p * C2p == 0:
        dhp = 0.0
    else:
        dhp = h2p - h1p
        if dhp > 180:
            dhp -= 360
        elif dhp < -180:
            dhp += 360
    dHp = 2 * math.sqrt(C1p * C2p) * math.sin(math.radians(dhp / 2))
    Lbp = (L1 + L2) / 2
    Cbp = (C1p + C2p) / 2
    if C1p * C2p == 0:
        hbp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hbp = (h1p + h2p + 360) / 2
    else:
        hbp = (h1p + h2p - 360) / 2
    T = (
        1
        - 0.17 * math.cos(math.radians(hbp - 30))
        + 0.24 * math.cos(math.radians(2 * hbp))
        + 0.32 * math.cos(math.radians(3 * hbp + 6))
        - 0.20 * math.cos(math.radians(4 * hbp - 63))
    )
    dtheta = 30 * math.exp(-(((hbp - 275) / 25) ** 2))
    Rc = 2 * math.sqrt(Cbp**7 / (Cbp**7 + 25.0**7))
    Sl = 1 + (0.015 * (Lbp - 50) ** 2) / math.sqrt(20 + (Lbp - 50) ** 2)
    Sc = 1 + 0.045 * Cbp
    Sh = 1 + 0.015 * Cbp * T
    Rt = -math.sin(math.radians(2 * dtheta)) * Rc
    return math.sqrt(
        (dLp / (kL * Sl)) ** 2
        + (dCp / (kC * Sc)) ** 2
        + (dHp / (kH * Sh)) ** 2
        + Rt * (dCp / (kC * Sc)) * (dHp / (kH * Sh))
    )
