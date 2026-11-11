"""Runs tests/intent/weaker_grade_noop.json (Zenkai Rules Z1). Two oracles per
example (Z2): the phrase's own top-level `noop` field (`SayResult.noop`,
scouter/types.py) and the FakeBridge's own recorded `darkroom.action` call
count -- an honest noop sends nothing at all."""

from __future__ import annotations

import json
from pathlib import Path

from scouter.core import moves
from tests.test_moves import _MovesFixture

EXAMPLES = json.loads((Path(__file__).parent / "weaker_grade_noop.json").read_text())["examples"]


class WeakerGradeNoopIntentTest(_MovesFixture):
    def run_example(self, ex: dict) -> None:
        inp, want = ex["input"], ex["expect"]
        if inp["precondition"]:
            moves.say.__wrapped__(inp["precondition"], commit=False, job="job-test")
        before_sends = len(self.sets())
        r = moves.say.__wrapped__(inp["phrase"], commit=False, job="job-test")
        self.assertEqual(r.outcome, want["outcome"], ex["id"])
        self.assertEqual(r.noop, want["noop"], ex["id"])
        self.assertEqual(len(self.sets()) - before_sends, want["sends"], ex["id"])
        self.assertEqual(all(m["noop"] for m in r.moves), want["every_move_noop"], ex["id"])


def _make(ex: dict):
    def test(self: WeakerGradeNoopIntentTest) -> None:
        self.run_example(ex)
    return test


for _ex in EXAMPLES:
    setattr(WeakerGradeNoopIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))
