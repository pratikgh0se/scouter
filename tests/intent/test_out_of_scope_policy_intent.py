"""Runs tests/intent/out_of_scope_policy.json (Zenkai Rules Z1). Two oracles
per example (Z2): the verdict's `status` and its `outcome` label
(gauntlet/evaluate.py:evaluate, unchanged by row 501 item G -- only the
case's own meta is now explicit)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from gauntlet.adapter import Observation
from gauntlet.case import Case, Expect
from gauntlet.evaluate import evaluate
from tests.gauntlet.policy import POLICY

EXAMPLES = json.loads((Path(__file__).parent / "out_of_scope_policy.json").read_text())["examples"]


def _case() -> Case:
    return Case(id="sca-intent-out", layer="scenarios_agent", tier="P0", mode="agent",
               steps=[], expect=Expect("refuse"), body="olympus", frame_role="hero",
               meta={"sentence": "sharpen it a touch", "followups": [],
                     "out_of_scope_policy": "refuse_and_offer_only", "substitute_ok": False,
                     "unrequested_write_reason": "unrequested_edit"})


class OutOfScopePolicyIntentTest(unittest.TestCase):
    def run_example(self, ex: dict) -> None:
        inp, want = ex["input"], ex["expect"]
        obs = Observation(agent_text=inp["agent_text"],
                          before={"fingerprint": "a"},
                          after={"fingerprint": "b" if inp["wrote"] else "a"})
        v = evaluate(_case(), obs, POLICY)
        self.assertEqual(v.score, want["status"], ex["id"])
        self.assertEqual(v.outcome, want["outcome"], ex["id"])


def _make(ex: dict):
    def test(self: OutOfScopePolicyIntentTest) -> None:
        self.run_example(ex)
    return test


for _ex in EXAMPLES:
    setattr(OutOfScopePolicyIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))
