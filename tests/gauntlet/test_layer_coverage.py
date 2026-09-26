import pytest

from scouter.core import vocab
from tests.gauntlet.layers import coverage


def _case(cid: str):
    return next(x for x in coverage.generate() if x.id == cid)


def test_coverage_counts_and_p0_core():
    cases = coverage.generate()
    assert len(cases) == coverage.expected_count() == 1316
    assert len({c.id for c in cases}) == 1316
    assert sum(c.tier == "P0" for c in cases) == 318
    assert sum(c.body == "sony_nex5n" for c in cases) == 20


def test_boundary_accepts_refusal_but_not_a_silent_clamp():
    c = _case("cov-exposure.exposure-olympus-set_lo_minus")
    assert c.meta["accept_refusal"]
    chk = c.expect.checks[0]
    assert chk.metric == "boundary" and chk.tags["silent_if_fail"]
    # the probe is one step below the row's lower bound, computed here from
    # the vocabulary row (exposure: lo -3.0 EV, step 0.3 EV), not read back
    # from the generated check
    s = vocab.spec("exposure", "exposure")
    assert (s.lo, s.step) == (-3.0, 0.3)
    assert chk.target["request"] == pytest.approx(s.lo - s.step) == -3.3
    assert chk.target["bound"] == s.lo
    assert c.steps[-1].args["value"] == chk.target["request"]


def test_kelvin_cases_reset_tint_first_and_offset_their_checks():
    c = _case("cov-temperature.temperature-olympus-adjust_up1")
    assert c.steps[0].args == {"module": "temperature", "param": "tint", "value": 1.0}
    assert c.expect.checks[0].target["from"] == 2


# ---------------------------------------------------------------- item 1:
# set_mid/set_neutral (and a wrapped set_hi) landing on the frame's own
# default is an honest noop, not a silent failure; set_mid itself is moved
# off the default so it stays a real probe.


def test_set_mid_is_a_real_move_off_the_symmetric_default():
    c = _case("cov-colorbalancergb.contrast-olympus-set_mid")
    v = c.expect.checks[0].target["value"]
    assert v != 0.0
    assert c.expect.sidecar["changed"] is True


def test_set_neutral_equal_to_default_expects_an_honest_noop():
    c = _case("cov-colorbalancergb.contrast-olympus-set_neutral")
    assert c.expect.checks[0].target["value"] == 0.0
    assert c.expect.sidecar["changed"] is False


def test_hue_set_hi_moves_off_the_wrap_equivalent_default():
    c = _case("cov-colorbalancergb.highlights_hue-olympus-set_hi")
    v = c.expect.checks[0].target["value"]
    assert v != 0.0 and v != 360.0
    assert c.expect.sidecar["changed"] is True


def test_hue_set_lo_equals_the_wrap_equivalent_default_expects_a_noop():
    c = _case("cov-colorbalancergb.highlights_hue-olympus-set_lo")
    assert c.expect.checks[0].target["value"] == 0.0
    assert c.expect.sidecar["changed"] is False


# ---------------------------------------------------------------- item 2:
# soft-range boundary sets and explicit phrase amounts beyond a param's
# range use the boundary check (either landed-as-requested or
# landed-and-reported-clamped), not a mandatory M1-equals-the-bound.


def test_set_hi_plus_uses_the_boundary_check():
    c = _case("cov-colorbalancergb.contrast-olympus-set_hi_plus")
    assert len(c.expect.checks) == 1
    chk = c.expect.checks[0]
    assert chk.metric == "boundary"
    assert chk.target["request"] == 0.55
    assert chk.target["bound"] == 0.5


def test_phrase_explicit_beyond_range_uses_the_boundary_check():
    c = _case("cov-phrase-deeper_blacks-olympus-explicit")
    assert len(c.expect.checks) == 1
    chk = c.expect.checks[0]
    assert chk.metric == "boundary"
    assert chk.target["delta"] == 0.2
    assert chk.target["lo"] == -0.1 and chk.target["hi"] == 0.1


# ---------------------------------------------------------------- item 3
# lives in test_layer_offline.py (the offline resolver sweep layer).


# ---------------------------------------------------------------- item 4:
# WB by body/range. sony_a7 has no calibration table, so a Kelvin-relative
# move (adjust/nudge/warmer/cooler) honestly refuses; olympus does have one,
# but its probe values must stay inside the table's sampled Kelvin range.


def test_temperature_adjust_on_uncalibrated_body_accepts_refusal():
    c = _case("cov-temperature.temperature-sony_a7-adjust_up1")
    assert c.meta.get("accept_refusal") is True


def test_temperature_adjust_on_calibrated_body_does_not_accept_refusal():
    c = _case("cov-temperature.temperature-olympus-adjust_up1")
    assert not c.meta.get("accept_refusal")


def test_warmer_phrase_on_uncalibrated_body_accepts_refusal():
    for label in ("bare", "touch", "lot", "explicit"):
        c = _case(f"cov-phrase-warmer-sony_a7-{label}")
        assert c.meta.get("accept_refusal") is True, label


def test_temperature_boundary_probes_stay_inside_the_calibration_sample_range():
    for label in ("set_lo", "set_hi"):
        c = _case(f"cov-temperature.temperature-olympus-{label}")
        v = c.expect.checks[0].target["value"]
        assert 3000.0 <= v <= 12000.0
    for label in ("set_lo_minus", "set_hi_plus"):
        c = _case(f"cov-temperature.temperature-olympus-{label}")
        v = c.expect.checks[0].target["request"]
        assert 3000.0 <= v <= 12000.0
