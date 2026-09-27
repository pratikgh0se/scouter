"""Tests for scripts/htc_post_run.sh (the HTC post-run runbook, rows
458/461/463): a fake run dir stands in for a real gauntlet run so these
never touch darktable, the real repo's git history, or the network -- the
script itself is invoked for real via subprocess (no mock of the subject)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "htc_post_run.sh"


def run_script(env_extra: dict, args: list[str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(SCRIPT), *(args or [])],
        capture_output=True, text=True, env=env,
    )


class _FakeRunDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs_root = Path(self.tmp.name) / "gauntlet"
        self.run_dir = self.runs_root / "night1-20260927"
        self.run_dir.mkdir(parents=True)

    def write_progress(self, status: str) -> None:
        (self.run_dir / "progress.json").write_text(
            json.dumps({"schema": "gauntlet-progress/1", "status": status}),
            encoding="utf-8",
        )

    def write_pidfile(self, pid: int) -> Path:
        night_dir = self.runs_root / "night1"
        night_dir.mkdir(parents=True, exist_ok=True)
        pidfile = night_dir / "runner.pid"
        pidfile.write_text(str(pid), encoding="utf-8")
        return pidfile


class UsageErrorsTest(unittest.TestCase):
    def test_missing_batch_and_run_dir_exits_2(self) -> None:
        p = run_script({"BATCH": "", "RUN_DIR": ""})
        self.assertEqual(p.returncode, 2, p.stderr)

    def test_unknown_flag_exits_2(self) -> None:
        p = run_script({"BATCH": "abc123", "RUN_DIR": "/tmp/does-not-matter"},
                        ["--bogus"])
        self.assertEqual(p.returncode, 2, p.stderr)

    def test_missing_run_dir_directory_exits_2(self) -> None:
        p = run_script({"BATCH": "abc123", "RUN_DIR": "/no/such/path/at/all"})
        self.assertEqual(p.returncode, 2, p.stderr)


class PreconditionGateTest(_FakeRunDirCase):
    def test_blocks_when_progress_json_missing(self) -> None:
        p = run_script({"BATCH": "abc123", "RUN_DIR": str(self.run_dir)})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("BLOCKED", p.stdout)
        self.assertIn("no progress.json", p.stdout)

    def test_blocks_when_progress_not_done(self) -> None:
        self.write_progress("running")
        p = run_script({"BATCH": "abc123", "RUN_DIR": str(self.run_dir)})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("BLOCKED", p.stdout)
        self.assertIn("not done", p.stdout)

    def test_blocks_when_runner_pid_is_alive(self) -> None:
        self.write_progress("done")
        # subprocess.run's own PID is guaranteed alive for the duration of
        # this test process, so use ours (os.getpid()) as a live pid.
        self.write_pidfile(os.getpid())
        p = run_script({"BATCH": "abc123", "RUN_DIR": str(self.run_dir)})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("BLOCKED", p.stdout)
        self.assertIn("still running", p.stdout)

    def test_passes_with_stale_pidfile(self) -> None:
        # A pidfile naming a pid that is not running (a leftover from a
        # crashed runner) must not block -- "the runner pid is gone" means
        # no LIVE process, not "no pidfile ever existed".
        self.write_progress("done")
        self.write_pidfile(999999999)  # not a real pid
        p = run_script({"BATCH": "abc123", "RUN_DIR": str(self.run_dir)})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("runner pid gone", p.stdout)

    def test_passes_and_prints_the_batch_when_both_hold(self) -> None:
        self.write_progress("done")
        p = run_script({"BATCH": "deadbeef", "RUN_DIR": str(self.run_dir)})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = p.stdout
        self.assertIn("ff-merge", out.lower())
        self.assertIn("git", out)
        self.assertIn("merge --ff-only deadbeef", out)
        self.assertIn("push origin main", out)
        self.assertIn("SCOUTER_HOST_TESTS=1", out)
        self.assertIn("tests/test_host_phase3.py::ProbeCandidatePathsHostTest", out)
        self.assertIn("tests/test_host_colorcal.py", out)
        self.assertIn("darkroom_say", out)
        self.assertIn("view_bounce_action", out)
        self.assertIn("make -C", out)
        self.assertIn("BASE=deadbeef", out)

    def test_idempotent_reruns_agree(self) -> None:
        self.write_progress("done")
        first = run_script({"BATCH": "deadbeef", "RUN_DIR": str(self.run_dir)})
        second = run_script({"BATCH": "deadbeef", "RUN_DIR": str(self.run_dir)})
        self.assertEqual(first.returncode, second.returncode)
        self.assertEqual(first.stdout, second.stdout)


class ExecuteModeTest(_FakeRunDirCase):
    """--execute must run only the gate rerun (via the overridable GATE_CMD);
    merge/push/host-probes/stale-history stay print-only even then."""

    def test_execute_runs_only_the_overridden_gate_command(self) -> None:
        self.write_progress("done")
        marker = Path(self.tmp.name) / "gate-ran.marker"
        fake_gate = f"echo mock-gate-ran > {marker}"
        p = run_script({"BATCH": "deadbeef", "RUN_DIR": str(self.run_dir),
                        "GATE_CMD": fake_gate}, ["--execute"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(marker.exists(), "GATE_CMD override was not run")
        self.assertEqual(marker.read_text().strip(), "mock-gate-ran")
        # steps 1-3 are still only printed, never actually run
        self.assertIn("never run by this script", p.stdout)

    def test_execute_propagates_a_failing_gate(self) -> None:
        self.write_progress("done")
        p = run_script({"BATCH": "deadbeef", "RUN_DIR": str(self.run_dir),
                        "GATE_CMD": "exit 1"}, ["--execute"])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)

    def test_execute_still_blocked_by_preconditions(self) -> None:
        self.write_progress("running")
        p = run_script({"BATCH": "deadbeef", "RUN_DIR": str(self.run_dir),
                        "GATE_CMD": "echo should-not-run"}, ["--execute"])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("should-not-run", p.stdout)


if __name__ == "__main__":
    unittest.main()
