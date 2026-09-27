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


# ---------------------------------------------------------------- night-1
# row 471, COV-weaker-at-zero: "weaker colour grade" on the hero frame's
# never-graded chroma rows, and colorzones.strength's coarse native nudge,
# both landed exactly back on the frame's own baseline -- a real, honestly
# reported move that read identically to "nothing happened" once only the
# sidecar was compared.


def test_weaker_grade_bare_gets_a_graded_precondition_first():
    c = _case("cov-phrase-weaker_grade-olympus-bare")
    assert [s.tool for s in c.steps] == ["darkroom_say", "darkroom_say"]
    assert c.steps[0].args["phrase"] == "stronger colour grade a lot"
    assert c.steps[1].args["phrase"] == "weaker colour grade"
    # the "changed" checks compare from the snapshot AFTER the grading
    # precondition, not from the frame's own (ungraded) start -- otherwise
    # a same-sized weaken cancels the grade step exactly and looks
    # unchanged from step 0 again.
    assert c.expect.checks
    for chk in c.expect.checks:
        assert chk.target["from"] == 1


def test_weaker_grade_touch_lot_and_synonyms_all_get_the_precondition_too():
    for cid in ("cov-phrase-weaker_grade-olympus-touch", "cov-phrase-weaker_grade-olympus-lot",
                "cov-phrase-weaker_grade-sony_a7-bare",
                "cov-synonym-weaker_grade-less_grade-olympus"):
        c = _case(cid)
        assert c.steps[0].args["phrase"] == "stronger colour grade a lot", cid
        assert all(chk.target["from"] == 1 for chk in c.expect.checks), cid


def test_weaker_grade_explicit_is_unaffected_a_multi_move_phrase_refuses_by_n():
    # 'weaker grade by N' has no precondition to add: it is refused (usage)
    # before any write, exactly like the other multi-move phrases.
    c = _case("cov-phrase-weaker_grade-olympus-explicit")
    assert c.expect.kind == "typed_error"
    assert c.steps == [c.steps[0]]


def test_stronger_grade_itself_keeps_no_precondition():
    c = _case("cov-phrase-stronger_grade-olympus-bare")
    assert [s.tool for s in c.steps] == ["darkroom_say"]
    assert c.expect.checks[0].target["from"] == 0


def test_colorzones_strength_nudge_down_precondition_is_away_from_the_baseline():
    c = _case("cov-colorzones.strength-sony_a7-nudge_dn1")
    set_step = c.steps[-2]
    assert set_step.tool == "darkroom_set"
    assert set_step.args["value"] == 2.0  # s.hi -- one native step down cannot reach 0.0
    # the up direction was never at risk (it can't nudge past the baseline
    # at s.lo going up), so it keeps the plain midpoint precondition.
    up = _case("cov-colorzones.strength-sony_a7-nudge_up1")
    assert up.steps[-2].args["value"] == 1.0
