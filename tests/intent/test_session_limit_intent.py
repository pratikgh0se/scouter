"""Runs tests/intent/session_limit.json (Zenkai Rules Z1). Two oracles per
example (Z2): parse_stream's own `limit` field and the reset time
`reset_time` decodes from it (tests/gauntlet/agent.py)."""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path

from tests.gauntlet import agent

EXAMPLES = json.loads((Path(__file__).parent / "session_limit.json").read_text())["examples"]


class SessionLimitIntentTest(unittest.TestCase):
    def run_example(self, ex: dict) -> None:
        inp, want = ex["input"], ex["expect"]
        parsed = agent.parse_stream([json.dumps({"type": "result", **inp})])
        self.assertEqual(bool(parsed["limit"]), want["limit_set"], ex["id"])
        if want["reset_hour"] is None:
            self.assertIsNone(parsed["limit"])
        else:
            now = datetime(2026, 9, 27, 13, 51)
            got = datetime.fromtimestamp(agent.reset_time(parsed["limit"], now))
            self.assertEqual(got.hour, want["reset_hour"], ex["id"])


def _make(ex: dict):
    def test(self: SessionLimitIntentTest) -> None:
        self.run_example(ex)

    return test


for _ex in EXAMPLES:
    setattr(SessionLimitIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))
