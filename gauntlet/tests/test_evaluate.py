from gauntlet.adapter import CallRecord, Observation
from gauntlet.case import Case, Check, Expect, Step
from gauntlet.evaluate import FAIL, OUTCOMES, POINTS, Policy, canonical_outcome, evaluate

POLICY = Policy(
    refusal_codes=frozenset({"usage", "value_unknown", "not_pinned"}),
    transient_codes=frozenset({"gui_not_ready", "bridge_timeout", "commit_timeout"}),
    refusal_markers=("can't", "cannot", "not able"),
    claim_markers=(r"\bdone\b", r"\bi've\b"),
)


def _ok(data=None, **kw):
    return {"ok": True, "outcome": "ok", "data": data or {}, **kw}


def _err(code, fix="try x instead"):
    return {"ok": False, "outcome": f"error:{code}", "error": {"code": code, "fix": fix,
                                                                "message": "no"}}


def _snap(fp, **params):
    return {"fingerprint": fp, "history_end": 1, "params": params}


def _edit_case(checks=None, sidecar=None, kind="edit", mode="tool", steps=None, meta=None):
    return Case(id="c", layer="coverage", tier="P0", mode=mode,
                steps=steps or [Step("darkroom_set", {}, budget_s=3.5, write=True)],
                expect=Expect(kind, checks=checks or [], sidecar=sidecar),
                meta=meta or {})


M1_SET = Check("M1", kind="equals", target={"key": "exposure.exposure", "value": 1.0,
                                            "unit": "EV"})
SIDECAR = {"changed": True, "key": "exposure.exposure", "units": {"exposure.exposure": "EV"}}


def _obs(env, before, after, wall=1.0, **kw):
    return Observation(calls=[CallRecord("darkroom_set", {}, env, wall_s=wall, write=True)],
                       before=before, after=after, **kw)


def test_verified_set():
    v = evaluate(_edit_case([M1_SET], SIDECAR),
                 _obs(_ok(), _snap("a", **{"exposure.exposure": 0.7}),
                      _snap("b", **{"exposure.exposure": 1.0})), POLICY)
    assert (v.score, v.outcome) == ("pass", "verified")


def test_silent_failure_a_sidecar_unchanged():
    v = evaluate(_edit_case([M1_SET], SIDECAR),
                 _obs(_ok(), _snap("a", **{"exposure.exposure": 0.7}),
                      _snap("a", **{"exposure.exposure": 0.7})), POLICY)
    assert v.outcome == "silent_failure" and v.silent_failure.startswith("a:")
    assert POINTS[v.score] == 0.0


def test_silent_failure_b_wrong_param_moved():
    v = evaluate(_edit_case([M1_SET], SIDECAR),
                 _obs(_ok(), _snap("a", **{"exposure.exposure": 0.7, "sigmoid.contrast": 1.5}),
                      _snap("b", **{"exposure.exposure": 0.7, "sigmoid.contrast": 1.8})), POLICY)
    assert v.silent_failure.startswith("b:")


def test_silent_failure_c_stat_wrong_direction():
    chk = Check("M2", stat="L", region="frame", sign=1)
    obs = _obs(_ok(), _snap("a"), _snap("b"),
               stats_before={"frame": {"L": 50.0}}, stats_after={"frame": {"L": 45.0}})
    v = evaluate(_edit_case([chk], {"changed": True}), obs, POLICY)
    assert v.silent_failure.startswith("c:")


def test_below_floor_is_silent_c_not_partial():
    chk = Check("M2", stat="L", region="frame", sign=1)
    obs = _obs(_ok(), _snap("a"), _snap("b"),
               stats_before={"frame": {"L": 50.0}}, stats_after={"frame": {"L": 50.1}},
               floors={"L": 0.5})
    assert evaluate(_edit_case([chk], {"changed": True}), obs, POLICY).outcome == "silent_failure"


def _mtl(module=None, param=None, applied=None, cap=None, moves=None):
    """A `moved_to_limit` envelope. With `module`/`param`, a single-adjust
    claim (`AdjustResult`'s own top-level module/param/applied/cap); with
    `moves`, a `SayResult`-shaped `data.moved_to_limit` list of such claims.
    With neither, the outcome is claimed but nothing about it is parseable
    -- review round 1 probe P1's shape (the claim still needs verifying,
    it just never can)."""
    data: dict = {}
    if moves is not None:
        data["moved_to_limit"] = moves
    elif module:
        data.update(module=module, param=param, applied=applied, cap=cap)
    return {"ok": False, "outcome": "moved_to_limit", "data": data}


#: Review round 1 (I1): `moved_to_limit` is a CLAIM, exactly like a plain
#: `ok` -- it must go through rules (a)-(f), and only scores
#: `correct_refusal` when the after snapshot actually verifies the claimed
#: module.param landed at its claimed cap, AND the failing below-floor
#: check is the one tagged (`Check.tags["clamp_param"]`) to that same
#: param. Probe ids below match `scratchpad/n1rev_probe.py`.

def test_p1_moved_to_limit_claim_xmp_unchanged_no_sidecar_is_silent_failure():
    """Probe P1: the claim is unverifiable (nothing in the envelope names a
    module/param/cap) and the XMP did not move at all. Before this fix,
    the unverified claim skipped rules (a)-(f) entirely (`reported` was
    False) and fell through to a bare 'failed'; it must be silent_failure,
    same as a plain `ok` that changed nothing would be."""
    chk = Check("M2", stat="L", region="frame", sign=1)
    obs = _obs(_mtl(), _snap("a", **{"exposure.exposure": 0.7}),
               _snap("a", **{"exposure.exposure": 0.7}),
               stats_before={"frame": {"L": 50.0}}, stats_after={"frame": {"L": 50.1}},
               floors={"L": 0.5})
    v = evaluate(_edit_case([chk], {"changed": True}), obs, POLICY)
    assert v.outcome == "silent_failure"


def test_p4_moved_to_limit_claim_applied_does_not_match_cap_is_silent_failure():
    """Probe P4: claims exposure capped at 4.0, but the after-XMP shows
    exposure only reached 1.0 -- the claim does not verify, so the
    below-floor exception must not apply."""
    chk = Check("M2", stat="L", region="frame", sign=1,
                tags={"clamp_param": "exposure.exposure"})
    obs = _obs(_mtl("exposure", "exposure", applied=4.0, cap=4.0),
               _snap("a", **{"exposure.exposure": 0.7}),
               _snap("b", **{"exposure.exposure": 1.0}),
               stats_before={"frame": {"L": 50.0}}, stats_after={"frame": {"L": 50.1}},
               floors={"L": 0.5})
    v = evaluate(_edit_case([chk], {"changed": True}), obs, POLICY)
    assert v.outcome == "silent_failure" and v.silent_failure.startswith("c:")


def test_moved_to_limit_verified_clamp_below_floor_is_correct_refusal_residual_clamp():
    """A clamp claim the after snapshot actually verifies (exposure landed
    exactly at its claimed cap, 4.0, changed from 0.7), whose only failing
    check is the one tagged to that same param, is `correct_refusal`,
    `residual: clamp` -- this is the shape row 471 describes, built
    synthetically (I4: the real night-1 D-bare-step rows never hit this
    path at all -- see I2/test_layer_coverage's touch-floor rows, which
    are plain `ok` and score `imperceptible_step` instead)."""
    chk = Check("M2", stat="L", region="frame", sign=1,
                tags={"clamp_param": "exposure.exposure"})
    obs = _obs(_mtl("exposure", "exposure", applied=4.0, cap=4.0),
               _snap("a", **{"exposure.exposure": 0.7}),
               _snap("b", **{"exposure.exposure": 4.0}),
               stats_before={"frame": {"L": 50.0}}, stats_after={"frame": {"L": 50.1}},
               floors={"L": 0.5})
    v = evaluate(_edit_case([chk], {"changed": True}), obs, POLICY)
    assert (v.score, v.outcome) == ("pass", "correct_refusal")
    assert v.notes == ["residual: clamp"]


def test_moved_to_limit_below_floor_check_tagged_to_a_different_unclamped_move_is_silent():
    """A phrase with one clamped move (colorbalancergb.shadows_chroma,
    verified at its cap) and one plain move (shadows_hue) -- the
    below-floor stat check is tagged to the PLAIN move's param, so the
    verified clamp on the OTHER param must not excuse it."""
    chk = Check("M2", stat="L", region="frame", sign=1,
                tags={"clamp_param": "colorbalancergb.shadows_hue"})
    obs = _obs(
        _mtl(moves=[{"module": "colorbalancergb", "param": "shadows_chroma",
                     "applied": 0.3, "cap": 0.3}]),
        _snap("a", **{"colorbalancergb.shadows_chroma": 0.2,
                      "colorbalancergb.shadows_hue": 10.0}),
        _snap("b", **{"colorbalancergb.shadows_chroma": 0.3,
                      "colorbalancergb.shadows_hue": 15.0}),
        stats_before={"frame": {"L": 50.0}}, stats_after={"frame": {"L": 50.1}},
        floors={"L": 0.5})
    v = evaluate(_edit_case([chk], {"changed": True}), obs, POLICY)
    assert v.outcome == "silent_failure" and v.silent_failure.startswith("c:")


def test_p6_moved_to_limit_wrong_direction_is_silent_failure():
    """Probe P6: a `moved_to_limit` outcome only ever excuses "below noise
    floor" -- a check that moved the WRONG way is rule (c)'s honest
    silent failure, clamp or not (the plain-ok control, P7, scores the
    same way -- see test_silent_failure_c_stat_wrong_direction above)."""
    chk = Check("M2", stat="L", region="frame", sign=1)
    obs = _obs(_mtl(), _snap("a"), _snap("b"),
               stats_before={"frame": {"L": 50.0}}, stats_after={"frame": {"L": 45.0}},
               floors={"L": 0.5})
    v = evaluate(_edit_case([chk], {"changed": True}), obs, POLICY)
    assert v.outcome == "silent_failure" and v.silent_failure.startswith("c:")


