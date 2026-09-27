"""Runs tests/intent/htc_night2.json (Zenkai Rules Z1) against the real
scripts/htc-night2.sh via subprocess, over a fake repo/runs-root -- two
oracles per example (Z2): the exit code AND the printed stdout content."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "htc-night2.sh"
EXAMPLES = json.loads((Path(__file__).parent / "htc_night2.json").read_text())["examples"]


def git(cwd, *args):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, f"git {args} failed: {p.stdout}{p.stderr}"


class HtcNight2IntentTest(unittest.TestCase):
    def run_example(self, ex: dict) -> None:
        inp, want = ex["input"], ex["expect"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "test@example.com")
            git(repo, "config", "user.name", "Test")
            venv_bin = repo / ".venv" / "bin"
            venv_bin.mkdir(parents=True)
            stub = venv_bin / "python"
            stub.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            stub.chmod(0o755)
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "init")

            runs_root = root / "runs"
            runs_root.mkdir()
            (runs_root / "corpus.json").write_text("{}", encoding="utf-8")
            trial = root / "trial"
            trial.mkdir()

            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            fake_pgrep = fake_bin / "pgrep"
            fake_pgrep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            fake_pgrep.chmod(0o755)

            if inp["pidfile"] is not None:
                night_dir = runs_root / inp["pidfile"]
                night_dir.mkdir(parents=True)
                (night_dir / "runner.pid").write_text(str(os.getpid()), encoding="utf-8")

            env = dict(os.environ)
            env.update({
                "HTC_REPO": str(repo), "HTC_RUNS_ROOT": str(runs_root),
                "HTC_TRIAL": str(trial), "PATH": f"{fake_bin}:{env['PATH']}",
            })
            if inp["stamp_env"]:
                env["STAMP"] = inp["stamp_env"]

            p = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)

        self.assertEqual(p.returncode, want["exit_code"], ex["id"] + "\n" + p.stdout + p.stderr)
        for needle in want.get("stdout_contains", []):
            self.assertIn(needle, p.stdout, ex["id"])
        if "stdout_matches" in want:
            self.assertRegex(p.stdout, want["stdout_matches"], ex["id"])


def _make(ex: dict):
    def test(self: HtcNight2IntentTest) -> None:
        self.run_example(ex)
    return test


for _ex in EXAMPLES:
    setattr(HtcNight2IntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))


if __name__ == "__main__":
    unittest.main()
