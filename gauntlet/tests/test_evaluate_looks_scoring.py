"""Night 3 looks-scoring round (harness only). Each test states, in words, the
behaviour a human calls right (Z1); both oracles (sidecar readback + image
statistic) stay in play (Z2)."""

from gauntlet.adapter import CallRecord, Observation
from gauntlet.case import Case, Check, Expect, Step
from gauntlet.evaluate import Policy, evaluate

POLICY = Policy(
    refusal_codes=frozenset({"usage", "value_unknown", "not_pinned", "module_unavailable"}),
    transient_codes=frozenset({"gui_not_ready", "bridge_timeout", "commit_timeout"}),
    refusal_markers=("can't", "cannot", "not able"),
    claim_markers=(r"\bdone\b", r"\bi've\b"),
)


def _snap(fp, **params):
    return {"fingerprint": fp, "history_end": 1, "params": params}


def _look_case(checks, meta=None, steps=None):
    meta = {"look": "x", **(meta or {})}
    return Case(id="lk-x-chart", layer="looks", tier="P1", mode="tool",
                steps=steps or [Step("look_plan", {"look_id": "x"}, 40.0, True)],
                expect=Expect("edit", checks, {"changed": True}), meta=meta)


def _look_call(moves):
    ok = all(m["ok"] for m in moves)
    return CallRecord("look_plan", {}, {"ok": ok, "outcome": "ok" if ok else "error:partial",
                                        "data": {"moves": moves}}, write=True)


def _mv(param, value, ok=True, error=None, module="colorbalancergb"):
    return {"tool": "darkroom_set", "module": module, "param": param, "value": value,
            "ok": ok, "error": error}


# ---- item 1: look_plan sub-moves count as landed requests; last write per key wins

def test_look_plan_landed_but_sub_floor_move_is_imperceptible_not_silent():
    """'Faithful look, +0.06 shadows' on a dark raw: the sidecar shows
    shadows_luminance at the asked 0.0607, the L statistic moved +0.27 under
    the 0.5 floor. The edit is real and complete: imperceptible_step."""
    chk = Check("M2", stat="L", region="shadows", sign=1)
    obs = Observation(calls=[_look_call([_mv("shadows_luminance", 0.0607)])],
                      before=_snap("a", **{"colorbalancergb.shadows_luminance": 0.0}),
                      after=_snap("b", **{"colorbalancergb.shadows_luminance": 0.0607}),
                      stats_before={"shadows": {"L": 20.0}}, stats_after={"shadows": {"L": 20.27}},
                      floors={"L": 0.5})
    v = evaluate(_look_case([chk]), obs, POLICY)
    assert (v.score, v.outcome) == ("partial", "imperceptible_step")


def test_look_plan_sub_floor_move_that_did_not_land_stays_silent():
    """Control: the look_plan says it set 0.0607 but the sidecar still reads 0.0:
    nothing landed, so the sub-floor exemption must not apply."""
    chk = Check("M2", stat="L", region="shadows", sign=1)
    obs = Observation(calls=[_look_call([_mv("shadows_luminance", 0.0607)])],
                      before=_snap("a", **{"colorbalancergb.shadows_luminance": 0.0}),
                      after=_snap("b", **{"colorbalancergb.shadows_luminance": 0.01}),
                      stats_before={"shadows": {"L": 20.0}}, stats_after={"shadows": {"L": 20.27}},
                      floors={"L": 0.5})
    v = evaluate(_look_case([chk]), obs, POLICY)
    assert v.outcome == "silent_failure"


def test_repeat_step_only_the_last_write_per_key_is_the_landing_claim():
    """'A bit more' twice: shadows_luminance 0.0625 then 0.125. The sidecar ends
    at 0.125, which is what the LAST write asked for; the first step was
    legitimately overwritten and must not deny the landing (scenario r0031)."""
    chk = Check("M2", stat="L", region="shadows", sign=1)

    def say(req, applied, prev):
        return {"ok": True, "outcome": "ok", "data": {"moves": [
            {"module": "colorbalancergb", "param": "shadows_luminance", "requested": req,
             "applied": applied, "unit": "ratio", "clamped": False, "noop": False}]}}
    obs = Observation(
        calls=[CallRecord("darkroom_say", {}, say(0.0625, 0.0625, 0.0), write=True),
               CallRecord("darkroom_say", {}, say(0.125, 0.125, 0.0625), write=True)],
        before=_snap("a", **{"colorbalancergb.shadows_luminance": 0.0}),
        after=_snap("b", **{"colorbalancergb.shadows_luminance": 0.125}),
        stats_before={"shadows": {"L": 20.0}}, stats_after={"shadows": {"L": 20.3}},
        floors={"L": 0.5})
    case = Case(id="s", layer="scenarios", tier="P1", mode="tool",
                steps=[Step("darkroom_say", {}, 3.5, True), Step("darkroom_say", {}, 3.5, True)],
                expect=Expect("edit", [chk], {"changed": True}))
    v = evaluate(case, obs, POLICY)
    assert (v.score, v.outcome) == ("partial", "imperceptible_step")