def _plain_ok_move(module, param, previous, delta, unit=""):
    applied = previous + delta
    return {"ok": True, "outcome": "ok",
            "data": {"module": module, "param": param, "previous": previous, "delta": delta,
                     "applied": applied, "requested": applied, "unit": unit,
                     "clamped": False, "noop": False}}


def test_imperceptible_step_real_recorded_shape_is_its_own_outcome():
    """Row 475 (I2 ruling, provisional). Real shape from the night-1
    gauntlet recording (~/Pictures/ScouterScratch/gauntlet/night1-20260926/
    cases/cov-phrase-more_chroma-sony_a7-bare.json): 'more chroma' moves
    colorbalancergb.global_chroma 0.0 -> 0.05 (a plain `ok`, `clamped:
    false`, requested == applied -- the move landed in full), and M5 C
    measures +0.407 against a floor of 0.5 (gauntlet/metrics.FLOOR_MIN).
    I2 found all 125 recorded D-bare-step-below-perceptual-floor rows have
    this exact shape (0 are `moved_to_limit`) -- this must not be
    `silent_failure` (the product claimed nothing false) nor
    `correct_refusal` (nothing was refused -- it moved): its own outcome,
    `imperceptible_step`, tagged `residual: step-below-floor`."""
    chk = Check("M5", stat="C", region="frame", sign=1)
    obs = _obs(_plain_ok_move("colorbalancergb", "global_chroma", 0.0, 0.05, unit="%"),
               _snap("a", **{"colorbalancergb.global_chroma": 0.0}),
               _snap("b", **{"colorbalancergb.global_chroma": 0.05}),
               stats_before={"frame": {"C": 40.0}}, stats_after={"frame": {"C": 40.407}},
               floors={"C": 0.5})
    v = evaluate(_edit_case([chk], {"changed": True}), obs, POLICY)
    assert (v.score, v.outcome) == ("partial", "imperceptible_step")
    assert v.notes == ["residual: step-below-floor"]
    assert v.silent_failure is None


#: Review round 2 (R1, high, blocking): `_writes_landed_as_requested` used to
#: trust the envelope's own `applied` field against its own `requested`
#: field -- both the product's word, never checked against the XMP. Probes
#: below match `scratchpad/n1rev_probe2.py` (Q1/Q4/Q5/Q7 -- Q2/Q3/Q6/Q8 were
#: already correct and are covered by other tests above).

def _claims_ok(applied, requested, unit="%"):
    return {"ok": True, "outcome": "ok",
            "data": {"module": "colorbalancergb", "param": "global_chroma",
                     "applied": applied, "requested": requested, "unit": unit,
                     "clamped": False, "noop": False}}


def test_q1_claim_matches_but_xmp_is_identical_is_silent_failure():
    """Probe Q1: the envelope claims applied == requested == 0.05 (a
    consistent, plausible-looking claim), but the after snapshot shows the
    param UNCHANGED from before -- nothing landed at all. Must be
    silent_failure, not imperceptible_step: `_writes_landed_as_requested`
    reads the XMP, not the envelope's own numbers."""
    chk = Check("M5", stat="C", region="frame", sign=1)
    obs = _obs(_claims_ok(0.05, 0.05), _snap("a", **{"colorbalancergb.global_chroma": 0.0}),
               _snap("a", **{"colorbalancergb.global_chroma": 0.0}),
               stats_before={"frame": {"C": 40.0}}, stats_after={"frame": {"C": 40.3}},
               floors={"C": 0.5})
    v = evaluate(_edit_case([chk], None), obs, POLICY)
    assert v.outcome == "silent_failure" and v.silent_failure.startswith("c:")


def test_q4_claim_matches_but_xmp_shows_a_short_move_is_silent_failure():
    """Probe Q4: claims applied == requested == 0.05, but the after
    snapshot shows the param only reached 0.01 -- a genuinely short move
    wearing a full-step claim. Must be silent_failure."""
    chk = Check("M5", stat="C", region="frame", sign=1)
    obs = _obs(_claims_ok(0.05, 0.05), _snap("a", **{"colorbalancergb.global_chroma": 0.0}),
               _snap("b", **{"colorbalancergb.global_chroma": 0.01}),
               stats_before={"frame": {"C": 40.0}}, stats_after={"frame": {"C": 40.3}},
               floors={"C": 0.5})
    v = evaluate(_edit_case([chk], {"changed": True}), obs, POLICY)
    assert v.outcome == "silent_failure" and v.silent_failure.startswith("c:")


def test_q5_short_move_with_a_passing_m1_check_is_still_silent_failure():
    """Probe Q5: same short move as Q4, plus an M1 'changed' check that
    PASSES (0.01 != 0 is a real, if short, change) -- the passing M1
    check must not distract from rule (c)'s below-floor stat failure."""
    chk = Check("M5", stat="C", region="frame", sign=1)
    m1 = Check("M1", kind="changed", sign=1,
               target={"key": "colorbalancergb.global_chroma", "unit": "%"})
    obs = _obs(_claims_ok(0.05, 0.05), _snap("a", **{"colorbalancergb.global_chroma": 0.0}),
               _snap("b", **{"colorbalancergb.global_chroma": 0.01}),
               stats_before={"frame": {"C": 40.0}}, stats_after={"frame": {"C": 40.3}},
               floors={"C": 0.5})
    v = evaluate(_edit_case([m1, chk], {"changed": True}), obs, POLICY)
    assert v.outcome == "silent_failure" and v.silent_failure.startswith("c:")


def test_q7_workflow_kind_genuine_step_below_floor_is_silent_failure_not_unlabelled():
    """Review round 2 (R2, low): a `workflow`-kind case is never scored by
    `_imperceptible_step_residual` (edit-only), so the rule (c) exemption
    must not apply to it either -- a genuine, XMP-verified, merely-too-
    small-to-measure move on a workflow case keeps its ordinary silent
    scoring, not an unlabelled 'failed' from a half-applied exemption."""
    chk = Check("M5", stat="C", region="frame", sign=1)
    case = _edit_case([chk], {"changed": True}, kind="workflow",
                      steps=[Step("darkroom_say", {}, budget_s=3.5, write=True)])
    obs = _obs(_claims_ok(0.05, 0.05), _snap("a", **{"colorbalancergb.global_chroma": 0.0}),
               _snap("b", **{"colorbalancergb.global_chroma": 0.05}),
               stats_before={"frame": {"C": 40.0}}, stats_after={"frame": {"C": 40.3}},
               floors={"C": 0.5})
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "silent_failure" and v.silent_failure.startswith("c:")


def test_imperceptible_step_does_not_excuse_a_short_move():
    """Control: if the move's own claimed `applied` falls short of its own
    `requested` (a genuine silent failure, not an imperceptible one), the
    exception must not apply -- still silent_failure."""
    chk = Check("M5", stat="C", region="frame", sign=1)
    move = _plain_ok_move("colorbalancergb", "global_chroma", 0.0, 0.05, unit="%")
    move["data"]["applied"] = 0.01  # claims requested 0.05 but only landed 0.01
    obs = _obs(move, _snap("a", **{"colorbalancergb.global_chroma": 0.0}),
               _snap("b", **{"colorbalancergb.global_chroma": 0.01}),
               stats_before={"frame": {"C": 40.0}}, stats_after={"frame": {"C": 40.05}},
               floors={"C": 0.5})
    v = evaluate(_edit_case([chk], {"changed": True}), obs, POLICY)
    assert v.outcome == "silent_failure" and v.silent_failure.startswith("c:")


def test_silent_failure_d_bulk_counts():
    obs = _obs(_ok(), _snap("a"), _snap("b"), extra={"reported_n": 7, "changed_n": 6,
                                                     "verified_n": 7})
    v = evaluate(_edit_case([], {"changed": True}, kind="workflow"), obs, POLICY)
    assert v.silent_failure.startswith("d:")


def test_silent_failure_e_undo_not_restored():
    chk = Check("M1", kind="restored", target={"keys": ["exposure.exposure"], "unit": "EV"})
    obs = Observation(
        calls=[CallRecord("darkroom_undo_last", {}, _ok(), write=True)],
        before=_snap("a", **{"exposure.exposure": 0.7}),
        after=_snap("b", **{"exposure.exposure": 1.0}))
    v = evaluate(_edit_case([chk], None, kind="workflow",
                            steps=[Step("darkroom_undo_last", write=True)]), obs, POLICY)
    assert v.silent_failure.startswith("e:")


def test_undo_own_refusal_is_not_silent_e():
    """Row 459 (COV-undo-first-introduction, night-1): the case sets a module
    that was absent before (an ok write), then tries to undo it and the
    product HONESTLY refuses (the previous value was never readable from the
    XMP -- untyped `error`, ok=False on the undo call itself). Rule (e) must
    look at the undo call's own ok/refusal, not just "was any write ok" --
    an earlier ok `set` must not turn an honest refusal into a silent
    failure. The case should score `failed` (honest), never `silent_failure`.
    """
    chk = Check("M1", kind="restored",
                target={"keys": ["colorbalancergb.highlights_chroma"], "unit": "%"})
    obs = Observation(
        calls=[
            CallRecord("darkroom_set", {}, _ok(), write=True),
            CallRecord("darkroom_undo_last", {},
                       _err("error", fix="press Ctrl-Z in darktable, or set the value "
                                          "you want explicitly"), write=True),
        ],
        before=_snap("a"),
        after=_snap("b", **{"colorbalancergb.highlights_chroma": 12.0}))
    case = _edit_case([chk], None, kind="workflow",
                       steps=[Step("darkroom_set", write=True),
                              Step("darkroom_undo_last", write=True)])
    v = evaluate(case, obs, POLICY)
    assert v.outcome != "silent_failure", v.silent_failure
    assert v.outcome == "failed", (v.outcome, v.silent_failure)


