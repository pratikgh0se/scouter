import pytest

from scouter.core import vocab
from tests.gauntlet import checkmap
from tests.gauntlet.layers import coverage


def _case(cid: str):
    return next(x for x in coverage.generate() if x.id == cid)


def test_coverage_counts_and_p0_core():
    cases = coverage.generate()
    assert len(cases) == coverage.expected_count() == 1638
    assert len({c.id for c in cases}) == 1638
    assert sum(c.tier == "P0" for c in cases) == 362
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
    assert len(cases) == coverage.expected_count() == 1638


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


@pytest.mark.usefixtures("route_a_unpinned")
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


@pytest.mark.usefixtures("route_a_unpinned")
def test_temperature_adjust_refuses_on_every_body_preset_4():
    for body in ("sony_a7", "olympus"):
        c = _case(f"cov-temperature.temperature-{body}-adjust_up1")
        assert c.expect.kind == "typed_error"
        assert c.expect.codes == ["module_unavailable"]
        assert not c.meta.get("accept_refusal")  # a strict refusal, not a permissive one


@pytest.mark.usefixtures("route_a_unpinned")
def test_warmer_phrase_refuses_on_every_body_preset_4():
    for body in ("sony_a7", "olympus"):
        for label in ("bare", "touch", "lot", "explicit"):
            c = _case(f"cov-phrase-warmer-{body}-{label}")
            assert c.expect.kind == "typed_error", (body, label)
            assert c.expect.codes == ["module_unavailable"], (body, label)


@pytest.mark.usefixtures("route_a_unpinned")
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


# ---------------------------------------------------------------- fix round
# 1 (review I1-I4): the WB route-A expectation, corrected.


def test_relative_tint_moves_expect_value_unknown_not_module_unavailable():
    """Review I1: temperature.tint's vocab status is absolute_only, so a
    RELATIVE tint move (adjust, or the more_magenta/more_green say phrase
    and its synonyms/P4/P5 forms) hits moves.gate(relative=True)'s
    ValueUnknown before refuse_wb_late ever runs -- on every preset, not
    just the corpus hero's preset 4. Only an ABSOLUTE tint darkroom_set
    reaches refuse_wb_late and is module_unavailable."""
    adj = _case("cov-temperature.tint-olympus-adjust")
    assert adj.expect.kind == "typed_error" and adj.expect.codes == ["value_unknown"]
    say = _case("cov-temperature.tint-olympus-say_more_magenta")
    assert say.expect.kind == "typed_error" and say.expect.codes == ["value_unknown"]
    phrase_bare = _case("cov-phrase-more_magenta-olympus-bare")
    assert phrase_bare.expect.kind == "typed_error"
    assert phrase_bare.expect.codes == ["value_unknown"]
    syn = _case("cov-synonym-more_green-more_green-olympus")
    assert syn.expect.kind == "typed_error" and syn.expect.codes == ["value_unknown"]
    # the absolute SET is unchanged -- it does reach refuse_wb_late.
    absolute = _case("cov-temperature.tint-olympus-set_0.9")
    assert absolute.expect.kind == "typed_error"
    assert absolute.expect.codes == ["module_unavailable"]


@pytest.mark.usefixtures("route_a_unpinned")
def test_undo_after_a_refused_wb_write_keeps_the_antecedent_and_expects_noop():
    """Review I2: the undo_after_set/undo_after_adjust cases must keep the
    refused set/adjust as a real step (it is what leaves the journal empty
    for the undo to find) and expect the undo's OWN code (its "noop"
    outcome, an empty journal), never the antecedent's module_unavailable."""
    c = _case("cov-temperature.temperature-olympus-undo_after_set")
    assert [s.tool for s in c.steps] == ["darkroom_set", "darkroom_undo_last"]
    assert c.expect.kind == "typed_error" and c.expect.codes == ["noop"]
    c2 = _case("cov-temperature.temperature-olympus-undo_after_adjust")
    assert [s.tool for s in c2.steps] == ["darkroom_adjust", "darkroom_undo_last"]
    assert c2.expect.kind == "typed_error" and c2.expect.codes == ["noop"]


@pytest.mark.usefixtures("route_a_unpinned")
def test_mixed_commutative_p2_pair_stays_an_honest_property_not_a_deterministic_fail():
    """Review I3: cov-p2-brighter-warmer's "warmer" half refuses on the
    corpus hero's preset 4, but "brighter" (the non-WB half) still lands
    both times it says (steps 0 and 5) -- the case must not become a single
    `typed_error` (the last step, say(brighter), always succeeds, so a
    live rerun would fail it deterministically). It stays `property` P2,
    scoped to the non-WB key, with an explicit envelope ok==false and error
    code check on each of warmer's own two (refused) steps."""
    c = _case("cov-p2-brighter-warmer-olympus")
    assert c.expect.kind == "property" and c.expect.property == "P2"
    m1 = next(chk for chk in c.expect.checks if chk.metric == "M1")
    assert m1.target["keys"] == ["exposure.exposure"]
    env_checks = [chk for chk in c.expect.checks if chk.metric == "envelope"]
    assert {chk.step for chk in env_checks} == {1, 4}
    for chk in env_checks:
        if chk.target["path"] == "ok":
            assert chk.target == {"path": "ok", "op": "eq", "value": False}
        else:
            assert chk.target == {"path": "error.code", "op": "eq", "value": "module_unavailable"}
    # a pure non-WB pair is unaffected
    c2 = _case("cov-p2-more_contrast-more_saturation-olympus")
    assert c2.expect.kind == "property"
    assert not any(chk.metric == "envelope" for chk in c2.expect.checks)
    # a pure WB pair (both sides refuse) is still an honest single refusal
    c3 = _case("cov-p5-warmer-cooler-olympus")
    assert c3.expect.kind == "typed_error"


