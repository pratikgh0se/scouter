from tests.gauntlet import chart_render, exec_looks, taxonomy


def test_plan_moves_is_linear_and_clamped():
    look = {"targets": {"tone": {"shadows_ev": 0.2, "mids_ev": 0, "highlights_ev": 0,
                                 "contrast": 0}, "saturation_delta": -1,
                        "grey_cast": {"a": 0, "b": 2.0},
                        "split_toning": {"shadow_hue": 200, "shadow_sat": 0.1,
                                         "highlight_hue": None, "highlight_sat": 0},
                        "black_lift": 0}}
    cal = {"colorbalancergb.shadows_luminance": 2.0, "temperature.temperature": 0.004}
    moves, notes = exec_looks.plan_moves(look, cal, {"temperature.temperature": 5000.0})
    d = {(m, p): v for m, p, v in moves}
    assert d[("colorbalancergb", "shadows_luminance")] == 0.1
    assert d[("temperature", "temperature")] == 5500.0
    assert d[("colorbalancergb", "global_saturation")] == -0.5
    assert d[("colorbalancergb", "shadows_hue")] == 200.0
    assert any("monochrome" in n for n in notes)


def test_every_look_plans_without_error():
    cal = {k: 1.0 for k in chart_render.CALIBRATE}
    for look in taxonomy.load().looks:
        exec_looks.plan_moves(look, cal, {})
