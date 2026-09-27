"""Tests for scripts/htc_post_run.sh (the HTC post-run runbook, rows
458/461/463): a fake run dir stands in for a real gauntlet run, and (fix
round 1, review I6) a fake two-commit git repo (HTC_REPO override) stands in
for the real scouter checkout, so these never touch darktable, the real
repo's git history, or the network -- the script itself is invoked for real
via subprocess (no mock of the subject)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "htc_post_run.sh"

DEFAULT_LAYERS = ("offline", "coverage", "scenarios", "bulk", "looks", "robustness",
                  "parity", "install", "guide", "scenarios_agent")


def git(cwd, *args):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, f"git {args} failed: {p.stdout}{p.stderr}"
    return p.stdout.strip()


def run_script(env_extra: dict, args: list[str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(SCRIPT), *(args or [])],
        capture_output=True, text=True, env=env,
    )


class _FakeRepoCase(unittest.TestCase):
    """A real, tiny git repo (two commits: `main`, then a `batch` branch one
    commit ahead) stands in for the scouter checkout, via HTC_REPO -- so
    `merge-base(main, BATCH)` (review I6) is a real, correct computation,
    never a placeholder sha like the old "deadbeef"/"abc123" fixtures (which
    the new merge-base precondition would now, correctly, refuse)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        self.repo = root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "f.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base_sha = git(self.repo, "rev-parse", "HEAD")

        git(self.repo, "checkout", "-q", "-b", "batch")
        (self.repo / "f.txt").write_text("batch\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "batch work")
        self.batch_sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "main")  # back on main, batch unmerged

        # Deliberately no .venv under the fake repo: PY must fall back to
        # the real system python3 (the script's own [[ -x "$PY" ]] || PY=
        # python3 fallback) so progress.json's real JSON parsing runs for
        # real -- only git operations (merge-base, rev-parse) need the fake
        # repo; --execute's GATE_CMD/PYTEST_CMD are always overridden below.

        self.runs_root = root / "gauntlet"
        self.run_dir = self.runs_root / "night1-20260927"
        self.run_dir.mkdir(parents=True)

    def write_progress(self, status: str, layers: dict[str, str] | None = None) -> None:
        if layers is None:
            layers = {name: "done" for name in DEFAULT_LAYERS}
        doc = {"schema": "gauntlet-progress/1", "status": status,
              "layers": {name: {"status": st} for name, st in layers.items()}}
        (self.run_dir / "progress.json").write_text(json.dumps(doc), encoding="utf-8")

    def write_pidfile(self, pid: int) -> Path:
        night_dir = self.runs_root / "night1"
        night_dir.mkdir(parents=True, exist_ok=True)
        pidfile = night_dir / "runner.pid"
        pidfile.write_text(str(pid), encoding="utf-8")
        return pidfile

    def env(self, **extra) -> dict:
        e = {"HTC_REPO": str(self.repo)}
        e.update(extra)
        return e

    def invoke(self, args: list[str] | None = None, **extra_env) -> subprocess.CompletedProcess:
        defaults = {"BATCH": self.batch_sha, "RUN_DIR": str(self.run_dir)}
        defaults.update(extra_env)
        return run_script(self.env(**defaults), args)


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


class PreconditionGateTest(_FakeRepoCase):
    def test_blocks_when_progress_json_missing(self) -> None:
        p = self.invoke()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("BLOCKED", p.stdout)
        self.assertIn("no progress.json", p.stdout)

    def test_blocks_when_progress_not_done(self) -> None:
        self.write_progress("running")
        p = self.invoke()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("BLOCKED", p.stdout)
        self.assertIn("not done", p.stdout)

    def test_blocks_when_runner_pid_is_alive(self) -> None:
        self.write_progress("done")
        # subprocess.run's own PID is guaranteed alive for the duration of
        # this test process, so use ours (os.getpid()) as a live pid.
        self.write_pidfile(os.getpid())
        p = self.invoke()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("BLOCKED", p.stdout)
        self.assertIn("still running", p.stdout)

    def test_passes_with_stale_pidfile(self) -> None:
        # A pidfile naming a pid that is not running (a leftover from a
        # crashed runner) must not block -- "the runner pid is gone" means
        # no LIVE process, not "no pidfile ever existed".
        self.write_progress("done")
        self.write_pidfile(999999999)  # not a real pid
        p = self.invoke()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("runner pid gone", p.stdout)

    def test_passes_and_prints_the_batch_when_both_hold(self) -> None:
        self.write_progress("done")
        p = self.invoke()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = p.stdout
        self.assertIn("ff-merge", out.lower())
        self.assertIn("git", out)
        self.assertIn(f"merge --ff-only {self.batch_sha}", out)
        self.assertIn("push origin main", out)
        self.assertIn("SCOUTER_HOST_TESTS=1", out)
        self.assertIn("tests/test_host_phase3.py::ProbeCandidatePathsHostTest", out)
        self.assertIn("tests/test_host_colorcal.py", out)
        self.assertIn("darkroom_say", out)
        self.assertIn("view_bounce_action", out)
        self.assertIn("make -C", out)
        self.assertIn(f"BASE={self.base_sha}", out)
        self.assertIn(f"HEAD={self.batch_sha}", out)
        self.assertIn("pytest -q -m", out)
        self.assertIn("targeted GAUNTLET rerun", out)
        self.assertIn("gauntlet run --stamp", out)
        self.assertIn("--ids-file", out)

    def test_idempotent_reruns_agree(self) -> None:
        self.write_progress("done")
        first = self.invoke()
        second = self.invoke()
        self.assertEqual(first.returncode, second.returncode)
        self.assertEqual(first.stdout, second.stdout)


