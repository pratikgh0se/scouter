"""Runs tests/intent/batch3_c5_evaluator_fixtures.json (Zenkai Rules Z1;
format pending t_450535e2): 10 real coverage cases sampled from night-2's 61
C5 evaluator false positives -- the observation is the exact recorded
`observation` block from
~/Pictures/ScouterScratch/gauntlet/night2-20260927/cases/<id>.json, trimmed
to the fields `gauntlet.adapter.Observation` reads. Two independent
observations per example (Z2): oracle A reads the RAW recorded facts a
correct evaluator should honour (the write's own `noop`/`clamped` flags,
the per-step snapshot delta) -- never the old evaluator's own verdict,
which is the thing being corrected -- against oracle B, the fixed
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
                       "batch3_c5_evaluator_fixtures.json").read_text())
POLICY = load_policy()
CASES = {c.id: c for c in coverage.generate()}


def _build_observation(o: dict) -> Observation:
    calls = [CallRecord(c["tool"], c.get("args", {}), c.get("envelope"), c.get("wall_s", 0.0),
                        c.get("ms"), c.get("bounced", False), c.get("error"), c.get("health"),
                        c.get("write", False)) for c in o["calls"]]
    return Observation(calls=calls, before=o.get("before", {}), after=o.get("after", {}),
                      stats_before=o.get("stats_before", {}), stats_after=o.get("stats_after", {}),
                      floors=o.get("floors", {}), agent_text=o.get("agent_text"),
                      blocked=o.get("blocked"), crash_reports=o.get("crash_reports", []),
                      hang_evidence=o.get("hang_evidence", []), healed=o.get("healed", False),
                      extra=o.get("extra", {}), wall_s=o.get("wall_s", 0.0))


def _assert_rule_evidence(rule: str, o: dict, case_id: str) -> None:
    """Oracle A, per rule -- the raw fact the fixed evaluator now honours,
    read straight from the recorded envelope/snapshots, not from the old
    verdict string."""
    data = (o["calls"][-1]["envelope"] or {}).get("data") or {}
    if rule == "f":
        assert data.get("noop") is True, case_id
    elif rule == "c":
        moves = data.get("moves") or []
        assert data.get("clamped") is True or any(m.get("clamped") for m in moves), case_id
    elif rule == "a":
        snaps = o.get("extra", {}).get("snapshots") or []
        assert len(snaps) >= 2, case_id
        assert snaps[-2] != snaps[-1], (case_id, "the write under test must show its own delta")
    else:
        raise AssertionError(f"{case_id}: unknown rule {rule!r}")


@pytest.mark.parametrize("case_id", sorted(FIXTURES))
def test_c5_false_positive_now_scores_the_recorded_behaviour_a_pass(case_id):
    fx = FIXTURES[case_id]
    rule = fx["recorded_silent_failure"][0]
    _assert_rule_evidence(rule, fx["observation"], case_id)  # oracle A
    case = CASES[case_id]
    obs = _build_observation(fx["observation"])
    v = evaluate(case, obs, POLICY)  # oracle B
    assert v.score == "pass", (case_id, v.outcome, v.notes, v.silent_failure)
