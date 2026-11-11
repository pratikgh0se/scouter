# mutation-check: targets-post-date-range -- the disk guard is new in this
# range (card t_90e75c20), so no marker is needed; this test exercises it
# directly against gauntlet/runner.py.
"""Runs tests/intent/disk_guard.json (Zenkai Rules Z1). Two oracles per
example (Z2): the run's own status, and whether the first case in the layer
got a written result at all (gauntlet/runner.py's disk guard)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from gauntlet.generate import write_generation
from gauntlet.runner import Runner
from gauntlet.store import RunDir
from gauntlet.tests.toy import ToyAdapter

EXAMPLES = json.loads((Path(__file__).parent / "disk_guard.json").read_text())["examples"]


class DiskGuardIntentTest(unittest.TestCase):
    def run_example(self, ex: dict, tmp_path: Path) -> None:
        inp, want = ex["input"], ex["expect"]
        adapter = ToyAdapter()
        run = RunDir(tmp_path, "s")
        write_generation(run, adapter)
        summary = Runner(adapter, run,
                         free_disk_gb=lambda root: inp["free_gb"]).run_layers(layers=["alpha"])
        # Oracle 1: the run's own status.
        self.assertEqual(summary.status, want["status"], ex["id"])
        # Oracle 2: the first case in the layer either got a result or didn't.
        has_result = run.read_result("alpha-0") is not None
        self.assertEqual(has_result, want["first_case_has_result"], ex["id"])


def _make(ex: dict):
    def test(self: DiskGuardIntentTest) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            self.run_example(ex, Path(d))
    return test


for _ex in EXAMPLES:
    setattr(DiskGuardIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))
