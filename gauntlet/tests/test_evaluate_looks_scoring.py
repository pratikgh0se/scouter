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
    return Case(id="lk-x-chart", layer="looks", tier="P1", mode="tool",
                steps=steps or [Step("look_plan", {"look_id": "x"}, 40.0, True)],
                expect=Expect("edit", checks, {"changed": True}), meta=meta or {})


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
