"""Runs tests/intent/clamped_move.json (Zenkai Rules Z1). Two oracles per
example (Z2): the call's own typed envelope fields (`outcome`/`cap`,
scouter/core/moves.py, scouter/types.py:AdjustResult) and the rendered
`summary` text an operator or agent actually reads."""

from __future__ import annotations

import json
from pathlib import Path

from scouter.core import moves
from tests.test_moves import _MovesFixture

EXAMPLES = json.loads((Path(__file__).parent / "clamped_move.json").read_text())["examples"]


class ClampedMoveIntentTest(_MovesFixture):
    def run_example(self, ex: dict) -> None:
        inp, want = ex["input"], ex["expect"]
        r = moves.adjust.__wrapped__(
            inp["module"], inp["param"], inp["delta"], commit=False, job="job-test"
        )
        self.assertEqual(r.outcome, want["outcome"], ex["id"])
        self.assertEqual(r.cap, want["cap"], ex["id"])
        if want["summary_contains"]:
            self.assertIn(want["summary_contains"], r.summary, ex["id"])
        else:
            self.assertNotIn("moved to the limit", r.summary, ex["id"])


def _make(ex: dict):
    def test(self: ClampedMoveIntentTest) -> None:
        self.run_example(ex)

    return test


for _ex in EXAMPLES:
    setattr(ClampedMoveIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))