def test_silent_failure_f_noop_but_changed():
    env = _ok({"noop": True})
    v = evaluate(_edit_case([], {"changed": False}),
                 _obs(env, _snap("a"), _snap("b")), POLICY)
    assert v.silent_failure.startswith("f:")


def test_wrong_refusal_of_an_in_vocabulary_edit():
    v = evaluate(_edit_case([M1_SET], SIDECAR),
                 _obs(_err("usage"), _snap("a"), _snap("a")), POLICY)
    assert v.outcome == "wrong_refusal"


def test_honest_tool_refusal_passes():
    case = _edit_case(kind="refuse", steps=[Step("darkroom_say", write=True)])
    v = evaluate(case, _obs(_err("usage"), _snap("a"), _snap("a")), POLICY)
    assert (v.score, v.outcome) == ("pass", "correct_refusal")


def test_refusal_with_half_applied_edit_fails():
    case = _edit_case(kind="refuse", steps=[Step("darkroom_say", write=True)])
    v = evaluate(case, _obs(_err("usage"), _snap("a"), _snap("b")), POLICY)
    assert v.score == "fail"


def test_refusal_code_outside_the_set_fails_and_transient_blocks():
    case = _edit_case(kind="refuse", steps=[Step("darkroom_say", write=True)])
    assert evaluate(case, _obs(_err("error"), _snap("a"), _snap("a")), POLICY).outcome == "failed"
    assert evaluate(case, _obs(_err("gui_not_ready"), _snap("a"), _snap("a")),
                    POLICY).outcome == "blocked"


def test_agent_refusal_rules():
    case = _edit_case(kind="refuse", mode="agent", steps=[])
    base = dict(calls=[], before=_snap("a"), after=_snap("a"))
    ok = evaluate(case, Observation(agent_text="I can't do that here.", **base), POLICY)
    assert ok.outcome == "correct_refusal"
    both = evaluate(case, Observation(agent_text="I can't, but done.", **base), POLICY)
    assert both.outcome == "needs_review" and both.score == "partial"
    neither = evaluate(case, Observation(agent_text="Sure.", **base), POLICY)
    assert neither.outcome == "needs_review"
    wrote = evaluate(case, Observation(agent_text="I can't.", calls=[], before=_snap("a"),
                                       after=_snap("b")), POLICY)
    assert wrote.outcome == "unexpected_edit"


def test_agent_claim_without_change_is_silent():
    case = _edit_case([], {"changed": True}, mode="agent", steps=[])
    v = evaluate(case, Observation(agent_text="Done, I've brightened it.", before=_snap("a"),
                                   after=_snap("a")), POLICY)
    assert v.outcome == "silent_failure"


def test_typed_error_expected():
    case = _edit_case(kind="typed_error", steps=[Step("darkroom_set", write=True)])
    case.expect.codes = ["value_unknown"]
    assert evaluate(case, _obs(_err("value_unknown"), _snap("a"), _snap("a")),
                    POLICY).outcome == "verified"
    assert evaluate(case, _obs(_ok(), _snap("a"), _snap("b")), POLICY).outcome == "failed"


def test_crash_and_block_and_heal():
    case = _edit_case([M1_SET], SIDECAR)
    assert evaluate(case, Observation(crash_reports=["x.ips"]), POLICY).outcome == "crash"
    assert evaluate(case, Observation(blocked="hang_twice"), POLICY).outcome == "failed_hang"
    assert evaluate(case, Observation(blocked="launch"), POLICY).outcome == "blocked"
    healed = _obs(_ok(), _snap("a", **{"exposure.exposure": 0.7}),
                  _snap("b", **{"exposure.exposure": 1.0}), healed=True)
    assert evaluate(case, healed, POLICY).outcome == "failed"


def test_over_budget_is_a_pass():
    v = evaluate(_edit_case([M1_SET], SIDECAR),
                 _obs(_ok(), _snap("a", **{"exposure.exposure": 0.7}),
                      _snap("b", **{"exposure.exposure": 1.0}), wall=9.0), POLICY)
    assert (v.score, v.outcome) == ("pass", "over_budget")


def test_collateral_is_partial():
    side = dict(SIDECAR, collateral_check=True)
    v = evaluate(_edit_case([M1_SET], side),
                 _obs(_ok(), _snap("a", **{"exposure.exposure": 0.7, "exposure.black": 0.0}),
                      _snap("b", **{"exposure.exposure": 1.0, "exposure.black": 0.05})),
                 POLICY)
    assert v.outcome == "collateral" and v.score == "partial"


def test_right_direction_wrong_magnitude_is_partial():
    chk = Check("M2", stat="ev", region="frame", sign=1, target={"value": 1.0, "tol": 0.15})
    obs = _obs(_ok(), _snap("a"), _snap("b"),
               stats_before={"frame": {"ev": 0.1}}, stats_after={"frame": {"ev": 0.141}})
    v = evaluate(_edit_case([chk], {"changed": True}), obs, POLICY)
    assert v.outcome == "partial"


def test_property_kinds():
    snaps = [{"k": 0.0}, {"k": 0.3}, {"k": 0.3}, {"k": 0.9}]
    obs = Observation(calls=[CallRecord("s", {}, _ok(), write=True)] * 3,
                      before=_snap("a"), after=_snap("b"), extra={"snapshots": snaps})
    checks = [
        Check("M1", kind="step_equal", target={"key": "k", "a": 1, "b": 2}),
        Check("M1", kind="step_ratio", target={"key": "k", "base": [0, 1], "double": [0, 3],
                                               "ratio": 3.0}),
        Check("M1", kind="monotonic", target={"key": "k", "pairs": [[0, 1], [0, 3]]}),
    ]
    case = _edit_case(checks, None, kind="property",
                      steps=[Step("s", write=True)] * 3)
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "verified", v


def test_hue_wrap_equality():
    chk = Check("M1", kind="equals", target={"key": "cb.h", "value": 10.0, "unit": "°"})
    obs = _obs(_ok(), _snap("a", **{"cb.h": 350.0}), _snap("b", **{"cb.h": 370.4 - 360}))
    assert evaluate(_edit_case([chk], {"changed": True}), obs, POLICY).outcome == "verified"


def test_legacy_alias():
    assert canonical_outcome("reported_ok_unverified") == "silent_failure"


def test_boundary_set_accepts_a_typed_refusal_but_not_a_silent_clamp():
    clamp = Check("envelope", target={"path": "data.clamped", "op": "truthy"},
                  tags={"silent_if_fail": True, "why": "clamp not reported"})
    chk = Check("M1", kind="equals", target={"key": "exposure.exposure", "value": -3.0,
                                             "unit": "EV"})
    case = _edit_case([chk, clamp], SIDECAR, meta={"accept_refusal": True})
    refused = evaluate(case, _obs(_err("usage"), _snap("a"), _snap("a")), POLICY)
    assert refused.outcome == "correct_refusal"
    silent = evaluate(case, _obs(_ok(), _snap("a", **{"exposure.exposure": 0.7}),
                                 _snap("b", **{"exposure.exposure": -3.0})), POLICY)
    assert silent.outcome == "silent_failure" and "clamp" in silent.silent_failure
    reported = evaluate(case, _obs(_ok({"clamped": True}), _snap("a", **{"exposure.exposure": 0.7}),
                                   _snap("b", **{"exposure.exposure": -3.0})), POLICY)
    assert reported.outcome == "verified"


def test_boundary_metric_landed_as_requested_needs_no_clamp_report():
    """Scouter's soft range is narrower than darktable's hard range: a
    `set` just outside the soft bound can land exactly where it was asked,
    honestly, with no clamp at all -- that must not read as a silent
    failure just because `data.clamped` is naturally false (row 2026-09-26,
    item 2: cov-colorbalancergb.contrast-olympus-set_hi_plus)."""
    chk = Check("boundary", target={"key": "exposure.exposure", "unit": "EV", "request": 4.3})
    case = _edit_case([chk], SIDECAR)
    v = evaluate(case, _obs(_ok(), _snap("a", **{"exposure.exposure": 4.0}),
                            _snap("b", **{"exposure.exposure": 4.3})), POLICY)
    assert v.outcome == "verified"


def test_boundary_metric_clamped_and_reported_passes():
    chk = Check("boundary", target={"key": "exposure.exposure", "unit": "EV", "request": 4.3,
                                    "bound": 4.0})
    case = _edit_case([chk], SIDECAR)
    v = evaluate(case, _obs(_ok({"clamped": True}), _snap("a", **{"exposure.exposure": 3.5}),
                            _snap("b", **{"exposure.exposure": 4.0})), POLICY)
    assert v.outcome == "verified"


def test_boundary_metric_clamped_but_unreported_is_a_silent_failure():
    """The real product bug this must still catch (row 2026-09-26, item 2):
    sigmoid.skew asked for 1.1, landed at the 1.0 bound, and the envelope
    never said so."""
    skew_sidecar = {"changed": True, "key": "sigmoid.skew", "units": {"sigmoid.skew": "ratio"}}
    chk = Check("boundary", target={"key": "sigmoid.skew", "unit": "ratio", "request": 1.1,
                                    "bound": 1.0}, tags={"silent_if_fail": True, "why": "clamp"})
    case = _edit_case([chk], skew_sidecar)
    v = evaluate(case, _obs(_ok(), _snap("a", **{"sigmoid.skew": 0.9}),
                            _snap("b", **{"sigmoid.skew": 1.0})), POLICY)
    assert v.outcome == "silent_failure" and "clamp" in v.silent_failure


