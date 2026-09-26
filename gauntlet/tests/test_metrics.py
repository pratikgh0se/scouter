# ruff: noqa: N802 - test name keeps CIELAB L (lightness) notation
import math

import pytest

from gauntlet import metrics
from gauntlet.raster import Raster8


def _flat(v, w=64, h=48, rgb=None):
    px = bytes(rgb or (v, v, v))
    return Raster8(w, h, px * (w * h))


def _gradient(w=64, h=48):
    data = bytearray()
    for y in range(h):
        for x in range(w):
            v = int(255 * x / (w - 1))
            data += bytes((v, v, v))
    return Raster8(w, h, bytes(data))


def test_masks_cover_expected_fractions():
    m = metrics.build_masks(_gradient())
    n = len(m.regions["frame"])
    assert n == 64 * 48
    assert len(m.regions["shadows"]) == n // 4
    assert len(m.regions["highlights"]) == n // 4
    assert len(m.regions["centre"]) == pytest.approx(n / 4, rel=0.05)
    assert 0 < len(m.regions["corners"]) < n * 0.05


def test_brighter_frame_has_positive_ev_and_L_delta():
    before, after = _flat(100), _flat(140)
    m = metrics.build_masks(before)
    sb = metrics.region_stats(before, m)["frame"]
    sa = metrics.region_stats(after, m)["frame"]
    assert metrics.delta("ev", sb["ev"], sa["ev"]) > 0.5
    assert metrics.delta("L", sb["L"], sa["L"]) > 10


def test_warmer_raises_rb_and_b():
    before = _flat(0, rgb=(120, 120, 120))
    after = _flat(0, rgb=(140, 120, 100))
    m = metrics.build_masks(before)
    sb, sa = (metrics.region_stats(x, m)["frame"] for x in (before, after))
    assert sa["rb"] > sb["rb"] and sa["b"] > sb["b"]


def test_clip_percentages():
    s = metrics.region_stats(_flat(255), metrics.build_masks(_flat(255)))["frame"]
    assert s["clip_high"] == 100.0 and s["clip_low"] == 0.0


def test_mask_size_mismatch_is_refused():
    with pytest.raises(ValueError):
        metrics.region_stats(_flat(1, 10, 10), metrics.build_masks(_flat(1)))


def test_ev_delta_of_zero_is_nan_not_crash():
    assert math.isnan(metrics.delta("ev", 0.0, 0.5))


def test_tolerances_follow_spec_5_1():
    assert metrics.tolerance("EV", 1.0) == 0.02
    assert metrics.tolerance("K", 5500) == 55.0
    assert metrics.tolerance("K", 3000) == 50.0
    assert metrics.param_equal("°", 359.5, 0.3)
    assert not metrics.param_equal("EV", 1.0, 1.05)
    assert metrics.param_equal("bool", 1, 1) and not metrics.param_equal("bool", 1, 0)


def test_noise_floor_minimums_and_non_determinism():
    s = {"frame": {"ev": 0.18, "L": 50.0}}
    floors, det = metrics.noise_floor(s, s)
    assert floors["ev"] == 0.02 and floors["L"] == 0.5 and det
    noisy = {"frame": {"ev": 0.18, "L": 52.0}}
    floors, det = metrics.noise_floor(s, noisy)
    assert floors["L"] == pytest.approx(6.0) and not det


def test_de_verdict_bands():
    assert metrics.de_verdict([0.5, 1.0]) == "pass"
    assert metrics.de_verdict([2.0, 2.0]) == "drift"  # mean over 2.3/3
    assert metrics.de_verdict([4.9]) == "drift"
    assert metrics.de_verdict([5.1]) == "fail"


def test_sharpness_and_noise_rank_images():
    flat = _flat(128)
    assert metrics.sharpness(flat) == 0.0
    assert metrics.sharpness(_gradient()) >= 0.0
    assert metrics.noise(flat) == 0.0