def test_wb_route_a_pinned_verifies_the_real_target_not_a_placeholder(monkeypatch):
    """Review I4: once channelmixerrgb.temperature is pinned, the generator
    must build a real Expect from the check-map dict (M1 on
    channelmixerrgb.temperature, the derived sidecar keys, temperature.preset
    unchanged) rather than falling through to the ordinary
    temperature.temperature checks (the write never touches that key once
    pinned) or a permissive `Expect("edit", [])` placeholder. Nudge always
    refuses, pinned or not -- darkroom.nudge never reroutes."""
    monkeypatch.setattr(checkmap, "route_a_pinned", lambda cmap: True)
    c = _case("cov-temperature.temperature-olympus-set_mid")
    assert c.expect.kind == "edit"
    assert c.steps == [coverage._set(vocab.spec("temperature", "temperature"), coverage._mid(
        vocab.spec("temperature", "temperature")))]
    m1 = next(chk for chk in c.expect.checks if chk.kind == "equals")
    assert m1.target["key"] == "channelmixerrgb.temperature"
    unchanged = next(chk for chk in c.expect.checks if chk.kind == "unchanged")
    assert unchanged.target["keys"] == ["temperature.preset"]
    assert c.expect.sidecar["key"] == "channelmixerrgb.temperature"
    assert c.expect.sidecar["allowed"] == ["channelmixerrgb.x", "channelmixerrgb.y"]

    adj = _case("cov-temperature.temperature-olympus-adjust_up1")
    assert adj.expect.kind == "edit"
    chg = next(chk for chk in adj.expect.checks if chk.kind == "changed")
    assert chg.target["key"] == "channelmixerrgb.temperature"
    assert chg.sign == 0  # direction_verified is False -- no sign asserted
    # GR-1 (review round 3): the tag key is `sign_unverified`, never
    # `needs_review` -- that key is now evaluator-reactive (install's own
    # disputed-config-version tag) and would wrongly opt this real
    # `changed` check out of hard failure too.
    assert chg.tags.get("sign_unverified")

    warmer = _case("cov-phrase-warmer-olympus-bare")
    assert warmer.expect.kind == "edit"
    assert any(chk.metric == "M4" and chk.stat == "rb" for chk in warmer.expect.checks)

    nudge = _case("cov-temperature.temperature-olympus-nudge_up1")
    assert nudge.expect.kind == "typed_error"
    assert nudge.expect.codes == ["module_unavailable"]

    tint_set = _case("cov-temperature.tint-olympus-set_0.9")
    assert tint_set.expect.kind == "typed_error"  # unaffected by the pin


def test_c3_unpinned_phrase_families_expect_edit_but_accept_the_honest_refusal():
    """Row 523 (night-2 triage C3, decision row 523, 2026-09-28): 14 phrases
    move a param whose `vocab.status` is still `candidate` (crop, rotate,
    sharpen, denoise, grain, vignette-corners) -- `moves.py::_refusal`
    refuses them honestly, `not_pinned`, on every body today. The generator
    used to demand a plain edit, so the honest refusal scored
    `wrong_refusal`/`failed` (236 coverage fails, evidence
    cov-phrase-crop_16x9-olympus-bare, cov-phrase-sharpen_more-olympus-bare,
    cov-p4-rotate_left-olympus). Pratik's ruling: score the refusal as a
    pass; a pinned edit of the same family must still expect the edit, so
    the fix only ADDS `accept_refusal` to the existing `Expect`, it never
    drops the checks -- verified here by keeping `expect.kind == "edit"`/
    `"property"` and non-empty checks alongside the new meta flag."""
    cases = coverage.generate()
    by_id = {c.id: c for c in cases}
    candidate_bare = {
        "cov-phrase-crop_16x9-olympus-bare": "edit",
        "cov-phrase-crop_square-sony_a7-bare": "edit",
        "cov-phrase-rotate_left-olympus-bare": "edit",
        "cov-phrase-rotate_right-sony_a7-bare": "edit",
        "cov-phrase-sharpen_more-olympus-bare": "edit",
        "cov-phrase-denoise_more-sony_a7-bare": "edit",
        "cov-phrase-more_grain-olympus-bare": "edit",
        "cov-phrase-darker_corners-sony_a7-bare": "edit",
    }
    for cid, kind in candidate_bare.items():
        c = by_id[cid]
        assert c.expect.kind == kind, cid
        assert c.meta.get("accept_refusal") is True, cid
    # A pinned edit of the same family is untouched -- no accept_refusal.
    brighter = by_id["cov-phrase-brighter-olympus-bare"]
    assert brighter.meta.get("accept_refusal") is not True
    assert brighter.expect.checks  # still a real edit expectation

    # The P4 monotonic property case for the same 14 keys never observes a
    # value (every "say" in its sequence honestly refuses first) -- it must
    # carry accept_refusal too, or its own checks can only ever fail.
    props = {c.id: c for c in coverage.property_cases("olympus")}
    p4 = props["cov-p4-rotate_left-olympus"]
    assert p4.expect.kind == "property"
    assert p4.meta.get("accept_refusal") is True
    p4_live = props["cov-p4-brighter-olympus"]
    assert p4_live.meta.get("accept_refusal") is not True