def test_boundary_metric_delta_form_reads_the_before_snapshot():
    """A phrase's explicit `by N` (item 2's `deeper_blacks by 0.2`): the
    generator does not know the frame's starting value, so `delta`+`lo`/`hi`
    resolve the request against the case's own before-snapshot."""
    black_sidecar = {"changed": True, "key": "exposure.black", "units": {"exposure.black": "ratio"}}
    chk = Check("boundary", target={"key": "exposure.black", "unit": "ratio", "delta": 0.2,
                                    "lo": -0.1, "hi": 0.1},
               tags={"silent_if_fail": True, "why": "clamp"})
    case = _edit_case([chk], black_sidecar)
    # honestly clamped at the bound and reported
    v = evaluate(case, _obs(_ok({"clamped": True}), _snap("a", **{"exposure.black": 0.0}),
                            _snap("b", **{"exposure.black": 0.1})), POLICY)
    assert v.outcome == "verified"
    # landed short of the bound with no clamp reported: a real silent failure
    bad = evaluate(case, _obs(_ok(), _snap("a", **{"exposure.black": 0.0}),
                              _snap("b", **{"exposure.black": 0.05})), POLICY)
    assert bad.outcome == "silent_failure"


def test_extra_and_envelope_ops():
    obs = Observation(calls=[CallRecord("t", {}, _ok({"n": 3, "k": "warmer"}))],
                      extra={"offline_ok": True, "count": 5})
    checks = [Check("extra", target={"path": "offline_ok", "op": "truthy"}),
              Check("extra", target={"path": "count", "op": "gte", "value": 5}),
              Check("envelope", target={"path": "data.k", "op": "in", "value": ["warmer"]}),
              Check("envelope", target={"path": "data.k", "op": "regex", "value": "^warm"}),
              Check("envelope", target={"path": "data.n", "op": "abs_within", "value": 3.2,
                                        "tol": 0.5})]
    case = _edit_case(checks, None, kind="workflow", steps=[Step("t")])
    assert evaluate(case, obs, POLICY).outcome == "verified"


# ---- fix round 1 regressions (t5-r1-findings.md, probes from probe5.py)


def test_c1_sidecar_expectation_is_enforced_without_checks():
    case = _edit_case([], {"changed": True})
    assert evaluate(case, _obs(_err("internal"), _snap("a"), _snap("a")),
                    POLICY).outcome == "failed"
    moved = evaluate(case, _obs(_ok(), _snap("a"), _snap("b")), POLICY)
    assert (moved.score, moved.outcome) == ("pass", "verified")
    assert evaluate(case, _obs(_ok(), _snap("a"), _snap("a")), POLICY).score == "fail"
    assert evaluate(case, _obs(_err("bridge_timeout"), _snap("a"), _snap("a")),
                    POLICY).outcome == "blocked"
    assert evaluate(case, Observation(before=_snap("a"), after=_snap("a")),
                    POLICY).outcome == "failed"
    agent = _edit_case([], {"changed": True}, mode="agent", steps=[])
    v = evaluate(agent, Observation(agent_text="Brightened it by a stop.", before=_snap("a"),
                                    after=_snap("a")), POLICY)
    assert v.score == "fail"


def test_c1_edit_without_checks_or_sidecar_needs_review():
    v = evaluate(_edit_case([], None), _obs(_ok(), _snap("a"), _snap("b")), POLICY)
    assert (v.score, v.outcome) == ("partial", "needs_review")
    assert "unverifiable" in v.notes[0]


def test_c2_agent_refuse_claim_only_is_silent():
    case = _edit_case(kind="refuse", mode="agent", steps=[])
    v = evaluate(case, Observation(agent_text="Done, I've removed the person.",
                                   before=_snap("a"), after=_snap("a")), POLICY)
    assert v.outcome == "silent_failure"


def test_i1_substitute_that_fails_its_checks_fails():
    chk = Check("M1", kind="equals", target={"key": "k", "value": 5.0})
    case = _edit_case([chk], kind="refuse", mode="agent", steps=[], meta={"substitute_ok": True})
    v = evaluate(case, Observation(agent_text="I can't do that.", before=_snap("a", k=0.0),
                                   after=_snap("b", k=1.0)), POLICY)
    assert v.score == "fail"
    good = evaluate(case, Observation(agent_text="I can't do that.", before=_snap("a", k=0.0),
                                      after=_snap("b", k=5.0)), POLICY)
    assert good.outcome == "correct_refusal"


def test_i2_tool_refuse_ok_without_change_is_silent():
    case = _edit_case(kind="refuse", steps=[Step("darkroom_say", write=True)])
    v = evaluate(case, _obs(_ok(), _snap("a"), _snap("a")), POLICY)
    assert v.outcome == "silent_failure"
    wrote = evaluate(case, _obs(_ok(), _snap("a"), _snap("b")), POLICY)
    assert wrote.outcome == "unexpected_edit"


def test_i3_frac_floor_is_scaled():
    chk = Check("M5", stat="C", region="frame", sign=1, kind="frac",
                target={"value": 0.08, "tol": 0.08})
    obs = _obs(_ok(), _snap("a"), _snap("b"), stats_before={"frame": {"C": 20.0}},
               stats_after={"frame": {"C": 21.6}})
    assert evaluate(_edit_case([chk], {"changed": True}), obs, POLICY).outcome == "verified"


def test_i4_non_numeric_params_compare_exactly():
    chk = Check("M1", kind="restored", target={"keys": ["m.mode"]})
    obs = Observation(calls=[CallRecord("darkroom_undo_last", {}, _ok(), write=True)],
                      before=_snap("a", **{"m.mode": "A"}), after=_snap("b", **{"m.mode": "B"}))
    case = _edit_case([chk], None, kind="workflow", steps=[Step("darkroom_undo_last", write=True)])
    assert evaluate(case, obs, POLICY).outcome == "silent_failure"
    same = Observation(calls=obs.calls, before=_snap("a", **{"m.mode": "A"}),
                       after=_snap("b", **{"m.mode": "A"}))
    assert evaluate(case, same, POLICY).outcome == "verified"


def test_i5_missing_measurements_fail():
    undo = [Step("darkroom_undo_last", write=True)]
    base = dict(calls=[CallRecord("darkroom_undo_last", {}, _ok(), write=True)],
                before=_snap("a"), after=_snap("b"))
    for chk in (Check("M9"), Check("file", target={"name": "out.jpg"}),
                Check("text", target={"regex": "error", "present": False})):
        v = evaluate(_edit_case([chk], None, kind="workflow", steps=undo),
                     Observation(**base), POLICY)
        assert v.score == "fail" and v.checks[0].note == "unmeasured", chk


def test_i6_accept_refusal_needs_a_named_fix():
    chk = Check("M1", kind="equals", target={"key": "k", "value": 1.0})
    case = _edit_case([chk], {"changed": True, "key": "k"}, meta={"accept_refusal": True})
    env = {"ok": False, "error": {"code": "usage", "fix": "", "message": ""}}
    assert evaluate(case, _obs(env, _snap("a"), _snap("a")), POLICY).score == "fail"


def test_i7_typed_error_after_half_applied_write_fails():
    case = Case(id="c", layer="x", tier="P0", mode="tool",
                steps=[Step("s", write=True), Step("s", write=True)],
                expect=Expect("typed_error", codes=["value_unknown"]))
    obs = Observation(calls=[CallRecord("s", {}, _ok(), write=True),
                             CallRecord("s", {}, _err("value_unknown"), write=True)],
                      before=_snap("a", k=0.0), after=_snap("b", k=1.0))
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "failed" and "half-applied" in v.notes[0]


def test_i8_a_uses_m1_key_and_b_uses_reported_key():
    chk = Check("M1", kind="equals", target={"key": "k", "value": 1.0})
    v = evaluate(_edit_case([chk], None), _obs(_ok(), _snap("a", k=0.0), _snap("a", k=0.0)),
                 POLICY)
    assert v.outcome == "silent_failure" and v.silent_failure.startswith("a:")
    env = _ok({"module": "exposure", "param": "exposure"})
    wrong = evaluate(_edit_case([], {"changed": True, "key": "sigmoid.contrast"}),
                     _obs(env, _snap("a", **{"exposure.exposure": 0.7, "sigmoid.contrast": 1.5}),
                          _snap("b", **{"exposure.exposure": 0.7, "sigmoid.contrast": 1.8})),
                     POLICY)
    assert wrong.silent_failure.startswith("b: reported exposure.exposure")


def _say_env(moves, key="warmer"):
    return _ok({"key": key, "phrase": key, "moves": moves})


def _move(module, param, **kw):
    return {"module": module, "param": param, "outcome": "ok", "committed": True,
            "noop": False, **kw}


WARM_BEFORE = {"temperature.temperature": 4924.6, "temperature.coeff_r": 1.914,
               "temperature.coeff_b": 1.820, "temperature.preset": 4}
WARM_AFTER = {"temperature.temperature": 5123.7, "temperature.coeff_r": 1.959,
              "temperature.coeff_b": 1.768, "temperature.preset": 2}


def test_smoke_s1a_b_uses_the_moves_not_the_phrase_key():
    """Smoke S1-A: darkroom_say's data.key is the PHRASE key ("warmer"), not a
    module.param; rule (b) must read the moves' module.param instead."""
    chk = Check("M1", kind="changed", target={"key": "temperature.temperature", "unit": "K"})
    case = _edit_case([chk], {"changed": True},
                      steps=[Step("darkroom_say", {"phrase": "warmer"}, write=True)])
    env = _say_env([_move("temperature", "temperature")])
    v = evaluate(case, _obs(env, _snap("a", **WARM_BEFORE), _snap("b", **WARM_AFTER)), POLICY)
    assert v.outcome == "verified", v.silent_failure
    # a phrase whose move moved a different param is still a (b), named by module.param
    wrong = _say_env([_move("exposure", "exposure")])
    v = evaluate(_edit_case([], {"changed": True}),
                 _obs(wrong, _snap("a", **WARM_BEFORE), _snap("b", **WARM_AFTER)), POLICY)
    assert v.silent_failure.startswith("b: reported exposure.exposure but"), v.silent_failure