class MergeBaseGateTest(_FakeRepoCase):
    """Review I6: BASE is merge-base(main, BATCH), captured before any
    merge -- HEAD is BATCH itself, never a post-merge `git rev-parse HEAD`
    (which would equal BATCH after a ff-merge, an empty, gate-nothing
    range)."""

    def test_base_is_the_real_merge_base_not_batchs_own_head(self) -> None:
        self.write_progress("done")
        p = self.invoke()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(f"BASE={self.base_sha} HEAD={self.batch_sha}", p.stdout)
        self.assertNotEqual(self.base_sha, self.batch_sha)

    def test_refuses_when_batch_is_already_on_main(self) -> None:
        self.write_progress("done")
        # Fast-forward main to batch: merge-base(main, BATCH) == BATCH now.
        git(self.repo, "merge", "--ff-only", self.batch_sha)
        p = self.invoke()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("BLOCKED", p.stdout)
        self.assertIn("nothing to gate", p.stdout)

    def test_refuses_when_batch_is_not_a_real_ref(self) -> None:
        self.write_progress("done")
        p = self.invoke(BATCH="not-a-real-sha-at-all")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("BLOCKED", p.stdout)
        self.assertIn("could not compute merge-base", p.stdout)


class ExpectedLayersTest(_FakeRepoCase):
    """Review I8: `progress.json.status == "done"` is not sufficient -- it
    is also written when an individual layer ended `stopped_crash`, and the
    two-pass launchers write it once after pass 1 alone, before pass 2
    (scenarios_agent) ever starts. Every expected layer must itself show a
    terminal "done" status."""

    def test_blocks_when_scenarios_agent_has_not_finished_yet(self) -> None:
        layers = {name: "done" for name in DEFAULT_LAYERS if name != "scenarios_agent"}
        layers["scenarios_agent"] = "pending"
        self.write_progress("done", layers)
        p = self.invoke()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("BLOCKED", p.stdout)
        self.assertIn("scenarios_agent", p.stdout)

    def test_blocks_when_a_layer_crashed_even_though_doc_status_is_done(self) -> None:
        layers = {name: "done" for name in DEFAULT_LAYERS}
        layers["looks"] = "stopped_crash"
        self.write_progress("done", layers)
        p = self.invoke()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("looks", p.stdout)

    def test_passes_when_every_expected_layer_is_done(self) -> None:
        self.write_progress("done")
        p = self.invoke()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_expected_layers_overridable_for_a_smaller_smoke_run(self) -> None:
        self.write_progress("done", {"offline": "done"})
        p = self.invoke(HTC_EXPECTED_LAYERS="offline")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


class LiveRunnerRefusalTest(_FakeRepoCase):
    """Review I8: the pidfile precondition only tracks the inner
    `gauntlet run` subprocess, which the launcher deletes BETWEEN pass 1
    and pass 2 -- a direct pgrep for the actual command line catches an
    actively running pass regardless of pidfile state."""

    def test_blocks_when_a_runner_for_this_stamp_is_live(self) -> None:
        self.write_progress("done")
        fake_bin = Path(self.tmp.name) / "fake-bin"
        fake_bin.mkdir()
        fake_pgrep = fake_bin / "pgrep"
        fake_pgrep.write_text(
            "#!/usr/bin/env bash\n"
            "case \"$*\" in\n"
            "  *'run --stamp'*) exit 0 ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n",
            encoding="utf-8")
        fake_pgrep.chmod(0o755)
        env = dict(os.environ)
        p = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True,
            env={**env, **self.env(BATCH=self.batch_sha, RUN_DIR=str(self.run_dir)),
                "PATH": f"{fake_bin}:{env['PATH']}"})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("BLOCKED", p.stdout)
        self.assertIn("still running", p.stdout)


class ExecuteModeTest(_FakeRepoCase):
    """--execute must run only the merge-time gate (mutation check via
    GATE_CMD, then pytest via PYTEST_CMD) -- merge/push/host-probes/
    stale-history/targeted-rerun stay print-only even then."""

    def test_execute_runs_the_overridden_gate_then_pytest_commands(self) -> None:
        self.write_progress("done")
        marker = Path(self.tmp.name) / "gate-ran.marker"
        pytest_marker = Path(self.tmp.name) / "pytest-ran.marker"
        p = self.invoke(
            ["--execute"],
            GATE_CMD=f"echo mock-gate-ran > {marker}",
            PYTEST_CMD=f"echo mock-pytest-ran > {pytest_marker}",
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(marker.exists(), "GATE_CMD override was not run")
        self.assertEqual(marker.read_text().strip(), "mock-gate-ran")
        self.assertTrue(pytest_marker.exists(), "PYTEST_CMD override was not run")
        self.assertEqual(pytest_marker.read_text().strip(), "mock-pytest-ran")
        # steps 1-3 and 5 are still only printed, never actually run
        self.assertIn("never run by this script", p.stdout)

    def test_execute_propagates_a_failing_gate_and_skips_pytest(self) -> None:
        self.write_progress("done")
        pytest_marker = Path(self.tmp.name) / "pytest-ran.marker"
        p = self.invoke(["--execute"], GATE_CMD="exit 1",
                     PYTEST_CMD=f"echo should-not-run > {pytest_marker}")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertFalse(pytest_marker.exists(), "pytest ran despite a failing gate")

    def test_execute_propagates_a_failing_pytest(self) -> None:
        self.write_progress("done")
        p = self.invoke(["--execute"], GATE_CMD="exit 0", PYTEST_CMD="exit 1")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)

    def test_execute_still_blocked_by_preconditions(self) -> None:
        self.write_progress("running")
        p = self.invoke(["--execute"], GATE_CMD="echo should-not-run")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("should-not-run", p.stdout)


if __name__ == "__main__":
    unittest.main()