# ---- item 2: components the planner could not plan / Scouter refused are declared gaps

def _gap_obs(moves, notes=(), stats_after=None, after=None):
    return Observation(
        calls=[_look_call(moves)],
        before=_snap("a", **{"colorbalancergb.shadows_luminance": 0.0}),
        after=_snap("b", **(after or {"colorbalancergb.shadows_luminance": 0.2})),
        stats_before={"shadows": {"L": 20.0}, "frame": {"spread": 50.0}},
        stats_after=stats_after or {"shadows": {"L": 25.0}, "frame": {"spread": 43.7}},
        floors={"L": 0.5, "spread": 0.5}, extra={"look_notes": list(notes)})


SHADOWS = Check("M2", stat="L", region="shadows", sign=1)
SPREAD = Check("M6", stat="spread", region="frame", sign=1, tags={"look_key": "sigmoid.contrast"})


def test_unplanned_contrast_is_a_declared_gap_not_a_silent_failure():
    """'Grade low key: contrast up, shadows down.' The run had no calibration for
    sigmoid.contrast, so no contrast move exists; the spread check measured
    -6.3 because nothing asked for it. That is a declared gap, recorded in the
    verdict, while the shadows move is still verified by both oracles."""
    obs = _gap_obs([_mv("shadows_luminance", 0.2)], ["no calibration for sigmoid.contrast"])
    v = evaluate(_look_case([SHADOWS, SPREAD]), obs, POLICY)
    assert v.outcome == "verified" and v.silent_failure is None
    assert any("sigmoid.contrast" in n and "declared gap" in n for n in v.notes)
    assert len(v.checks) == 1  # the spread check was not asserted


def test_gap_does_not_hide_a_real_failure_of_a_planned_check():
    """Control: with contrast declared a gap, a planned shadows check that moved the
    wrong way is still scored."""
    obs = _gap_obs([_mv("shadows_luminance", 0.2)], ["no calibration for sigmoid.contrast"],
                   stats_after={"shadows": {"L": 15.0}, "frame": {"spread": 43.7}})
    v = evaluate(_look_case([SHADOWS, SPREAD]), obs, POLICY)
    assert v.outcome != "verified" and v.score != "pass"


def test_look_with_only_unplannable_checks_needs_review():
    obs = Observation(calls=[_look_call([])], before=_snap("a"), after=_snap("a"),
                      extra={"look_notes": ["no calibration for sigmoid.contrast"]})
    v = evaluate(_look_case([SPREAD]), obs, POLICY)
    assert (v.score, v.outcome) == ("partial", "needs_review")
    assert "sigmoid.contrast" in v.notes[0]


def test_honest_tint_refusal_is_a_declared_gap_and_the_rest_is_scored():
    """'Warm grey cast' on a camera raw with late WB: Scouter refuses
    temperature.tint (module_unavailable). That is honest; the look's other
    components are judged and the tint check is a gap."""
    tint = Check("M4", stat="a", region="patch:ramp_+0", sign=1,
                 tags={"look_key": "temperature.tint"})
    moves = [_mv("shadows_luminance", 0.2),
             _mv("tint", 0.9, ok=False, error="module_unavailable", module="temperature")]
    v = evaluate(_look_case([SHADOWS, tint]), _gap_obs(moves), POLICY)
    assert v.outcome == "verified"
    assert any("temperature.tint" in n and "refused module_unavailable" in n for n in v.notes)