def test_smoke_s1a_multi_move_phrase_and_keyless_envelope():
    both = _say_env([_move("exposure", "exposure"), _move("temperature", "temperature")])
    v = evaluate(_edit_case([], {"changed": True}),
                 _obs(both, _snap("a", **WARM_BEFORE), _snap("b", **WARM_AFTER)), POLICY)
    assert v.outcome == "verified", v.silent_failure
    # a phrase key with no moves names no param: (b) is skipped, never compared to "warmer"
    bare = _ok({"key": "warmer"})
    v = evaluate(_edit_case([], {"changed": True}),
                 _obs(bare, _snap("a", **WARM_BEFORE), _snap("b", **WARM_AFTER)), POLICY)
    assert v.outcome == "verified", v.silent_failure
    # noop moves are not reports of a change; only the non-noop move counts
    mixed = _say_env([_move("temperature", "temperature", noop=True),
                      _move("exposure", "exposure")])
    v = evaluate(_edit_case([], {"changed": True}),
                 _obs(mixed, _snap("a", **WARM_BEFORE), _snap("b", **WARM_AFTER)), POLICY)
    assert v.silent_failure.startswith("b: reported exposure.exposure but"), v.silent_failure


def test_smoke_r1_i1_all_noop_say_that_changed_the_sidecar_is_silent():
    """Review I1: every move answered noop (e.g. clamped) yet params changed."""
    allnoop = _say_env([_move("temperature", "temperature", noop=True)])
    v = evaluate(_edit_case([], {"changed": True}),
                 _obs(allnoop, _snap("a", **WARM_BEFORE), _snap("b", **WARM_AFTER)), POLICY)
    assert v.outcome == "silent_failure", v.outcome
    assert v.silent_failure.startswith("f: every move answered noop but the sidecar changed")
    # same fingerprint but a param changed still counts
    v = evaluate(_edit_case([], None),
                 _obs(allnoop, _snap("a", **WARM_BEFORE), _snap("a", **WARM_AFTER)), POLICY)
    assert v.silent_failure.startswith("f: every move answered noop"), v.silent_failure
    # an honest all-noop say that changed nothing is not a silent failure
    v = evaluate(_edit_case([], None),
                 _obs(allnoop, _snap("a", **WARM_BEFORE), _snap("a", **WARM_BEFORE)), POLICY)
    assert v.silent_failure is None


def test_smoke_r1_a_is_per_reported_key():
    """Review minor (a): a committed non-noop move whose key did not change is an (a)
    when the case expects a change, even if another reported key did change."""
    two = _say_env([_move("temperature", "temperature"), _move("exposure", "exposure")])
    before = {**WARM_BEFORE, "exposure.exposure": 0.7}
    after = {**WARM_AFTER, "exposure.exposure": 0.7}
    v = evaluate(_edit_case([], {"changed": True}),
                 _obs(two, _snap("a", **before), _snap("b", **after)), POLICY)
    assert v.silent_failure == "a: reported ok but exposure.exposure did not change"
    # an uncommitted move is not a report of a change
    unc = _say_env([_move("temperature", "temperature"),
                    _move("exposure", "exposure", committed=False)])
    v = evaluate(_edit_case([], {"changed": True}),
                 _obs(unc, _snap("a", **before), _snap("b", **after)), POLICY)
    assert v.outcome == "verified", v.silent_failure


# ------------------------------------------------ night-1 evaluator fixes (htc-fix-evaluator)
# Shapes below are taken from real night-1 case JSONs (gauntlet/night1-20260926).


def _steps_obs(envs, snaps, *, before_meta=None, fps=("a", "b"), tools=None):
    """An observation with one write call per env and per-step snapshots
    (snaps[0] = before, snaps[i + 1] = after step i)."""
    tools = tools or ["darkroom_set"] * len(envs)
    meta = before_meta or {}
    return Observation(
        calls=[CallRecord(t, {}, e, wall_s=1.0, write=True) for t, e in zip(tools, envs)],
        before={"fingerprint": fps[0], "history_end": 1, "params": dict(snaps[0]), **meta},
        after={"fingerprint": fps[1], "history_end": 2, "params": dict(snaps[-1]), **meta},
        extra={"snapshots": [dict(s) for s in snaps]})


def _cb(**kw):
    """colorbalancergb as darktable writes it on first use: every key at 0."""
    keys = ("contrast", "global_chroma", "global_saturation", "global_vibrance",
            "highlights_chroma", "highlights_hue", "highlights_luminance", "midtones_chroma",
            "midtones_hue", "midtones_luminance", "shadows_chroma", "shadows_hue",
            "shadows_luminance")
    out = {"colorbalancergb.__enabled": 1, **{f"colorbalancergb.{k}": 0.0 for k in keys}}
    out.update({f"colorbalancergb.{k}": v for k, v in kw.items()})
    return out


CB_DEFAULTS = {k: 0.0 for k in _cb() if not k.endswith("__enabled")}
WB_KEYS = ["temperature.coeff_r", "temperature.coeff_g", "temperature.coeff_b",
           "temperature.coeff_g2", "temperature.preset"]
WB_META = {"derived": {"temperature.temperature": WB_KEYS + ["temperature.tint"],
                       "temperature.tint": WB_KEYS + ["temperature.temperature"]},
           "null_equiv": {"temperature.coeff_g2": 0.0}}


def _mv(module, param, noop=False, **data):
    return _ok({"module": module, "param": param, "noop": noop, **data})


# ---- bug 1: rule (f) attributes the change per step


def test_fix1_noop_setup_step_then_real_adjust_is_not_f():
    """cov-colorbalancergb.contrast-olympus-adjust_up1: step 0 sets the value
    already there (honest noop), step 1 adjusts it; the change is step 1's."""
    k = "colorbalancergb.contrast"
    chk = Check("M1", kind="delta", target={"delta": 0.05, "from": 1, "key": k, "step": 0.05,
                                            "tol": 0.005, "unit": "%"})
    case = _edit_case([chk], {"allowed": [], "changed": True, "key": k, "units": {k: "%"}},
                      steps=[Step("darkroom_set", write=True), Step("darkroom_adjust", write=True)])
    obs = _steps_obs([_mv("colorbalancergb", "contrast", noop=True),
                      _mv("colorbalancergb", "contrast")],
                     [_cb(), _cb(), _cb(contrast=0.05000001937150955)],
                     tools=["darkroom_set", "darkroom_adjust"])
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "verified", v.silent_failure


def test_fix1_noop_step_that_itself_changed_the_sidecar_is_still_f():
    k = "colorbalancergb.contrast"
    case = _edit_case([], {"changed": True, "key": k},
                      steps=[Step("darkroom_set", write=True), Step("darkroom_adjust", write=True)])
    obs = _steps_obs([_mv("colorbalancergb", "contrast", noop=True),
                      _mv("colorbalancergb", "contrast")],
                     [_cb(), _cb(contrast=0.02), _cb(contrast=0.07)])
    v = evaluate(case, obs, POLICY)
    assert v.silent_failure == "f: answered noop but the sidecar changed"


def test_fix1_without_snapshots_f_fires_only_when_every_write_answered_noop():
    k = "colorbalancergb.contrast"
    case = _edit_case([], {"changed": True, "key": k},
                      steps=[Step("darkroom_set", write=True), Step("darkroom_adjust", write=True)])
    obs = _steps_obs([_mv("colorbalancergb", "contrast", noop=True),
                      _mv("colorbalancergb", "contrast")], [_cb(), _cb(contrast=0.05)])
    obs.extra = {}
    assert evaluate(case, obs, POLICY).outcome == "verified"
    # a sole noop write that changed the sidecar is still flagged
    solo = _edit_case([], {"changed": False})
    obs = _steps_obs([_mv("colorbalancergb", "contrast", noop=True)],
                     [_cb(), _cb(contrast=0.05)])
    obs.extra = {}
    assert evaluate(solo, obs, POLICY).silent_failure == "f: answered noop but the sidecar changed"


def test_fix1_noop_for_a_needed_change_only_when_no_write_made_it():
    k = "colorbalancergb.contrast"
    side = {"changed": True, "key": k}
    # the sole answer is noop and nothing changed: still f
    obs = _steps_obs([_mv("colorbalancergb", "contrast", noop=True)], [_cb(), _cb()],
                     fps=("a", "a"))
    v = evaluate(_edit_case([], side), obs, POLICY)
    assert v.silent_failure == "f: answered noop for a request that needed a change"
    # a later write produced the change: not this rule
    two = _edit_case([], side, steps=[Step("darkroom_set", write=True),
                                      Step("darkroom_adjust", write=True)])
    obs = _steps_obs([_mv("colorbalancergb", "contrast", noop=True),
                      _mv("colorbalancergb", "contrast")],
                     [_cb(), _cb(), _cb(contrast=0.05)])
    assert evaluate(two, obs, POLICY).outcome == "verified"
    # ...but if the later write reported ok and changed nothing, it is still silent
    obs = _steps_obs([_mv("colorbalancergb", "contrast", noop=True),
                      _mv("colorbalancergb", "contrast")], [_cb(), _cb(), _cb()],
                     fps=("a", "a"))
    assert evaluate(two, obs, POLICY).outcome == "silent_failure"


def test_fix1_all_moves_noop_is_attributed_per_step_too():
    say_noop = _say_env([_move("temperature", "temperature", noop=True)])
    real = _mv("exposure", "exposure")
    case = _edit_case([], {"changed": True}, steps=[Step("darkroom_say", write=True),
                                                    Step("darkroom_set", write=True)])
    obs = _steps_obs([say_noop, real], [{"exposure.exposure": 0.7}, {"exposure.exposure": 0.7},
                                        {"exposure.exposure": 1.0}])
    assert evaluate(case, obs, POLICY).outcome == "verified"
    obs = _steps_obs([say_noop, real], [{"exposure.exposure": 0.7}, {"exposure.exposure": 0.9},
                                        {"exposure.exposure": 1.0}])
    v = evaluate(case, obs, POLICY)
    assert v.silent_failure.startswith("f: every move answered noop"), v.silent_failure


