"""Tests for scripts/htc-night2.sh: argument/env handling (STAMP) and the
pid-alive refusal, against a fake repo and a fake runs root (HTC_REPO /
HTC_RUNS_ROOT overrides) -- never the real main checkout, never darktable,
never the real gauntlet corpus. The script itself is run for real via
subprocess (no mock of the subject); a stub `.venv/bin/python` stands in
for the real interpreter so the run stops right after logging its stamp,
before anything gauntlet- or darktable-shaped would happen."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "htc-night2.sh"


def git(cwd, *args):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, f"git {args} failed: {p.stdout}{p.stderr}"
    return p.stdout


class _FakeReposCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        self.repo = root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "README.md").write_text("x", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")

        venv_bin = self.repo / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        stub = venv_bin / "python"
        # Stands in for the real interpreter: "generate" always fails, so the
        # script dies with exit 2 right after logging the stamp -- no real
        # gauntlet module, no darktable, ever runs.
        stub.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        stub.chmod(0o755)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "add stub venv")

        self.runs_root = root / "runs"
        self.runs_root.mkdir()
        (self.runs_root / "corpus.json").write_text("{}", encoding="utf-8")

        self.trial = root / "trial"
        self.trial.mkdir()

        # The script's very first precondition is "no darktable running
        # anywhere on the machine" (pgrep -x/-f darktable) -- a real check
        # that must stay untouched, but this machine may have a REAL,
        # unrelated live gauntlet run's darktable up at any time (this repo
        # is shared with the live release gauntlet). A fake `pgrep` ahead
        # on PATH that always reports "no match" isolates these tests from
        # that, without weakening or overriding anything the real script
        # does -- it's a test double for a shell dependency, not the
        # subject, and every other command (git, python, find, stat, cmp,
        # df) still runs for real.
        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        fake_pgrep = fake_bin / "pgrep"
        fake_pgrep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        fake_pgrep.chmod(0o755)
        self.fake_bin = fake_bin

    def env(self, **extra) -> dict:
        e = dict(os.environ)
        e.update({
            "HTC_REPO": str(self.repo),
            "HTC_RUNS_ROOT": str(self.runs_root),
            "HTC_TRIAL": str(self.trial),
            "PATH": f"{self.fake_bin}:{e['PATH']}",
        })
        e.update(extra)
        return e

    def run_script(self, **extra_env) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                              env=self.env(**extra_env))


class ArgumentHandlingTest(_FakeReposCase):
    def test_custom_stamp_env_is_honored(self) -> None:
        p = self.run_script(STAMP="night2-19991231")
        self.assertIn("stamp night2-19991231", p.stdout)
        self.assertEqual((self.runs_root / "night2" / "CURRENT_STAMP").read_text().strip(),
                          "night2-19991231")
        # dies at the (stubbed) generate step, never touching darktable
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)

    def test_default_stamp_matches_night2_dateformat(self) -> None:
        p = self.run_script()
        m = re.search(r"stamp (night2-\d{8})", p.stdout)
        self.assertIsNotNone(m, p.stdout + p.stderr)

    def test_resume_reuses_the_current_stamp(self) -> None:
        night2 = self.runs_root / "night2"
        night2.mkdir(parents=True)
        (night2 / "CURRENT_STAMP").write_text("night2-20260101\n", encoding="utf-8")
        p = self.run_script()
        self.assertIn("stamp night2-20260101", p.stdout)


class PidAliveRefusalTest(_FakeReposCase):
    def test_refuses_while_its_own_runner_pid_is_alive(self) -> None:
        night2 = self.runs_root / "night2"
        night2.mkdir(parents=True)
        (night2 / "runner.pid").write_text(str(os.getpid()), encoding="utf-8")
        p = self.run_script()
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertIn("night-2 runner is already running", p.stdout)

    def test_refuses_while_a_night1_runner_pid_is_alive(self) -> None:
        night1 = self.runs_root / "night1"
        night1.mkdir(parents=True)
        (night1 / "runner.pid").write_text(str(os.getpid()), encoding="utf-8")
        p = self.run_script()
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertIn("night-1 runner is still running", p.stdout)

    def test_stale_pidfile_does_not_refuse(self) -> None:
        night2 = self.runs_root / "night2"
        night2.mkdir(parents=True)
        (night2 / "runner.pid").write_text("999999999", encoding="utf-8")  # not a real pid
        p = self.run_script()
        # Gets past the pid check and on to the (stubbed) generate failure.
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertNotIn("is already running", p.stdout)


if __name__ == "__main__":
    unittest.main()
