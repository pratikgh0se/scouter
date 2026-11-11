"""Tests for scripts/htc_post_run.sh (the HTC post-run runbook, rows
458/461/463): a fake run dir stands in for a real gauntlet run, and (fix
round 1, review I6) a fake two-commit git repo (HTC_REPO override) stands in
for the real scouter checkout, so these never touch darktable, the real
repo's git history, or the network -- the script itself is invoked for real
via subprocess (no mock of the subject)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "htc_post_run.sh"

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


def git(cwd, *args):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, f"git {args} failed: {p.stdout}{p.stderr}"
    return p.stdout.strip()


def run_script(env_extra: dict, args: list[str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(SCRIPT), *(args or [])],
        capture_output=True,
        text=True,
        env=env,
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
        # Row 495/496: a unique, per-test stamp (uuid), not a fixed
        # "night1-20260927" shared by every test in this class -- the
        # precondition check does a HOST-WIDE `pgrep -f "run --stamp
        # <stamp>"` (unfaked in this class, unlike LiveRunnerRefusalTest),
        # so a fixed, date-shaped stamp can collide with a real, unrelated
        # gauntlet runner actually live on a busy shared machine (observed:
        # keeper-log.md, three separate transient pgrep hits on
        # night1-20260927 across three reruns). A uuid-suffixed stamp is
        # never a real run's stamp.
        self.stamp = f"night1-test-{uuid.uuid4().hex[:12]}"
        self.run_dir = self.runs_root / self.stamp
        self.run_dir.mkdir(parents=True)

    def write_progress(self, status: str, layers: dict[str, str] | None = None) -> None:
        if layers is None:
            layers = {name: "done" for name in DEFAULT_LAYERS}
        doc = {
            "schema": "gauntlet-progress/1",
            "status": status,
            "layers": {name: {"status": st} for name, st in layers.items()},
        }
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
        p = run_script({"BATCH": "abc123", "RUN_DIR": "/tmp/does-not-matter"}, ["--bogus"])
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

    def _spawn_foreign_process(self, argv_text: str) -> subprocess.Popen:
        """A real process (fix round 1, finding 9: via `sys.executable`, not
        a bare `python3` off PATH) whose command line contains `argv_text`
        -- same spawn pattern as
        `tests/test_htc_night2.py::GauntletRunnerRefusalTest::
        _spawn_dummy_launcher`. Waits for real `pgrep -f` to see it before
        returning, and the process is killed AND reaped in cleanup (finding
        9: `proc.wait()` after `kill`, so it never lingers as a zombie)."""
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                f"import time  # stand-in for the gauntlet launcher's own argv\n"
                f"# {argv_text}\n"
                "time.sleep(20)",
            ]
        )

        def _cleanup() -> None:
            proc.kill()
            proc.wait()

        self.addCleanup(_cleanup)
        deadline = time.monotonic() + 5.0
        needle = argv_text.split(" --resume")[0]  # "run --stamp <stamp>"
        while time.monotonic() < deadline:
            found = subprocess.run(["pgrep", "-f", needle], capture_output=True).returncode == 0
            if found:
                break
            time.sleep(0.05)
        else:
            self.fail("dummy foreign process never became visible to pgrep")
        return proc

    def test_a_foreign_process_whose_argv_contains_another_runs_stamp_text_is_not_matched(
        self,
    ) -> None:
        """Row 495/496: `pgrep -f` does a plain substring/regex search, so
        an UNANCHORED `run --stamp $STAMP_NAME` would false-positive when
        this run's own stamp is a textual PREFIX of a different, unrelated
        run's (longer) stamp -- exactly the shape a uuid-suffixed test
        stamp (`self.stamp`) and a copy of it with extra text tacked on
        recreate. Spawns a REAL foreign process whose argv is
        `... run --stamp <self.stamp>-OTHERRUN --resume ...` -- text that
        trivially CONTAINS `self.stamp` -- and proves the precondition for
        `self.stamp` itself does NOT block on it. Paired with the positive
        case right below: the same machinery DOES block on an exact match."""
        self.write_progress("done")
        foreign_stamp = f"{self.stamp}-OTHERRUN"
        self._spawn_foreign_process(f"run --stamp {foreign_stamp} --resume --wait-on-park")

        p = self.invoke()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("no live runner", p.stdout)
        self.assertNotIn("BLOCKED", p.stdout)

    def test_a_foreign_process_running_this_exact_stamp_is_matched_and_blocks(self) -> None:
        """Fix round 1, finding 7 (reviewer): the anchored regex has no
        POSITIVE real-pgrep test -- an over-anchored pattern (e.g. matching
        only end-of-argv) would pass every existing test here while missing
        the real launcher's argv (which has more text, `--resume ...`,
        after the stamp) and silently never blocking a live run. Spawns a
        real foreign process for THIS test's own stamp and proves it DOES
        block."""
        self.write_progress("done")
        self._spawn_foreign_process(f"run --stamp {self.stamp} --resume --wait-on-park")

        p = self.invoke()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("BLOCKED", p.stdout)
        self.assertIn(f"stamp {self.stamp}", p.stdout)

    def test_pgrep_override_is_honoured(self) -> None:
        """Fix round 1, finding 6 (reviewer): `$PGREP` was added but never
        exercised by a test -- a script that silently hard-codes `pgrep`
        again would still pass every other test here. A fake `PGREP`
        pointed to directly (not via a PATH trick, unlike
        `LiveRunnerRefusalTest`) that always reports a match must BLOCK."""
        self.write_progress("done")
        fake_pgrep = Path(self.tmp.name) / "fake-pgrep-direct"
        fake_pgrep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        fake_pgrep.chmod(0o755)
        p = self.invoke(PGREP=str(fake_pgrep))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("BLOCKED", p.stdout)
        self.assertIn(f"stamp {self.stamp}", p.stdout)

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
        self.assertIn("tests/test_host_phase3.py::ProbeExposureBlackDefaultHostTest", out)
        self.assertIn("tests/test_host_colorcal.py", out)
        self.assertIn("darkroom_say", out)
        self.assertIn("view_bounce_action", out)
        self.assertIn("tests/mutation_check.py", out)
        self.assertNotIn("make -C", out)  # R3b: not `make gate` as a real command
        self.assertIn(f"--base {self.base_sha}", out)
        self.assertIn(f"--head {self.batch_sha}", out)
        self.assertIn("--repo", out)
        self.assertIn("pytest -q -m", out)
        self.assertIn("targeted GAUNTLET rerun", out)
        self.assertIn("gauntlet run --stamp", out)
        self.assertIn("--ids-file", out)

    def test_probe_commands_carry_m_host_exactly_where_the_marker_is(self) -> None:
        """Row 495/496: the two printed `tests/test_host_phase3.py` probe
        classes carry `@pytest.mark.host` -- `pyproject.toml`'s
        `addopts = "-m 'not host'"` deselects them by default, so their
        printed command needs an explicit `-m host` or a copy-paste of the
        runbook silently runs nothing (exit 5, deselected). `test_host_
        colorcal.py` gates via `@unittest.skipUnless`, no pytest marker, so
        `-m host` there would wrongly deselect IT instead. Verified two
        ways (oracle 1: the real marker, read by collecting the repo's own
        test files with `-m host`; oracle 2: the printed command text).

        Fix round 1, finding 8 (reviewer): collecting `test_host_colorcal.py`
        under `-m host` and finding no `::` lines is ALSO exactly what a
        broken collection (e.g. a syntax error introduced elsewhere in that
        file) looks like -- a vacuous pass, not evidence the file has no
        `@pytest.mark.host`. Pytest's own exit code tells the two apart: 0
        (some tests ran) or 5 (collected zero, cleanly) are both healthy;
        anything else (2: collection error) is not -- asserted here, plus a
        positive control (collecting colorcal with no `-m` at all must find
        real tests, so collection isn't broken some OTHER way)."""
        real_repo = Path(__file__).resolve().parent.parent
        phase3 = real_repo / "tests" / "test_host_phase3.py"
        colorcal = real_repo / "tests" / "test_host_colorcal.py"

        def collected_under_host(node_id: str) -> tuple[set[str], int]:
            p = subprocess.run(
                [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", "host", node_id],
                cwd=str(real_repo),
                capture_output=True,
                text=True,
            )
            return {line for line in p.stdout.splitlines() if "::" in line}, p.returncode

        candidate_paths_host, candidate_paths_rc = collected_under_host(
            f"{phase3}::ProbeCandidatePathsHostTest"
        )
        exposure_black_host, exposure_black_rc = collected_under_host(
            f"{phase3}::ProbeExposureBlackDefaultHostTest"
        )
        colorcal_host, colorcal_rc = collected_under_host(str(colorcal))

        # Oracle 1: the real markers, as pytest itself resolves them.
        self.assertTrue(candidate_paths_host, "ProbeCandidatePathsHostTest lost @pytest.mark.host")
        self.assertEqual(candidate_paths_rc, 0)
        self.assertTrue(
            exposure_black_host, "ProbeExposureBlackDefaultHostTest lost @pytest.mark.host"
        )
        self.assertEqual(exposure_black_rc, 0)
        self.assertFalse(
            colorcal_host, "test_host_colorcal.py unexpectedly gained @pytest.mark.host"
        )
        # Finding 8: an empty result must be a clean "collected zero" (5),
        # not a broken collection (any other code) passing vacuously.
        self.assertIn(
            colorcal_rc,
            (0, 5),
            (colorcal_rc, "collection under -m host must be clean, not silently broken"),
        )
        # Positive control: colorcal WITHOUT -m host really does collect
        # real tests (rules out collection being broken some other way).
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", str(colorcal)],
            cwd=str(real_repo),
            capture_output=True,
            text=True,
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(
            any("::" in line for line in p.stdout.splitlines()),
            "test_host_colorcal.py collected no tests at all without -m host",
        )

        self.write_progress("done")
        out = self.invoke().stdout

        # Oracle 2: the printed command text matches what oracle 1 found.
        candidate_paths_line = next(
            l for l in out.splitlines() if "ProbeCandidatePathsHostTest" in l
        )  # noqa: E741
        exposure_black_line = next(
            l for l in out.splitlines() if "ProbeExposureBlackDefaultHostTest" in l
        )  # noqa: E741
        colorcal_line = next(
            l for l in out.splitlines() if "test_host_colorcal.py" in l
        )  # noqa: E741
        self.assertIn("-m host", candidate_paths_line, candidate_paths_line)
        self.assertIn("-m host", exposure_black_line, exposure_black_line)
        self.assertNotIn("-m host", colorcal_line, colorcal_line)

    def test_idempotent_reruns_agree(self) -> None:
        self.write_progress("done")
        first = self.invoke()
        second = self.invoke()
        self.assertEqual(first.returncode, second.returncode)
        # T1: strip say()'s second-granularity timestamps before comparing
        # -- a rerun that straddles a wall-clock second boundary otherwise
        # fails this on timestamp text alone, not on any real disagreement.
        strip = lambda s: re.sub(
            r"(?m)^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ", "", s
        )  # noqa: E731,E501
        self.assertEqual(strip(first.stdout), strip(second.stdout))


class RunOverWhenBlockedTest(_FakeRepoCase):
    """Row 493/495: night 1 can end with progress.json status
    'blocked: <reason>' (the loop refused a precondition rather than
    finishing every layer) -- the run is nonetheless OVER once the runner
    pid is gone and no live process matches the stamp (preconditions 2 and
    3, unchanged). A live runner still refuses, exactly as for 'done'."""

    BLOCKED_STATUS = (
        "blocked: darktable pids [69648] were not launched by " "the gauntlet; quit them first"
    )

    def test_blocked_with_runner_gone_is_accepted_and_prints_run_over(self) -> None:
        self.write_progress(self.BLOCKED_STATUS)
        # no pidfile at all -- precondition 2 passes ("runner pid gone")
        p = self.invoke()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(f"RUN OVER (ended blocked): {self.BLOCKED_STATUS}", p.stdout)
        self.assertNotIn("BLOCKED", p.stdout)
        # the rest of the batch still prints, same as the 'done' case
        self.assertIn("ff-merge", p.stdout.lower())

    def test_blocked_with_live_runner_pid_still_refuses(self) -> None:
        self.write_progress(self.BLOCKED_STATUS)
        self.write_pidfile(os.getpid())  # a genuinely live pid
        p = self.invoke()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("BLOCKED", p.stdout)
        self.assertIn("still running", p.stdout)
        self.assertNotIn("RUN OVER", p.stdout)


class MergeBaseGateTest(_FakeRepoCase):
    """Review I6: BASE is merge-base(main, BATCH), captured before any
    merge -- HEAD is BATCH itself, never a post-merge `git rev-parse HEAD`
    (which would equal BATCH after a ff-merge, an empty, gate-nothing
    range)."""

    def test_base_is_the_real_merge_base_not_batchs_own_head(self) -> None:
        self.write_progress("done")
        p = self.invoke()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(f"--base {self.base_sha} --head {self.batch_sha}", p.stdout)
        self.assertNotEqual(self.base_sha, self.batch_sha)

    def test_reports_already_merged_instead_of_refusing(self) -> None:
        """Fix round 2 (review R3): merge-base(main, BATCH) == BATCH can
        only mean BATCH is already merged into main -- never an error.
        Rerunning this script right after a human ran step 2 (the merge)
        must still exit 0 ("idempotent, rerun anytime"), not refuse."""
        self.write_progress("done")
        # Fast-forward main to batch: merge-base(main, BATCH) == BATCH now.
        git(self.repo, "merge", "--ff-only", self.batch_sha)
        p = self.invoke()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("already merged", p.stdout)
        self.assertNotIn("BLOCKED", p.stdout)
        # The remaining steps still print (host probes, stale-history, the
        # targeted rerun); step 1's own gate command is skipped, not shown
        # with a vacuous BASE==HEAD.
        self.assertIn("SKIPPED", p.stdout)
        self.assertIn("host probes", p.stdout)
        self.assertIn("targeted GAUNTLET rerun", p.stdout)
        self.assertNotIn(f"BASE={self.batch_sha} HEAD={self.batch_sha}", p.stdout)

    def test_execute_on_an_already_merged_batch_exits_0_without_running_anything(self) -> None:
        self.write_progress("done")
        git(self.repo, "merge", "--ff-only", self.batch_sha)
        marker = Path(self.tmp.name) / "gate-ran.marker"
        p = self.invoke(["--execute"], GATE_CMD=f"echo ran > {marker}")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(marker.exists(), "the gate must not run against an already-merged batch")

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
            'case "$*" in\n'
            "  *'run --stamp'*) exit 0 ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_pgrep.chmod(0o755)
        env = dict(os.environ)
        p = subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True,
            text=True,
            env={
                **env,
                **self.env(BATCH=self.batch_sha, RUN_DIR=str(self.run_dir)),
                "PATH": f"{fake_bin}:{env['PATH']}",
            },
        )
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("BLOCKED", p.stdout)
        self.assertIn("still running", p.stdout)


class PythonResolutionTest(unittest.TestCase):
    """Fix round 2 (review R3): before the merge, this script typically runs
    from the batch's own checkout or a bare worktree, neither of which has
    its own `.venv` -- PY must fall back to the MAIN checkout's `.venv`
    (found via git's own common-dir), not a bare `python3` with no pytest."""

    def test_falls_back_to_the_main_checkouts_venv_from_a_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main_repo = root / "main"
            main_repo.mkdir()
            git(main_repo, "init", "-q", "-b", "main")
            git(main_repo, "config", "user.email", "test@example.com")
            git(main_repo, "config", "user.name", "Test")
            (main_repo / "f.txt").write_text("base\n", encoding="utf-8")
            git(main_repo, "add", "-A")
            git(main_repo, "commit", "-q", "-m", "base")

            marker = root / "main-venv-used.marker"
            venv_bin = main_repo / ".venv" / "bin"
            venv_bin.mkdir(parents=True)
            stub = venv_bin / "python3"
            stub.write_text(
                f"#!/usr/bin/env bash\n"
                f"echo used >> {marker}\n"
                f'exec /usr/bin/env python3 "$@"\n',
                encoding="utf-8",
            )
            stub.chmod(0o755)

            worktree = root / "wt"
            git(main_repo, "worktree", "add", "-q", str(worktree), "-b", "batch")
            (worktree / "f.txt").write_text("batch\n", encoding="utf-8")
            git(worktree, "add", "-A")
            git(worktree, "commit", "-q", "-m", "batch work")
            batch_sha = git(worktree, "rev-parse", "HEAD")

            run_dir = root / "rundir"
            run_dir.mkdir()
            layers = {name: {"status": "done"} for name in DEFAULT_LAYERS}
            (run_dir / "progress.json").write_text(
                json.dumps({"status": "done", "layers": layers}), encoding="utf-8"
            )

            env = dict(os.environ)
            env.pop("SCOUTER_PYTHON", None)
            env.update({"HTC_REPO": str(worktree), "BATCH": batch_sha, "RUN_DIR": str(run_dir)})
            p = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)

            self.assertTrue(
                marker.exists(), "PY did not resolve to the main checkout's venv:\n" + p.stdout
            )
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_scouter_python_env_var_wins_over_everything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "test@example.com")
            git(repo, "config", "user.name", "Test")
            (repo / "f.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "base")
            git(repo, "checkout", "-q", "-b", "batch")
            (repo / "f.txt").write_text("batch\n", encoding="utf-8")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "batch work")
            batch_sha = git(repo, "rev-parse", "HEAD")

            marker = root / "explicit-python-used.marker"
            explicit_py = root / "explicit-python3"
            explicit_py.write_text(
                f"#!/usr/bin/env bash\n"
                f"echo used >> {marker}\n"
                f'exec /usr/bin/env python3 "$@"\n',
                encoding="utf-8",
            )
            explicit_py.chmod(0o755)

            run_dir = root / "rundir"
            run_dir.mkdir()
            layers = {name: {"status": "done"} for name in DEFAULT_LAYERS}
            (run_dir / "progress.json").write_text(
                json.dumps({"status": "done", "layers": layers}), encoding="utf-8"
            )

            env = dict(os.environ)
            env.update(
                {
                    "HTC_REPO": str(repo),
                    "BATCH": batch_sha,
                    "RUN_DIR": str(run_dir),
                    "SOUTER_PYTHON_UNUSED": "",
                    "SCOUTER_PYTHON": str(explicit_py),
                }
            )
            p = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)

            self.assertTrue(marker.exists(), "SCOUTER_PYTHON override was not used:\n" + p.stdout)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


