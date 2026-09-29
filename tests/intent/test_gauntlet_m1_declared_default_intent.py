"""Runs tests/intent/gauntlet_m1_declared_default.json (Zenkai Rules Z1):
the two real proof4-20260929 evidence cases (mirrored, read-only originals
untouched, at gauntlet/tests/fixtures/proof4-20260929/) whose `after.params`
carries no history entry for `colorzones` at all, so the M1 `equals` check
on `colorzones.strength` used to read `measured: null` and fail even though
`after.defaults` carried a host-verified default and the product's own
envelope reported an honest noop at that default (decision rows 548, 554).

Two independent oracles per example (Z2), mirroring
tests/intent/test_batch3_c5_evaluator_intent.py: oracle A reads the raw
recorded facts (the envelope's own `noop`/`applied` fields, and that the
module is genuinely absent from `after.params`) -- never the old recorded
verdict, which is the thing being corrected -- against oracle B, the fixed
`evaluate()` scoring the SAME observation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet.adapter import CallRecord, Observation
from gauntlet.evaluate import evaluate
from tests.gauntlet.layers import coverage
from tests.gauntlet.policy import load_policy

FIXTURES = json.loads((Path(__file__).parent /
                       "gauntlet_m1_declared_default_fixtures.json").read_text())
POLICY = load_policy()
CASES = {c.id: c for c in coverage.generate()}


def _build_observation(o: dict) -> Observation:
    calls = [CallRecord(c["tool"], c.get("args", {}), c.get("envelope"), c.get("wall_s", 0.0),
                        c.get("ms"), c.get("bounced", False), c.get("error"), c.get("health"),
                        c.get("write", False)) for c in o["calls"]]
    return Observation(calls=calls, before=o.get("before", {}), after=o.get("after", {}),
                      extra=o.get("extra", {}))


def _module_absent(after_params: dict, module: str) -> bool:
    return not any(v is not None for k, v in after_params.items()
                  if k.split(".", 1)[0] == module)


@pytest.mark.parametrize("case_id", sorted(FIXTURES))
def test_declared_default_now_scores_the_recorded_write_a_pass(case_id):
    fx = FIXTURES[case_id]
    obs_dict = fx["observation"]
    after_params = obs_dict["after"]["params"]
    after_defaults = obs_dict["after"]["defaults"]

    # oracle A: the raw recorded facts a correct evaluator should honour.
    assert _module_absent(after_params, "colorzones"), case_id
    assert after_defaults["colorzones.strength"] == 0.0, case_id
    data = obs_dict["calls"][-1]["envelope"]["data"]
    assert data["noop"] is True and data["applied"] == 0.0, case_id
    # the case really did score fail/null before this fix (spec 5.4's own
    # ground truth: recorded_outcome/recorded_measured from the proof4 run).
    assert fx["recorded_outcome"] == "failed", case_id
    assert fx["recorded_measured"] is None, case_id

    # oracle B: the fixed evaluator, scoring the SAME observation.
    case = CASES[case_id]
    obs = _build_observation(obs_dict)
    v = evaluate(case, obs, POLICY)
    assert (v.score, v.outcome) == ("pass", "verified"), (case_id, v.outcome, v.notes)
    assert v.checks[0].measured == 0.0, case_id