def test_untyped_sub_move_failure_is_not_a_gap():
    """Control: a sub-move that failed with a non-refusal code is not excused."""
    tint = Check("M4", stat="a", region="patch:ramp_+0", sign=1,
                 tags={"look_key": "temperature.tint"})
    moves = [_mv("shadows_luminance", 0.2),
             _mv("tint", 0.9, ok=False, error="weird_failure", module="temperature")]
    v = evaluate(_look_case([SHADOWS, tint]), _gap_obs(moves), POLICY)
    assert v.outcome != "verified"


def test_stored_cases_without_a_tag_derive_the_gap_from_metric_and_component():
    """Night-3 cases carry no look_key tag: an M6 check is the sigmoid.contrast one."""
    spread = Check("M6", stat="spread", region="frame", sign=1)
    obs = _gap_obs([_mv("shadows_luminance", 0.2)], ["no calibration for sigmoid.contrast"])
    v = evaluate(_look_case([SHADOWS, spread], meta={"look": "x", "check_tags": [
        {"component": "tone"}, {"component": "tone"}]}), obs, POLICY)
    assert v.outcome == "verified"


# ---- item 3: unmeasurable stats and per-step honest refusals

def _say(moves=None, err=None):
    if err:
        return {"ok": False, "outcome": f"error:{err}", "data": {},
                "error": {"code": err, "message": "no", "fix": "x"}}
    return {"ok": True, "outcome": "ok", "data": {"moves": moves}}


def _mvs(param, req, module="colorbalancergb"):
    return {"module": module, "param": param, "requested": req, "applied": req,
            "unit": "ratio", "clamped": False, "noop": False, "committed": True}


def test_clip_high_already_zero_is_unmeasurable_not_silent():
    """'Tame the highlights a bit' on a frame with nothing clipped (r0029): the
    highlights move landed in the sidecar (0 -> -0.05); clip_high was 0.0 before
    and stays 0.0 -- 'less clipping' cannot be observed. Not silent."""
    chk = Check("M3", stat="clip_high", region="frame", sign=-1)
    obs = Observation(
        calls=[CallRecord("darkroom_say", {}, _say([_mvs("highlights_luminance", -0.05)]),
                          write=True)],
        before=_snap("a", **{"colorbalancergb.highlights_luminance": 0.0}),
        after=_snap("b", **{"colorbalancergb.highlights_luminance": -0.05}),
        stats_before={"frame": {"clip_high": 0.0}}, stats_after={"frame": {"clip_high": 0.0}},
        floors={"clip_high": 0.05})
    case = Case(id="s", layer="scenarios", tier="P1", mode="tool",
                steps=[Step("darkroom_say", {}, 3.5, True)],
                expect=Expect("edit", [chk], {"changed": True}))
    v = evaluate(case, obs, POLICY)
    assert v.outcome != "silent_failure" and v.score != "fail"


def test_clip_high_unmeasurable_does_not_excuse_an_unlanded_move():
    """Control: the same stat shape, but the sidecar shows nothing changed while
    the product said ok: still silent (the sidecar oracle is untouched)."""
    chk = Check("M3", stat="clip_high", region="frame", sign=-1)
    obs = Observation(
        calls=[CallRecord("darkroom_say", {}, _say([_mvs("highlights_luminance", -0.05)]),
                          write=True)],
        before=_snap("a", **{"colorbalancergb.highlights_luminance": 0.0}),
        after=_snap("a", **{"colorbalancergb.highlights_luminance": 0.0}),
        stats_before={"frame": {"clip_high": 0.0}}, stats_after={"frame": {"clip_high": 0.0}},
        floors={"clip_high": 0.05})
    case = Case(id="s", layer="scenarios", tier="P1", mode="tool",
                steps=[Step("darkroom_say", {}, 3.5, True)],
                expect=Expect("edit", [chk], {"changed": True}))
    assert evaluate(case, obs, POLICY).outcome == "silent_failure"


def _two_step_case(check_step):
    chk = Check("M4", stat="rb", region="frame", sign=1, step=check_step)
    return Case(id="s", layer="scenarios", tier="P1", mode="tool",
                steps=[Step("darkroom_say", {}, 3.5, True), Step("darkroom_say", {}, 3.5, True)],
                expect=Expect("edit", [chk], {"changed": True}))


