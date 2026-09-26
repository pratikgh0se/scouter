from gauntlet.adapter import CallRecord, Observation
from gauntlet.case import Case, Check, Expect, Step
from gauntlet.evaluate import POINTS, Policy, canonical_outcome, evaluate

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
