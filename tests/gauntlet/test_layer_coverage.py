from tests.gauntlet.layers import coverage


def test_coverage_counts_and_p0_core():
    cases = coverage.generate()
    assert len(cases) == coverage.expected_count() == 1316
    assert len({c.id for c in cases}) == 1316
    assert sum(c.tier == "P0" for c in cases) == 318
    assert sum(c.body == "sony_nex5n" for c in cases) == 20


def test_touch_floor_residual_cases_target_hero_bright_on_olympus_only():
    """Cluster D / row 458: the 6 olympus KNOWN_RESIDUAL touch cases (see
    tests/test_touch_floor.py) move to the brighter stand-in hero; the 4
    sony_a7 KNOWN_RESIDUAL touch cases are untouched (no bright sony hero
    yet) and the total case count is unchanged."""
    cases = coverage.generate()
    by_id = {c.id: c for c in cases}
    expect_bright = {
        "cov-phrase-deepen_shadows-olympus-touch", "cov-phrase-lift_shadows-olympus-touch",
        "cov-phrase-more_vibrance-olympus-touch", "cov-phrase-less_vibrance-olympus-touch",
        "cov-phrase-cool_shadows-olympus-touch", "cov-phrase-teal_orange-olympus-touch",
    }
    for cid in expect_bright:
        assert by_id[cid].frame_role == "hero_bright", cid
    unaffected = {
        "cov-phrase-deepen_shadows-sony_a7-touch", "cov-phrase-lift_shadows-sony_a7-touch",
        "cov-phrase-more_vibrance-sony_a7-touch", "cov-phrase-less_vibrance-sony_a7-touch",
        # bare/lot forms of the same olympus phrases stay on the plain hero
        "cov-phrase-deepen_shadows-olympus-bare", "cov-phrase-deepen_shadows-olympus-lot",
    }
    for cid in unaffected:
        assert by_id[cid].frame_role == "hero", cid
    assert sum(c.frame_role == "hero_bright" for c in cases) == 6
    assert len(cases) == coverage.expected_count() == 1316


def test_boundary_accepts_refusal_but_not_a_silent_clamp():
    c = next(x for x in coverage.generate()
             if x.id == "cov-exposure.exposure-olympus-set_lo_minus")
    assert c.meta["accept_refusal"] and c.expect.checks[1].tags["silent_if_fail"]


def test_kelvin_cases_reset_tint_first_and_offset_their_checks():
    c = next(x for x in coverage.generate()
             if x.id == "cov-temperature.temperature-olympus-adjust_up1")
    assert c.steps[0].args == {"module": "temperature", "param": "tint", "value": 1.0}
    assert c.expect.checks[0].target["from"] == 2
