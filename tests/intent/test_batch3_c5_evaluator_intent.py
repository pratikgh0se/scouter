"""Runs tests/intent/batch3_c5_evaluator_fixtures.json (Zenkai Rules Z1;
format pending t_450535e2): 10 real coverage cases sampled from night-2's 61
C5 evaluator false positives (row 523, 2026-09-28) -- the observation is the
exact recorded `observation` block from
~/Pictures/ScouterScratch/gauntlet/night2-20260927/cases/<id>.json, trimmed
to the fields `gauntlet.adapter.Observation` reads; `recorded_outcome`/
`recorded_silent_failure` are that same file's own `verdict`. Two
independent observations per example (Z2): the fixture's OWN recorded
verdict (oracle A, darktable's real behaviour as the night-2 gauntlet saw
it) against `evaluate()` run on the SAME observation with the fixed
evaluator (oracle B, this repo's scoring of it) -- neither is derived from
the other; the test asserts they now disagree in the direction the triage
says they should (recorded silent_failure/fail -> fixed verified/pass)."""

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


@pytest.mark.parametrize("case_id", sorted(FIXTURES))
def test_c5_false_positive_now_scores_the_recorded_behaviour_a_pass(case_id):
    fx = FIXTURES[case_id]
    # Oracle A: the night-2 gauntlet's own recorded verdict was a silent
    # failure -- the evaluator, not Scouter, was wrong (row 523, C5).
    assert fx["recorded_outcome"] == "silent_failure"
    assert fx["recorded_silent_failure"].startswith(("a:", "c:", "f:"))
    case = CASES[case_id]
    obs = _build_observation(fx["observation"])
    # Oracle B: the fixed evaluator, replaying the SAME recorded darktable
    # behaviour, now scores it a pass.
    v = evaluate(case, obs, POLICY)
    assert v.score == "pass", (case_id, v.outcome, v.notes, v.silent_failure)
