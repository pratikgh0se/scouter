"""N12 gauntlet follow-up: coverage nudges of a soft-bounded slider (channelmixerrgb.temperature,
`vocab.DT_SOFT_BOUNDS`) test a real move from inside the soft range, and the soft max has its own
honest-noop case. Never launches darktable."""

from __future__ import annotations

import pytest

from gauntlet.adapter import CallRecord, Observation
from gauntlet.evaluate import evaluate
from scouter.core import vocab
from tests.gauntlet.layers import coverage
from tests.gauntlet.policy import load_policy

POLICY = load_policy()
CASES = {c.id: c for c in coverage.generate()}
KEY = ("channelmixerrgb", "temperature")
LO, HI = vocab.DT_SOFT_BOUNDS[KEY]
BODIES = ("olympus", "sony_a7")
BASE = {"channelmixerrgb.temperature": 3926.7, "channelmixerrgb.x": 0.3, "channelmixerrgb.y": 0.32}
UNITS = {"channelmixerrgb.temperature": "K"}


def _cid(body: str, label: str) -> str:
    return f"cov-channelmixerrgb.temperature-{body}-{label}"


@pytest.mark.parametrize("body", BODIES)
@pytest.mark.parametrize("label", ["nudge_up1", "nudge_dn1"])
def test_nudge_cases_start_inside_the_soft_range(body, label):
    case = CASES[_cid(body, label)]
    prep, action = case.steps
    assert prep.setup and prep.tool == "darkroom_set"
    assert LO < prep.args["value"] < HI
    assert action.tool == "darkroom_nudge"
    assert case.expect.kind == "edit"


@pytest.mark.parametrize("body", BODIES)
def test_soft_max_nudge_up_has_its_own_honest_noop_case(body):
    case = CASES[_cid(body, "nudge_up_soft_max")]
    prep, action = case.steps
    assert prep.setup and prep.args["value"] == HI
    assert action.tool == "darkroom_nudge" and action.args["steps"] == 1
    assert case.expect.kind == "noop" and case.expect.codes == ["soft_limit"]


def test_no_coverage_case_nudges_into_a_soft_bound_unless_it_expects_the_noop():
    for case in CASES.values():
        last_set = None
        for st in case.steps:
            if (st.args.get("module"), st.args.get("param")) != KEY:
                continue
            if st.tool == "darkroom_set":
                last_set = st.args["value"]
            if st.tool == "darkroom_nudge" and case.expect.kind != "noop":
                n = st.args["steps"]
                assert last_set is not None, case.id
                assert (last_set < HI) if n > 0 else (last_set > LO), case.id


def _obs(env: dict, after_nudge: float = HI) -> Observation:
    calls = [CallRecord("darkroom_set", {}, {"ok": True, "outcome": "ok", "data": {}}, 1.0,
                        write=True),
             CallRecord("darkroom_nudge", {}, env, 1.0, write=True)]
    s1 = {**BASE, "channelmixerrgb.temperature": HI}
    s2 = {**BASE, "channelmixerrgb.temperature": after_nudge}
    return Observation(calls=calls, before={"params": BASE, "fingerprint": "a"},
                       after={"params": s2, "fingerprint": "b", "units": UNITS},
                       extra={"snapshots": [BASE, s1, s2]})


def _noop(reason: str | None) -> dict:
    return {"ok": True, "outcome": "ok", "data": {"noop": True, "reason": reason}}


def test_honest_soft_limit_noop_passes_after_a_prep_set():
    case = CASES[_cid("olympus", "nudge_up_soft_max")]
    v = evaluate(case, _obs(_noop("soft_limit")), POLICY)
    assert (v.outcome, v.silent_failure) == ("verified", None), v


def test_a_noop_with_another_reason_does_not_pass():
    case = CASES[_cid("olympus", "nudge_up_soft_max")]
    assert evaluate(case, _obs(_noop(None)), POLICY).outcome == "failed"


def test_a_noop_claim_while_the_sidecar_moved_does_not_pass():
    case = CASES[_cid("olympus", "nudge_up_soft_max")]
    v = evaluate(case, _obs(_noop("soft_limit"), after_nudge=HI + 1), POLICY)
    assert v.outcome != "verified", v


def test_an_ok_nudge_that_changed_nothing_is_still_not_a_pass_for_the_noop_case():
    case = CASES[_cid("olympus", "nudge_up_soft_max")]
    env = {"ok": True, "outcome": "ok", "data": {"committed": True}}
    assert evaluate(case, _obs(env), POLICY).outcome == "failed"
