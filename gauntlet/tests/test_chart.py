import pytest

from gauntlet import chart, color
from gauntlet.raster import Raster8, encode_pfm, parse_pfm


def test_layout_has_24_patches_and_an_11_step_ramp_inside_the_frame():
    pts = chart.layout()
    assert len(pts) == 35
    assert sum(p.name.startswith("ramp_") for p in pts) == 11
    for p in pts:
        assert 0 <= p.x and p.x + p.w <= chart.WIDTH
        assert 0 <= p.y and p.y + p.h <= chart.HEIGHT


def test_patches_do_not_overlap():
    pts = chart.layout()
    for i, a in enumerate(pts):
        for b in pts[i + 1 :]:
            assert a.x + a.w <= b.x or b.x + b.w <= a.x or a.y + a.h <= b.y or b.y + b.h <= a.y, (
                a.name,
                b.name,
            )


def test_ramp_includes_the_looks_tone_points():
    names = {p.name: p.linear[0] for p in chart.layout()}
    assert names["ramp_+0"] == pytest.approx(0.18)
    assert names["ramp_-4"] == pytest.approx(0.18 / 16)
    assert names["ramp_+2"] == pytest.approx(0.72)


def test_build_samples_back_to_reference_labs():
    r = chart.build()
    got = chart.sample(r)
    ref = chart.reference_labs()
    for name, lab in ref.items():
        assert color.delta_e2000(got[name], lab) < 0.01, name


def test_pfm_round_trip_of_the_chart_is_exact():
    r = chart.build()
    assert parse_pfm(encode_pfm(r)).data == r.data


def test_sample_scales_to_a_smaller_8_bit_render():
    small_w, small_h = chart.WIDTH // 2, chart.HEIGHT // 2
    grey = color.linear_to_srgb(0.18)
    v = int(round(grey * 255))
    r8 = Raster8(small_w, small_h, bytes([v, v, v]) * (small_w * small_h))
    labs = chart.sample(r8)
    assert labs["red"][0] == pytest.approx(labs["ramp_+0"][0], abs=0.01)


def test_patch_stats_regions():
    st = chart.patch_stats(chart.build())
    assert st["patch:ramp_+0"]["ev"] == pytest.approx(0.18, abs=1e-4)
    assert abs(st["patch:ramp_+0"]["a"]) < 0.01
    assert st["chart_chromatic"]["C"] > 20
    assert st["chart_tone"]["slope"] > 0
