"""Runs tests/intent/htc_post_run.json (Zenkai Rules Z1) against the real
scripts/htc_post_run.sh via subprocess, over a fake run dir AND (fix round
1, review I6) a fake two-commit git repo (HTC_REPO override) standing in for
the real scouter checkout, so the merge-base precondition it added is a
real, correct git computation rather than a placeholder sha -- two oracles
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

DEFAULT_LAYERS = (
    "offline",
    "coverage",
    "scenarios",
    "bulk",
    "looks",
    "robustness",
    "parity",
    "install",
    "guide",
    "scenarios_agent",
)


def _git(cwd, *args):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, f"git {args} failed: {p.stdout}{p.stderr}"
    return p.stdout.strip()


def _make_fake_repo(root: Path) -> str:
    """A real two-commit repo (main, then a batch branch one commit ahead)
    so `merge-base(main, BATCH)` is a real computation. Returns the batch
    branch's sha."""
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "checkout", "-q", "-b", "batch")
    (repo / "f.txt").write_text("batch\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "batch work")
    batch_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return str(repo), batch_sha


class HtcPostRunIntentTest(unittest.TestCase):
    def run_example(self, ex: dict) -> None:
        inp, want = ex["input"], ex["expect"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, batch_sha = _make_fake_repo(root)
            runs_root = root / "gauntlet"
            run_dir = runs_root / "night1-20260927"
            run_dir.mkdir(parents=True)
            layers = (
                {name: {"status": "done"} for name in DEFAULT_LAYERS}
                if inp["progress_status"] == "done"
                else {}
            )
            (run_dir / "progress.json").write_text(
                json.dumps({"status": inp["progress_status"], "layers": layers}), encoding="utf-8"
            )
            if inp["pidfile"] == "self":
                night_dir = runs_root / "night1"
                night_dir.mkdir(parents=True, exist_ok=True)
                (night_dir / "runner.pid").write_text(str(os.getpid()), encoding="utf-8")

            p = subprocess.run(
                ["bash", str(SCRIPT)],
                capture_output=True,
                text=True,
                env={**os.environ, "BATCH": batch_sha, "RUN_DIR": str(run_dir), "HTC_REPO": repo},
            )

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
