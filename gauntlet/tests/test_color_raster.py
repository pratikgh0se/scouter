# ruff: noqa: N806 - L is CIELAB lightness (L*)
from array import array

import pytest

from gauntlet import color
from gauntlet.raster import (
    Raster8,
    RasterF,
    encode_pfm,
    encode_ppm,
    encode_tiff16,
    parse_pfm,
    parse_ppm,
)

# Sharma, Wu and Dalal (2005), "The CIEDE2000 color-difference formula",
# Table 1: all 34 published test pairs (L1, a1, b1), (L2, a2, b2), dE00.
SHARMA_PAIRS = [
    ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
    ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
    ((50.0000, 2.8361, -74.0200), (50.0000, 0.0000, -82.7485), 3.4412),
    ((50.0000, -1.3802, -84.2814), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -1.1848, -84.8006), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -0.9009, -85.5211), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, 0.0000, 0.0000), (50.0000, -1.0000, 2.0000), 2.3669),
    ((50.0000, -1.0000, 2.0000), (50.0000, 0.0000, 0.0000), 2.3669),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0009), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0010), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0011), 7.2195),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0012), 7.2195),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0009, -2.4900), 4.8045),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0010, -2.4900), 4.8045),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0011, -2.4900), 4.7461),
    ((50.0000, 2.5000, 0.0000), (50.0000, 0.0000, -2.5000), 4.3065),
    ((50.0000, 2.5000, 0.0000), (73.0000, 25.0000, -18.0000), 27.1492),
    ((50.0000, 2.5000, 0.0000), (61.0000, -5.0000, 29.0000), 22.8977),
    ((50.0000, 2.5000, 0.0000), (56.0000, -27.0000, -3.0000), 31.9030),
    ((50.0000, 2.5000, 0.0000), (58.0000, 24.0000, 15.0000), 19.4535),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.1736, 0.5854), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2972, 0.0000), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 1.8634, 0.5757), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2592, 0.3350), 1.0000),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((63.0109, -31.0961, -5.8663), (62.8187, -29.7946, -4.0864), 1.2630),
    ((61.2901, 3.7196, -5.3901), (61.4292, 2.2480, -4.9620), 1.8731),
    ((35.0831, -44.1164, 3.7933), (35.0232, -40.0716, 1.5901), 1.8645),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
    ((36.4612, 47.8580, 18.3852), (36.2715, 50.5065, 21.2231), 1.4146),
    ((90.8027, -2.0831, 1.4410), (91.1528, -1.6435, 0.0447), 1.4441),
    ((90.9257, -0.5406, -0.9208), (88.6381, -0.8985, -0.7239), 1.5381),
    ((6.7747, -0.2908, -2.4247), (5.8714, -0.0985, -2.2286), 0.6377),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
]


@pytest.mark.parametrize("lab1,lab2,expected", SHARMA_PAIRS)
def test_ciede2000_matches_sharma_table(lab1, lab2, expected):
    assert color.delta_e2000(lab1, lab2) == pytest.approx(expected, abs=5e-5)
    assert color.delta_e2000(lab2, lab1) == pytest.approx(expected, abs=5e-5)


def test_there_are_34_pairs():
    assert len(SHARMA_PAIRS) == 34


def test_srgb_round_trip_and_white():
    for v in (0.0, 0.02, 0.5, 1.0):
        assert color.linear_to_srgb(color.srgb_to_linear(v)) == pytest.approx(v, abs=1e-9)
    L, a, b = color.srgb8_to_lab(255, 255, 255)
    assert (L, a, b) == pytest.approx((100.0, 0.0, 0.0), abs=0.02)


def test_mid_grey_lab_and_lch():
    L, a, b = color.xyz_to_lab(*color.linear_rgb_to_xyz(0.18, 0.18, 0.18))
    assert L == pytest.approx(49.5, abs=0.1)
    assert abs(a) < 0.01 and abs(b) < 0.01
    c, h = color.lch(0.0, 10.0)
    assert (c, h) == pytest.approx((10.0, 90.0))


def test_lab_linear_round_trip():
    rgb = color.lab_to_linear_rgb(*color.xyz_to_lab(*color.linear_rgb_to_xyz(0.3, 0.2, 0.1)))
    assert rgb == pytest.approx((0.3, 0.2, 0.1), abs=1e-5)


def test_ppm_round_trip_and_16_bit_reduction():
    r = Raster8(2, 1, bytes([1, 2, 3, 250, 251, 252]))
    assert parse_ppm(encode_ppm(r)) == r
    sixteen = b"P6\n1 1\n65535\n" + bytes([0x12, 0x34, 0xAB, 0xCD, 0xFF, 0x00])
    assert parse_ppm(sixteen).data == bytes([0x12, 0xAB, 0xFF])


def test_ppm_rejects_short_payload_and_wrong_magic():
    with pytest.raises(ValueError):
        parse_ppm(b"P6\n2 2\n255\n\x00\x00")
    with pytest.raises(ValueError):
        parse_ppm(b"P3\n1 1\n255\n0 0 0")


def test_pfm_round_trip_keeps_top_row_first():
    data = array("f", [0.1, 0.2, 0.3, 1.5, 2.5, 3.5])  # 1 wide, 2 tall: top then bottom
    r = RasterF(1, 2, data)
    back = parse_pfm(encode_pfm(r))
    assert list(back.data) == pytest.approx(list(data))
    raw = encode_pfm(r)
    assert raw.startswith(b"PF\n1 2\n-1.0\n")


def test_tiff16_header_and_size():
    r = RasterF(2, 2, array("f", [0.5] * 12))
    t = encode_tiff16(r)
    assert t[:4] == b"II*\x00"
    assert len(t) > 2 * 2 * 3 * 2
    assert t[-2:] == (32768).to_bytes(2, "little")