# ---- bug 2: "did it change?" uses a strict float epsilon, not the M1 landed tolerance


def test_fix2_one_notch_black_adjust_is_a_change():
    """cov-exposure.black-olympus-adjust_up1: -0.000244 -> 0.0 -> 0.005."""
    k = "exposure.black"
    chk = Check("M1", kind="delta", target={"delta": 0.005, "from": 1, "key": k, "step": 0.005,
                                            "tol": 0.0005, "unit": "ratio"})
    case = _edit_case([chk], {"allowed": [], "changed": True, "key": k, "units": {k: "ratio"}},
                      steps=[Step("darkroom_set", write=True), Step("darkroom_adjust", write=True)])
    obs = _steps_obs([_mv("exposure", "black"), _mv("exposure", "black")],
                     [{k: -0.000244140625}, {k: 0.0}, {k: 0.005000005941838026}])
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "verified", v.silent_failure


def test_fix2_one_degree_hue_nudge_is_a_change():
    """cov-colorbalancergb.highlights_hue-olympus-nudge_up1: 180 -> 181."""
    k = "colorbalancergb.highlights_hue"
    chk = Check("M1", kind="changed", sign=1, target={"from": 1, "key": k, "unit": "°"})
    case = _edit_case([chk], {"allowed": [], "changed": True, "key": k, "units": {k: "°"}},
                      steps=[Step("darkroom_set", write=True), Step("darkroom_nudge", write=True)])
    obs = _steps_obs([_mv("colorbalancergb", "highlights_hue"),
                      _mv("colorbalancergb", "highlights_hue")],
                     [_cb(), _cb(highlights_hue=180.0), _cb(highlights_hue=180.99998474121094)])
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "verified", (v.silent_failure, v.checks)


def test_fix2_a_param_that_truly_did_not_move_is_still_a():
    k = "exposure.black"
    chk = Check("M1", kind="changed", target={"key": k, "unit": "ratio"})
    case = _edit_case([chk], {"changed": True, "key": k})
    # history grew (fingerprint changed) but the value is bit-identical
    obs = _steps_obs([_mv("exposure", "black")], [{k: 0.005}, {k: 0.005}])
    v = evaluate(case, obs, POLICY)
    assert v.silent_failure == "a: reported ok but exposure.black did not change"
    # a float32 round-trip of the same value is not a change either
    obs = _steps_obs([_mv("exposure", "black")], [{k: 0.05}, {k: 0.050000011920928955}])
    assert evaluate(case, obs, POLICY).outcome == "silent_failure"
    # a hue wrap 360 -> 0 is not a change
    h = "colorbalancergb.shadows_hue"
    hc = Check("M1", kind="changed", target={"key": h, "unit": "°"})
    obs = _steps_obs([_mv("colorbalancergb", "shadows_hue")], [{h: 360.0}, {h: 0.0}])
    assert evaluate(_edit_case([hc], {"changed": True, "key": h, "units": {h: "°"}}), obs,
                    POLICY).outcome == "silent_failure"


def test_fix2_multi_say_uses_every_reported_key():
    """cov-p2-deeper_blacks-more_vibrance-olympus: two says, two undos, two says;
    the last say reports exposure.black, but global_vibrance (reported by an
    earlier ok say) also changed. Not a (b)."""
    b, gv = "exposure.black", "colorbalancergb.global_vibrance"
    blacks = _say_env([_move("exposure", "black")], key="deeper_blacks")
    vib = _say_env([_move("colorbalancergb", "global_vibrance")], key="more_vibrance")
    undo = _ok({})
    s0 = {b: -0.000244140625, gv: 0.0}
    s1 = {b: 0.0048, gv: 0.0}
    s2 = {b: 0.0048, gv: 0.05}
    s4 = {b: -0.0002, gv: 0.0}
    s5 = {b: -0.0002, gv: 0.05}
    s6 = {b: 0.0048, gv: 0.05}
    chk = Check("M1", kind="step_equal", target={"a": 2, "b": 6, "keys": [b, gv]})
    case = _edit_case([chk], None, kind="property", steps=[Step("darkroom_say", write=True)] * 6)
    obs = _steps_obs([blacks, vib, undo, undo, vib, blacks], [s0, s1, s2, s1, s4, s5, s6],
                     tools=["darkroom_say", "darkroom_say", "darkroom_undo_last",
                            "darkroom_undo_last", "darkroom_say", "darkroom_say"])
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "verified", v.silent_failure


def test_fix2_change_on_an_unrelated_preexisting_param_is_still_b_and_collateral():
    b, sc = "exposure.black", "sigmoid.contrast"
    case = _edit_case([], {"changed": True, "key": b})
    obs = _steps_obs([_mv("exposure", "black")], [{b: 0.0, sc: 1.5}, {b: 0.0, sc: 1.505}])
    assert evaluate(case, obs, POLICY).silent_failure == \
        "b: reported exposure.black but sigmoid.contrast changed"
    chk = Check("M1", kind="equals", target={"key": b, "value": 0.01, "unit": "ratio"})
    obs = _steps_obs([_mv("exposure", "black")], [{b: 0.0, sc: 1.5}, {b: 0.01, sc: 1.505}])
    v = evaluate(_edit_case([chk], {"changed": True, "key": b}), obs, POLICY)
    assert v.outcome == "collateral" and v.notes == [sc]


# ---- bug 3: a module the write introduces is not collateral; default == absent


def test_fix3_module_introduced_by_the_write_is_not_collateral():
    """cov-colorbalancergb.global_saturation-sony_a7-set_hi: the module was not
    in the history; the write adds it with every sibling at 0."""
    k = "colorbalancergb.global_saturation"
    chk = Check("M1", kind="equals", target={"key": k, "unit": "%", "value": 0.5})
    case = _edit_case([chk], {"allowed": [], "changed": True, "key": k, "units": {k: "%"}})
    obs = _steps_obs([_mv("colorbalancergb", "global_saturation")],
                     [{"exposure.exposure": 0.7}, {"exposure.exposure": 0.7,
                                                   **_cb(global_saturation=0.5)}])
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "verified", (v.outcome, v.notes)


def test_fix3_filmic_introduced_with_nonzero_siblings_is_not_collateral():
    """cov-filmicrgb.contrast-olympus-adjust_overflow."""
    k = "filmicrgb.contrast"
    chk = Check("M1", kind="equals", target={"key": k, "unit": "ratio", "value": 3.0})
    case = _edit_case([chk], {"allowed": [], "changed": True, "key": k, "units": {k: "ratio"}},
                      steps=[Step("darkroom_set", write=True), Step("darkroom_adjust", write=True)])
    filmic = {"filmicrgb.__enabled": 1, "filmicrgb.black_relative_exposure": -7.65,
              "filmicrgb.white_relative_exposure": 4.56}
    obs = _steps_obs([_mv("filmicrgb", "contrast"), _mv("filmicrgb", "contrast")],
                     [{}, {**filmic, k: 2.9000000953674316}, {**filmic, k: 3.0}])
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "verified", (v.outcome, v.notes)


def test_fix3_siblings_of_a_module_already_present_are_still_collateral():
    k = "colorbalancergb.global_saturation"
    chk = Check("M1", kind="equals", target={"key": k, "unit": "%", "value": 0.5})
    case = _edit_case([chk], {"allowed": [], "changed": True, "key": k})
    obs = _steps_obs([_mv("colorbalancergb", "global_saturation")],
                     [_cb(), _cb(global_saturation=0.5, contrast=0.1)])
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "collateral" and v.notes == ["colorbalancergb.contrast"]
    # a DIFFERENT module introduced by the write is collateral too
    obs = _steps_obs([_mv("colorbalancergb", "global_saturation")],
                     [_cb(), {**_cb(global_saturation=0.5), "filmicrgb.__enabled": 1,
                              "filmicrgb.contrast": 1.0}])
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "collateral" and v.notes == ["filmicrgb.__enabled", "filmicrgb.contrast"]
    # the module IS present before, but this sibling key is not in the snapshot:
    # the write did not introduce the module, so the sibling appearing is
    # collateral (pins the `_module_absent` guard; without it the absent-before
    # sibling would be excused as part of an "introduced" module)
    obs = _steps_obs([_mv("colorbalancergb", "global_saturation")],
                     [{k: 0.0}, {k: 0.5, "colorbalancergb.contrast": 0.1}])
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "collateral" and v.notes == ["colorbalancergb.contrast"]


def _undo_case(k):
    chk = Check("M1", kind="restored", target={"from": 0, "keys": [k], "units": {k: "%"}})
    return _edit_case([chk], None, kind="workflow",
                      steps=[Step("darkroom_adjust", write=True),
                             Step("darkroom_undo_last", write=True)])


def test_fix3_undo_back_to_the_module_default_is_restored():
    """cov-colorbalancergb.highlights_chroma-olympus-undo_after_adjust: before the
    module is absent; after undo it is present at its default (0.0)."""
    k = "colorbalancergb.highlights_chroma"
    snaps = [{}, _cb(highlights_chroma=0.019999999552965164), _cb()]
    obs = _steps_obs([_mv("colorbalancergb", "highlights_chroma"), _ok({})], snaps,
                     before_meta={"defaults": CB_DEFAULTS},
                     tools=["darkroom_adjust", "darkroom_undo_last"])
    v = evaluate(_undo_case(k), obs, POLICY)
    assert v.outcome == "verified", (v.silent_failure, v.checks)


