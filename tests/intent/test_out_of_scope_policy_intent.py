"""Runs tests/intent/out_of_scope_policy.json (Zenkai Rules Z1). Two oracles
per example (Z2): the verdict's `score` and its `outcome` label.

Fix round 1 (Opus review finding 6): the case now comes from the REAL
generator (tests.gauntlet.layers.scenarios.agent_case, on r0019, an actual
"out" request) instead of a hand-built Case with the policy meta typed in by
this test file -- reverting scenarios.py's item G change (the
out_of_scope_policy/substitute_ok meta) or evaluate.py's unrequested_edit
change now fails this test, not only test_layer_scenarios.py's generator
test."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from gauntlet.adapter import Observation
from gauntlet.evaluate import evaluate
from tests.gauntlet import taxonomy
from tests.gauntlet import checkmap
from tests.gauntlet.layers import scenarios
from tests.gauntlet.policy import POLICY

EXAMPLES = json.loads((Path(__file__).parent / "out_of_scope_policy.json").read_text())["examples"]


def _real_out_of_scope_case():
    tax = taxonomy.load()
    req = next(r for r in tax.requests if r["id"] == "r0019")
    plan = checkmap.plan_request(req, tax.check_map)
    assert plan.klass == "out", "r0019 must still be an out-of-scope request"
    return scenarios.agent_case(req, plan, tax, "P0")


class OutOfScopePolicyIntentTest(unittest.TestCase):
    def run_example(self, ex: dict) -> None:
        inp, want = ex["input"], ex["expect"]
        case = _real_out_of_scope_case()
        obs = Observation(agent_text=inp["agent_text"],
                          before={"fingerprint": "a"},
                          after={"fingerprint": "b" if inp["wrote"] else "a"})
        v = evaluate(case, obs, POLICY)
        self.assertEqual(v.score, want["status"], ex["id"])
        self.assertEqual(v.outcome, want["outcome"], ex["id"])


def _make(ex: dict):
    def test(self: OutOfScopePolicyIntentTest) -> None:
        self.run_example(ex)
    return test


for _ex in EXAMPLES:
    setattr(OutOfScopePolicyIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))
