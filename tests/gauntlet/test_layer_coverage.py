from tests.gauntlet.layers import coverage


def test_coverage_counts_and_p0_core():
    cases = coverage.generate()
    assert len(cases) == coverage.expected_count() == 1450
    assert len({c.id for c in cases}) == 1450
    assert sum(c.tier == "P0" for c in cases) == 334
    assert sum(c.body == "sony_nex5n" for c in cases) == 20


def test_boundary_accepts_refusal_but_not_a_silent_clamp():
    c = next(x for x in coverage.generate()
             if x.id == "cov-exposure.exposure-olympus-set_lo_minus")
    assert c.meta["accept_refusal"] and c.expect.checks[1].tags["silent_if_fail"]


def test_kelvin_cases_reset_tint_first_and_offset_their_checks():
    c = next(x for x in coverage.generate()
             if x.id == "cov-temperature.temperature-olympus-adjust_up1")
    assert c.steps[0].args == {"module": "temperature", "param": "tint", "value": 1.0}
    assert c.expect.checks[0].target["from"] == 2