def _two_step_obs(first_env, snap_after_first):
    b = _snap("a", **{"exposure.black": 0.0})
    return Observation(
        calls=[CallRecord("darkroom_say", {}, first_env, write=True),
               CallRecord("darkroom_say", {}, _say([_mvs("black", 0.0025, "exposure")]),
                          write=True)],
        before=b, after=_snap("b", **{"exposure.black": 0.0025}),
        stats_before={"frame": {"rb": 0.16}}, stats_after={"frame": {"rb": 0.13}},
        floors={"rb": 0.002},
        extra={"snapshots": [b, snap_after_first, _snap("b", **{"exposure.black": 0.0025})]})


def test_honest_refusal_of_the_checked_step_is_not_silent_even_when_a_later_step_is_ok():
    """'Warmer a touch, then deeper blacks' (r0372): step 0 honestly refuses
    (module_unavailable, nothing written); step 1 is ok. The warmth check belongs
    to step 0, so its miss is the refusal's, not a silent failure."""
    obs = _two_step_obs(_say(err="module_unavailable"), _snap("a", **{"exposure.black": 0.0}))
    v = evaluate(_two_step_case(0), obs, POLICY)
    assert v.outcome != "silent_failure"


def test_check_on_the_ok_step_is_still_silent_when_it_misses():
    """Control: the same miss attributed to the ok step 1 stays silent."""
    obs = _two_step_obs(_say(err="module_unavailable"), _snap("a", **{"exposure.black": 0.0}))
    v = evaluate(_two_step_case(1), obs, POLICY)
    assert v.outcome == "silent_failure"


def test_refused_step_that_changed_the_sidecar_is_not_excused():
    """Control (Z2): a 'refused' step whose own snapshot shows a change keeps the
    old reading."""
    obs = _two_step_obs(_say(err="module_unavailable"), _snap("c", **{"exposure.black": 0.5}))
    v = evaluate(_two_step_case(0), obs, POLICY)
    assert v.outcome == "silent_failure"


# ---- item 5: nested transient codes score blocked, like top-level ones

def test_commit_timeout_inside_a_look_plan_sub_move_is_blocked_not_failed():
    """A look whose global_chroma sub-move hit darktable's commit_timeout (honest,
    sidecar restored, retried by the product) is a blocked case -- darktable was
    not ready -- exactly like a top-level commit_timeout; never `failed`."""
    chk = Check("M5", stat="C", region="frame", sign=1)
    obs = Observation(
        calls=[_look_call([_mv("global_chroma", 0.1, ok=False, error="commit_timeout")])],
        before=_snap("a", **{"colorbalancergb.global_chroma": 0.0}),
        after=_snap("a", **{"colorbalancergb.global_chroma": 0.0}),
        stats_before={"frame": {"C": 40.0}}, stats_after={"frame": {"C": 40.0}},
        floors={"C": 0.5})
    v = evaluate(_look_case([chk]), obs, POLICY)
    assert v.outcome == "blocked"


def test_say_partial_refusing_with_commit_timeout_is_blocked():
    """A `say` partial whose refused move carries commit_timeout is blocked too
    (the `data.refused[*].code` path)."""
    env = {"ok": False, "outcome": "error:partial", "data": {"refused": [
        {"code": "commit_timeout"}]}, "error": {"code": "partial"}}
    obs = Observation(calls=[CallRecord("darkroom_say", {}, env, write=True)],
                      before=_snap("a", **{"exposure.exposure": 0.0}),
                      after=_snap("a", **{"exposure.exposure": 0.0}))
    case = Case(id="s", layer="scenarios", tier="P1", mode="tool",
                steps=[Step("darkroom_say", {}, 3.5, True)],
                expect=Expect("edit", [Check("M1", kind="changed", sign=1,
                                             target={"key": "exposure.exposure",
                                                     "unit": "EV"})], {"changed": True}))
    assert evaluate(case, obs, POLICY).outcome == "blocked"


def test_look_plan_sub_move_failing_with_a_non_transient_code_stays_failed():
    """Control: a non-transient failure code is not blocked."""
    chk = Check("M5", stat="C", region="frame", sign=1)
    obs = Observation(
        calls=[_look_call([_mv("global_chroma", 0.1, ok=False, error="weird_failure")])],
        before=_snap("a", **{"colorbalancergb.global_chroma": 0.0}),
        after=_snap("a", **{"colorbalancergb.global_chroma": 0.0}),
        stats_before={"frame": {"C": 40.0}}, stats_after={"frame": {"C": 40.0}},
        floors={"C": 0.5})
    assert evaluate(_look_case([chk]), obs, POLICY).outcome != "blocked"
