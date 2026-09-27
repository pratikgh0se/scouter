"""Runs tests/intent/usage_meter_park.json (Zenkai Rules Z1). Two oracles per
example (Z2): whether Park was raised, and (when it was) that `until` equals
the window's own `reset_at` (tests/gauntlet/agent_budget.py)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import pytest

from gauntlet.adapter import Park
from tests.gauntlet import agent_budget

EXAMPLES = json.loads((Path(__file__).parent / "usage_meter_park.json").read_text())["examples"]


class UsageMeterParkIntentTest(unittest.TestCase):
    def run_example(self, ex: dict, tmp_path: Path) -> None:
        inp, want = ex["input"], ex["expect"]
        now = 1_000_000.0
        path = tmp_path / f"{ex['id']}-usage-window.json"
        path.write_text(json.dumps({
            "utilization": inp["utilization"],
            "reset_at": now + inp["reset_at_offset_s"],
            "written_at": now + inp["written_at_offset_s"],
        }))
        real_path, agent_budget.USAGE_WINDOW_PATH = agent_budget.USAGE_WINDOW_PATH, path
        try:
            usage_fn = lambda: agent_budget.usage_fraction(now=now)  # noqa: E731
            if want["parked"]:
                with pytest.raises(Park, match="usage_meter") as exc_info:
                    agent_budget.check_usage_budget(usage_fn=usage_fn)
                got_until = exc_info.value.until - now
                self.assertAlmostEqual(got_until, want["until_offset_s"], msg=ex["id"])
            else:
                agent_budget.check_usage_budget(usage_fn=usage_fn)  # must not raise
        finally:
            agent_budget.USAGE_WINDOW_PATH = real_path


def _make(ex: dict):
    def test(self: UsageMeterParkIntentTest) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            self.run_example(ex, Path(d))
    return test


for _ex in EXAMPLES:
    setattr(UsageMeterParkIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))