class DefaultGateCommandTest(unittest.TestCase):
    """Fix round 3 (re-review R3b): --execute's DEFAULT GATE_CMD (no
    override) calls tests/mutation_check.py directly with this script's own
    already-resolved PY, not `make gate` -- whose own wrapper
    (scripts/mutation_check.sh) resolves python only from its own
    `.venv`, falling back to a bare python3 with no pytest from exactly the
    kind of bare worktree this script runs from before the merge. Verified
    with NO GATE_CMD override and NO `.venv` in the batch's own checkout,
    same worktree-without-a-venv shape as PythonResolutionTest."""

    def test_default_gate_command_calls_mutation_check_py_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main_repo = root / "main"
            main_repo.mkdir()
            git(main_repo, "init", "-q", "-b", "main")
            git(main_repo, "config", "user.email", "test@example.com")
            git(main_repo, "config", "user.name", "Test")
            (main_repo / "f.txt").write_text("base\n", encoding="utf-8")
            git(main_repo, "add", "-A")
            git(main_repo, "commit", "-q", "-m", "base")
            base_sha = git(main_repo, "rev-parse", "HEAD")

            argv_log = root / "argv.log"
            venv_bin = main_repo / ".venv" / "bin"
            venv_bin.mkdir(parents=True)
            stub = venv_bin / "python3"
            stub.write_text(
                f"#!/usr/bin/env bash\n" f'echo "$@" >> {argv_log}\n'
                # The status-parsing call (`"$PY" -c '...'`) needs to run for
                # real (this script's own preconditions read its output);
                # a script/module invocation (mutation_check.py, -m pytest)
                # only needs to be PROVEN CALLED with the right args -- there
                # is no real scouter repo here for either to run against.
                f'case "$1" in\n'
                f'  -c) exec /usr/bin/env python3 "$@" ;;\n'
                f"  *) exit 0 ;;\n"
                f"esac\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)

            worktree = root / "wt"
            git(main_repo, "worktree", "add", "-q", str(worktree), "-b", "batch")
            (worktree / "f.txt").write_text("batch\n", encoding="utf-8")
            git(worktree, "add", "-A")
            git(worktree, "commit", "-q", "-m", "batch work")
            batch_sha = git(worktree, "rev-parse", "HEAD")

            run_dir = root / "rundir"
            run_dir.mkdir()
            layers = {name: {"status": "done"} for name in DEFAULT_LAYERS}
            (run_dir / "progress.json").write_text(
                json.dumps({"status": "done", "layers": layers}), encoding="utf-8"
            )

            env = dict(os.environ)
            for k in ("SCOUTER_PYTHON", "GATE_CMD", "PYTEST_CMD"):
                env.pop(k, None)
            env.update({"HTC_REPO": str(worktree), "BATCH": batch_sha, "RUN_DIR": str(run_dir)})
            p = subprocess.run(
                ["bash", str(SCRIPT), "--execute"], capture_output=True, text=True, env=env
            )

            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertTrue(argv_log.exists(), "the stub python3 was never invoked:\n" + p.stdout)
            lines = argv_log.read_text(encoding="utf-8").splitlines()
            gate_lines = [ln for ln in lines if "mutation_check.py" in ln]
            self.assertTrue(gate_lines, f"tests/mutation_check.py was never called: {lines}")
            gate_line = gate_lines[0]
            self.assertIn(f"--base {base_sha}", gate_line)
            self.assertIn(f"--head {batch_sha}", gate_line)
            self.assertIn(f"--repo {worktree}", gate_line)
            self.assertFalse(
                any(ln.split()[:1] == ["gate"] for ln in lines), "must never call `make gate`"
            )


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
        p = self.invoke(
            ["--execute"], GATE_CMD="exit 1", PYTEST_CMD=f"echo should-not-run > {pytest_marker}"
        )
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