def test_fix3_undo_that_leaves_a_non_default_or_unknown_default_still_fails():
    k = "colorbalancergb.highlights_chroma"
    tools = ["darkroom_adjust", "darkroom_undo_last"]
    envs = [_mv("colorbalancergb", "highlights_chroma"), _ok({})]
    # the undone key is left at a non-default value
    obs = _steps_obs(envs, [{}, _cb(highlights_chroma=0.02), _cb(highlights_chroma=0.02)],
                     before_meta={"defaults": CB_DEFAULTS}, tools=tools)
    assert evaluate(_undo_case(k), obs, POLICY).silent_failure.startswith("e:")
    # the key is at default but a sibling is not: the module is not "as if absent"
    obs = _steps_obs(envs, [{}, _cb(highlights_chroma=0.02), _cb(contrast=0.1)],
                     before_meta={"defaults": CB_DEFAULTS}, tools=tools)
    assert evaluate(_undo_case(k), obs, POLICY).silent_failure.startswith("e:")
    # the module is disabled after undo: not equivalent to absent
    obs = _steps_obs(envs, [{}, _cb(highlights_chroma=0.02),
                            {**_cb(), "colorbalancergb.__enabled": 0}],
                     before_meta={"defaults": CB_DEFAULTS}, tools=tools)
    assert evaluate(_undo_case(k), obs, POLICY).silent_failure.startswith("e:")
    # no defaults known: conservative, still a mismatch
    obs = _steps_obs(envs, [{}, _cb(highlights_chroma=0.02), _cb()], tools=tools)
    assert evaluate(_undo_case(k), obs, POLICY).silent_failure.startswith("e:")
    # module present before: None-before rule does not apply at all
    obs = _steps_obs(envs, [_cb(highlights_chroma=0.1), _cb(highlights_chroma=0.12), _cb()],
                     before_meta={"defaults": CB_DEFAULTS}, tools=tools)
    assert evaluate(_undo_case(k), obs, POLICY).silent_failure.startswith("e:")


# ---- bug 4: temperature/tint are derived from the stored WB coefficients


def test_fix4_tint_set_moves_the_coefficients_it_is_derived_from():
    """cov-temperature.tint-sony_a7-set_0.8 (no Kelvin decode on this body)."""
    case = _edit_case([], {"changed": True})
    obs = _steps_obs([_mv("temperature", "tint", previous=None, applied=None)],
                     [{"temperature.coeff_b": 2.32421875, "temperature.coeff_g2": 0.0,
                       "temperature.coeff_r": 1.61328125, "temperature.preset": 4},
                      {"temperature.coeff_b": 2.8568811416625977, "temperature.coeff_g2": None,
                       "temperature.coeff_r": 2.43988299369812, "temperature.preset": 2}],
                     before_meta=WB_META)
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "verified", v.silent_failure
    # olympus: the derived Kelvin moves too (cov-temperature.tint-olympus-set_1.1)
    obs = _steps_obs([_mv("temperature", "tint")],
                     [{"temperature.coeff_b": 1.8203125, "temperature.coeff_g2": 0.0,
                       "temperature.coeff_r": 1.9140625, "temperature.preset": 4,
                       "temperature.temperature": 4924.615011773081},
                      {"temperature.coeff_b": 1.7376030683517456, "temperature.coeff_g2": None,
                       "temperature.coeff_r": 1.7524051666259766, "temperature.preset": 2,
                       "temperature.temperature": 4788.635454379341}], before_meta=WB_META)
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "verified", v.silent_failure


def test_fix4_temperature_set_is_not_collateral_on_its_coefficients():
    """cov-temperature.temperature-olympus-set_hi."""
    k = "temperature.temperature"
    chk = Check("M1", kind="equals", target={"key": k, "unit": "K", "value": 12000.0})
    case = _edit_case([chk], {"allowed": [], "changed": True, "key": k, "units": {k: "K"}},
                      steps=[Step("darkroom_set", write=True), Step("darkroom_set", write=True)])
    s = [{"temperature.coeff_b": 1.8203125, "temperature.coeff_g2": 0.0,
          "temperature.coeff_r": 1.9140625, "temperature.preset": 4, k: 4924.615011773081},
         {"temperature.coeff_b": 1.8782345056533813, "temperature.coeff_g2": None,
          "temperature.coeff_r": 2.0358850955963135, "temperature.preset": 2, k: 5000},
         {"temperature.coeff_b": 1.2534654140472412, "temperature.coeff_g2": None,
          "temperature.coeff_r": 2.7179274559020996, "temperature.preset": 2, k: 12000}]
    obs = _steps_obs([_mv("temperature", "tint"), _mv("temperature", "temperature")], s,
                     before_meta=WB_META)
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "verified", (v.outcome, v.notes)


def test_fix4_wb_ownership_does_not_hide_real_problems():
    # a temperature report that moved an unrelated param is still (b)
    case = _edit_case([], {"changed": True})
    obs = _steps_obs([_mv("temperature", "temperature")],
                     [{"temperature.coeff_r": 1.9, "sigmoid.contrast": 1.5},
                      {"temperature.coeff_r": 1.9, "sigmoid.contrast": 1.6}], before_meta=WB_META)
    assert evaluate(case, obs, POLICY).silent_failure == \
        "b: reported temperature.temperature but sigmoid.contrast changed"
    # a warmer move that renders cooler still fails its M4 stat check (c)
    chk = Check("M4", stat="rb", region="frame", sign=1)
    obs = _steps_obs([_mv("temperature", "temperature")],
                     [{"temperature.coeff_r": 1.9, "temperature.coeff_g2": 0.0},
                      {"temperature.coeff_r": 2.1, "temperature.coeff_g2": None}],
                     before_meta=WB_META)
    obs.stats_before, obs.stats_after = {"frame": {"rb": 1.0}}, {"frame": {"rb": 0.8}}
    v = evaluate(_edit_case([chk], {"changed": True}), obs, POLICY)
    assert v.silent_failure.startswith("c: M4 rb moved"), v.silent_failure
    # coeff_g2 0.0 -> None alone is not a change, so the history-only write is still (a)
    obs = _steps_obs([_mv("temperature", "temperature")],
                     [{"temperature.coeff_r": 1.9, "temperature.coeff_g2": 0.0},
                      {"temperature.coeff_r": 1.9, "temperature.coeff_g2": None}],
                     before_meta=WB_META)
    v = evaluate(_edit_case([], {"changed": True, "key": "temperature.coeff_r"}), obs, POLICY)
    assert v.silent_failure is not None and v.silent_failure.startswith("a:"), v.silent_failure


def test_fix4_derived_key_is_restored_only_when_its_coefficients_are():
    """cov-temperature.temperature-sony_a7-undo_after_set: Kelvin is undecodable on
    this body (None before and after), the undo refused and the coefficients
    stayed at the 7000 K values; None == None must not read as restored."""
    k = "temperature.temperature"
    chk = Check("M1", kind="restored", target={"from": 1, "keys": [k], "units": {k: "K"}})
    case = _edit_case([chk], None, kind="workflow",
                      steps=[Step("darkroom_set", write=True), Step("darkroom_set", write=True),
                             Step("darkroom_undo_last", write=True)])
    s1 = {"temperature.coeff_b": 2.3526809215545654, "temperature.coeff_g2": None,
          "temperature.coeff_r": 1.6502976417541504, "temperature.preset": 2}
    s2 = {"temperature.coeff_b": 1.1910351514816284, "temperature.coeff_g2": None,
          "temperature.coeff_r": 2.8313372135162354, "temperature.preset": 2}
    s0 = {"temperature.coeff_b": 2.32421875, "temperature.coeff_g2": 0.0,
          "temperature.coeff_r": 1.61328125, "temperature.preset": 4}
    tools = ["darkroom_set", "darkroom_set", "darkroom_undo_last"]
    envs = [_mv("temperature", "tint"), _mv("temperature", "temperature"),
            _err("unsupported_undo")]
    v = evaluate(case, _steps_obs(envs, [s0, s1, s2, s2], before_meta=WB_META, tools=tools),
                 POLICY)
    assert v.score == "fail" and not v.checks[0].ok, v
    # an undo that put the coefficients back is restored (coeff_g2 None == None)
    ok_undo = [*envs[:2], _ok({})]
    v = evaluate(case, _steps_obs(ok_undo, [s0, s1, s2, s1], before_meta=WB_META, tools=tools),
                 POLICY)
    assert v.outcome == "verified", v


# -- row 501 item B (fix round 1, Opus finding 3): the "verb" and "undo" ----
# check kinds, and the workflow needs_review fallback for a clarify-only
# conversation with no verb/write call.

def _agent_workflow_case(checks, sidecar):
    return Case(id="sca-c", layer="scenarios_agent", tier="P0", mode="agent", steps=[],
               expect=Expect("workflow", checks, sidecar), meta={})


def test_verb_check_rejects_a_successful_read_that_is_not_the_workflow_verb():
    """Night-1 r0159 (cull)/r0066 (export)/r0356 (export) shape: the agent's
    LAST call is a successful READ, never the tool this workflow verb
    needs. The old generic check ("last call answered ok") scored this
    verified; the verb check must not."""
    check = Check("verb", target={"any_of": ["cull.propose"]})
    case = _agent_workflow_case([check], {"changed": False})
    calls = [CallRecord("scout_stats", {}, {"ok": True, "outcome": "ok", "verb": "scout.stats"})]
    obs = Observation(calls=calls, before=_snap("a"), after=_snap("a"))
    v = evaluate(case, obs, POLICY)
    assert v.score != "pass" and v.outcome != "verified", v


def test_verb_check_rejects_zero_calls_r0159_hollow_pass_is_impossible():
    check = Check("verb", target={"any_of": ["cull.propose"]})
    case = _agent_workflow_case([check], {"changed": False})
    obs = Observation(calls=[], before=_snap("a"), after=_snap("a"))
    v = evaluate(case, obs, POLICY)
    assert v.score != "pass" and v.outcome != "verified", v
    assert v.outcome == "needs_review", v  # clarify-only, no verb/write call at all


