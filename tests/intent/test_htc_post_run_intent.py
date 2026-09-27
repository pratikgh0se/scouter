"""Runs tests/intent/htc_post_run.json (Zenkai Rules Z1) against the real
scripts/htc_post_run.sh via subprocess, over a fake run dir -- two oracles
per example (Z2): the exit code AND the printed stdout content."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "htc_post_run.sh"
EXAMPLES = json.loads((Path(__file__).parent / "htc_post_run.json").read_text())["examples"]


class HtcPostRunIntentTest(unittest.TestCase):
    def run_example(self, ex: dict) -> None:
        inp, want = ex["input"], ex["expect"]
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp) / "gauntlet"
            run_dir = runs_root / "night1-20260927"
            run_dir.mkdir(parents=True)
            (run_dir / "progress.json").write_text(
                json.dumps({"status": inp["progress_status"]}), encoding="utf-8")
            if inp["pidfile"] == "self":
                night_dir = runs_root / "night1"
                night_dir.mkdir(parents=True, exist_ok=True)
                (night_dir / "runner.pid").write_text(str(os.getpid()), encoding="utf-8")

            p = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                              env={**os.environ, "BATCH": "deadbeef", "RUN_DIR": str(run_dir)})

        self.assertEqual(p.returncode, want["exit_code"], ex["id"] + "\n" + p.stdout + p.stderr)
        for needle in want["stdout_contains"]:
            self.assertIn(needle, p.stdout, ex["id"])


def _make(ex: dict):
    def test(self: HtcPostRunIntentTest) -> None:
        self.run_example(ex)
    return test


for _ex in EXAMPLES:
    setattr(HtcPostRunIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))


if __name__ == "__main__":
    unittest.main()
