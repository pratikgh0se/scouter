import pytest

from scouter.core import vocab
from tests.gauntlet.layers import coverage


def _case(cid: str):
    return next(x for x in coverage.generate() if x.id == cid)


def test_coverage_counts_and_p0_core():
    cases = coverage.generate()
    assert len(cases) == coverage.expected_count() == 1596
    assert len({c.id for c in cases}) == 1596
    assert sum(c.tier == "P0" for c in cases) == 350
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
    assert len(cases) == coverage.expected_count() == 1596


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


def test_kelvin_cases_no_longer_reset_tint_first_on_a_wb_route_a_refusal():
    """Superseded by rows 458/461 (COV-WB-preset4-expectation): the corpus
    hero's preset 4 refuses every temperature.temperature write outright
    (checkmap.wb_route_a_expect, candidate until pinned), which preempts
    item 4a's tint-reset prep entirely -- an item-4a tint-reset write would
    itself refuse the same way, so it is dropped, same as the (former)
    "no calibration table" refusal cases already dropped their prep."""
    c = _case("cov-temperature.temperature-olympus-adjust_up1")
    assert [s.tool for s in c.steps] == ["darkroom_adjust"]
    assert c.expect.kind == "typed_error"
    assert c.expect.codes == ["module_unavailable"]


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
# WB by body/range. Superseded by rows 458/461 (COV-WB-preset4-expectation):
# the corpus hero's preset 4 refuses every temperature.temperature write on
# EVERY body (checkmap.wb_route_a_expect, candidate until
# channelmixerrgb.temperature is pinned) -- this preempts item 4a's
# "no calibration table" distinction between sony_a7 and olympus, which
# never gets a chance to fire on a preset-4 frame. `_probe_lo_hi`'s
# WB_SAMPLED_K narrowing is still exercised (it decides the refused step's
# own request value, which matters once route A is pinned and live), but
# the boundary CHECK itself no longer applies to a refused write.


def test_temperature_adjust_refuses_on_every_body_preset_4():
    for body in ("sony_a7", "olympus"):
        c = _case(f"cov-temperature.temperature-{body}-adjust_up1")
        assert c.expect.kind == "typed_error"
        assert c.expect.codes == ["module_unavailable"]
        assert not c.meta.get("accept_refusal")  # a strict refusal, not a permissive one


def test_warmer_phrase_refuses_on_every_body_preset_4():
    for body in ("sony_a7", "olympus"):
        for label in ("bare", "touch", "lot", "explicit"):
            c = _case(f"cov-phrase-warmer-{body}-{label}")
            assert c.expect.kind == "typed_error", (body, label)
            assert c.expect.codes == ["module_unavailable"], (body, label)


def test_temperature_boundary_probe_requests_stay_inside_the_calibration_sample_range():
    for label in ("set_lo", "set_hi"):
        c = _case(f"cov-temperature.temperature-olympus-{label}")
        assert c.expect.kind == "typed_error"  # refused; no M1 check to read a value from
        v = c.steps[-1].args["value"]
        assert 3000.0 <= v <= 12000.0
    for label in ("set_lo_minus", "set_hi_plus"):
        c = _case(f"cov-temperature.temperature-olympus-{label}")
        assert c.expect.kind == "typed_error"
        v = c.steps[-1].args["value"]
        assert 3000.0 <= v <= 12000.0