def test_clarify_only_needs_review_reaches_r0348_r0364_shape_before_sidecar_gate():
    """(row 501 item B, fix round 2, Pratik's ruling 3, Opus re-review
    finding 3): `preset_save` (r0348) and `repeat_more` (r0364, "More.")
    both carry `sidecar={"changed": True}`. With the sidecar-must-change
    gate checked BEFORE the clarify-only fallback (the fix round 1 bug), a
    clarify-only conversation (no calls at all) hit `_sidecar_miss`'s
    "sidecar expected to change but did not" and returned a hard `failed`
    -- the clarify-only needs_review branch was never reached. GREEN: the
    clarify-only branch runs first."""
    require_write_check = Check("verb", target={"require_write": True})
    case = _agent_workflow_case([require_write_check], {"changed": True})
    obs = Observation(calls=[], before=_snap("a"), after=_snap("a"))
    v = evaluate(case, obs, POLICY)
    assert v.outcome != "failed", v
    assert v.outcome == "needs_review", v

    any_of_check = Check("verb", target={"any_of": ["preset.save"]})
    case2 = _agent_workflow_case([any_of_check], {"changed": True})
    obs2 = Observation(calls=[], before=_snap("a"), after=_snap("a"))
    v2 = evaluate(case2, obs2, POLICY)
    assert v2.outcome == "needs_review", v2

    # A real write that is not the workflow's own verb (not clarify-only:
    # something WAS written) must still fall through to the sidecar gate,
    # not needs_review -- the fix must not swallow every failure.
    calls_wrote_something_else = [
        CallRecord("darkroom_set", {}, {"ok": True, "verb": "darkroom.set"}, write=True)]
    obs3 = Observation(calls=calls_wrote_something_else, before=_snap("a"), after=_snap("a"))
    v3 = evaluate(case2, obs3, POLICY)
    assert v3.outcome != "needs_review", v3


def test_verb_check_passes_on_the_real_verb_even_if_not_the_last_call():
    """The agent reads state first (a real, sensible habit), then calls the
    verb: the verb check must not require it to be the LAST call."""
    check = Check("verb", target={"any_of": ["export.release", "export.run"]})
    case = _agent_workflow_case([check], {"changed": False})
    calls = [CallRecord("library_jobs", {}, {"ok": True, "verb": "library.jobs"}),
             CallRecord("export_release", {}, {"ok": True, "verb": "export.release"}),
             CallRecord("darkroom_status", {}, {"ok": True, "verb": "darkroom.status"})]
    obs = Observation(calls=calls, before=_snap("a"), after=_snap("a"))
    v = evaluate(case, obs, POLICY)
    assert v.score == "pass" and v.outcome == "verified", v


def test_repeat_more_verb_check_requires_a_real_write_not_any_ok_call():
    check = Check("verb", target={"require_write": True})
    case = _agent_workflow_case([check], {"changed": True})
    # a read-only conversation: no write call anywhere
    calls = [CallRecord("darkroom_get", {}, {"ok": True, "verb": "darkroom.get"})]
    obs = Observation(calls=calls, before=_snap("a", **{"exposure.exposure": 1.0}),
                      after=_snap("a", **{"exposure.exposure": 1.0}))
    v = evaluate(case, obs, POLICY)
    assert v.score != "pass", v
    # a real write call passes
    calls_ok = [CallRecord("darkroom_say", {}, {"ok": True, "verb": "darkroom.say"}, write=True)]
    obs_ok = Observation(calls=calls_ok, before=_snap("a", **{"exposure.exposure": 1.0}),
                         after=_snap("b", **{"exposure.exposure": 1.5}))
    v2 = evaluate(case, obs_ok, POLICY)
    assert v2.score == "pass" and v2.outcome == "verified", v2


def test_undo_check_verified_by_history_end_decrement_no_sidecar_gate():
    """Ruling: r0340/r0344/r0368-shape undo cases are verified by
    history_end DECREMENT or params restored -- not gated on a sidecar
    change a fresh frame with nothing of the agent's own to undo may never
    produce."""
    case = _agent_workflow_case([Check("undo", target={})], None)
    calls = [CallRecord("darkroom_undo_last", {}, {"ok": True, "verb": "darkroom.undo_last"},
                        write=True)]
    before = {"fingerprint": "a", "history_end": 3, "params": {"exposure.exposure": 1.0}}
    after = {"fingerprint": "a", "history_end": 2, "params": {"exposure.exposure": 1.0}}
    obs = Observation(calls=calls, before=before, after=after)
    v = evaluate(case, obs, POLICY)
    assert v.score == "pass" and v.outcome == "verified", v


def test_undo_check_needs_review_with_zero_calls_hollow_pass_is_impossible():
    """(row 501 item B, fix round 2, Pratik's ruling 1, Opus re-review
    finding 1): the old check only compared before/after snapshots, which
    are trivially equal to themselves when nothing was called at all --
    r0340/r0344/r0368 scored `verified` with zero calls, one successful
    read, or a no-op undo. A successful `darkroom_undo_last` call is now
    required before `decremented OR restored` is even evaluated; with none,
    and no write call at all, the workflow needs_review fallback applies
    (the same shape as the verb check's own zero-call case) -- never a
    silent pass."""
    case = _agent_workflow_case([Check("undo", target={})], None)
    before = {"fingerprint": "a", "history_end": 1, "params": {"exposure.exposure": 1.0}}
    after = {"fingerprint": "a", "history_end": 1, "params": {"exposure.exposure": 1.0}}
    obs = Observation(calls=[], before=before, after=after)
    v = evaluate(case, obs, POLICY)
    assert v.score != "pass" and v.outcome != "verified", v
    assert v.outcome == "needs_review", v

    # A successful READ (never an undo call) must not pass either.
    read_calls = [CallRecord("library_jobs", {}, {"ok": True, "verb": "library.jobs"})]
    obs_read = Observation(calls=read_calls, before=before, after=after)
    v_read = evaluate(case, obs_read, POLICY)
    assert v_read.score != "pass" and v_read.outcome == "needs_review", v_read


def test_undo_check_passes_on_a_real_undo_call_that_restores_params():
    """A genuine, successful `darkroom_undo_last` call (verb
    `darkroom.undo_last`, envelope `ok`) that restores the snapshotted
    params -- the guard above lets `decremented OR restored` decide, same
    as before the fix."""
    case = _agent_workflow_case([Check("undo", target={})], None)
    calls = [CallRecord("darkroom_undo_last", {}, {"ok": True, "verb": "darkroom.undo_last"},
                        write=True)]
    before = {"fingerprint": "a", "history_end": 1, "params": {"exposure.exposure": 1.0}}
    after = {"fingerprint": "a", "history_end": 1, "params": {"exposure.exposure": 1.0}}
    obs = Observation(calls=calls, before=before, after=after)
    v = evaluate(case, obs, POLICY)
    assert v.score == "pass" and v.outcome == "verified", v


def test_undo_check_fails_when_neither_decremented_nor_restored():
    case = _agent_workflow_case([Check("undo", target={})], None)
    calls = [CallRecord("darkroom_undo_last", {}, {"ok": True, "verb": "darkroom.undo_last"},
                        write=True)]
    before = {"fingerprint": "a", "history_end": 1, "params": {"exposure.exposure": 1.0}}
    after = {"fingerprint": "b", "history_end": 2, "params": {"exposure.exposure": 1.8}}
    obs = Observation(calls=calls, before=before, after=after)
    v = evaluate(case, obs, POLICY)
    assert v.score != "pass", v


def test_undo_check_honest_noop_scores_needs_review_not_failed():
    """An empty journal makes `darkroom_undo_last` answer `ok: false,
    outcome: "noop"` -- an honest "nothing to undo", not a failed attempt.
    That must not count as a write for the clarify-only fallback, or an
    agent that honestly finds nothing to undo scores worse than one that
    never called anything at all."""
    case = _agent_workflow_case([Check("undo", target={})], None)
    calls = [CallRecord("darkroom_undo_last", {},
                        {"ok": False, "outcome": "noop", "verb": "darkroom.undo_last",
                         "error": {"code": "noop", "message": "nothing to undo"}}, write=True)]
    before = {"fingerprint": "a", "history_end": 0, "params": {"exposure.exposure": 1.0}}
    after = {"fingerprint": "a", "history_end": 0, "params": {"exposure.exposure": 1.0}}
    obs = Observation(calls=calls, before=before, after=after)
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "needs_review", v


# -- row 501 item G, ruling 8: `unrequested_edit` in OUTCOMES -----------------

def test_unrequested_edit_is_in_the_outcomes_table_as_a_fail():
    """(row 501 item G, fix round 1, Opus finding 6): `evaluate()`'s
    out-of-scope policy branch returns the outcome `unrequested_edit`, but
    it was missing from `OUTCOMES` -- tidiness only (nothing here reads the
    table to score a case), but any reporting code that DOES read it must
    see it as a fail, same as its sibling `unexpected_edit`."""
    assert OUTCOMES["unrequested_edit"] == FAIL


def test_unrequested_edit_outcome_is_produced_and_now_in_the_table():
    """End to end: the out-of-scope refuse_and_offer_only policy's own
    write-before-choice case really does produce `unrequested_edit`, and
    that outcome now has a table entry."""
    case = Case(id="g-c", layer="scenarios_agent", tier="P0", mode="agent",
               steps=[], expect=Expect("refuse"),
               meta={"out_of_scope_policy": "refuse_and_offer_only"})
    obs = Observation(calls=[], agent_text="Sure, done.",
                      before={"fingerprint": "a"}, after={"fingerprint": "b"})
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "unrequested_edit"
    assert OUTCOMES[v.outcome] == FAIL
