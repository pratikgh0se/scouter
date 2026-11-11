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
import sys
import tempfile
import time
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
        e.update(
            {
                "HTC_REPO": str(self.repo),
                "HTC_RUNS_ROOT": str(self.runs_root),
                "HTC_TRIAL": str(self.trial),
                "HTC_MIN_FREE_GB": "0",  # disk headroom is not under test; CI runners have < 15 GB free
                "PATH": f"{self.fake_bin}:{e['PATH']}",
            }
        )
        e.update(extra)
        return e

    def run_script(self, **extra_env) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True, env=self.env(**extra_env)
        )


class ArgumentHandlingTest(_FakeReposCase):
    def test_custom_stamp_env_is_honored(self) -> None:
        p = self.run_script(STAMP="night2-19991231")
        self.assertIn("stamp night2-19991231", p.stdout)
        self.assertEqual(
            (self.runs_root / "night2" / "CURRENT_STAMP").read_text().strip(), "night2-19991231"
        )
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


class GauntletRunnerRefusalTest(_FakeReposCase):
    """N3 (review), fix round 2 (review R2): the two pidfile checks above
    only catch this script's OWN launchers -- a smoke run or a manual
    `gauntlet run --stamp ...` started outside either launcher would still
    race darktable. A direct pgrep for that command line catches it too,
    and the refusal must not pin CURRENT_STAMP.

    Review R2: the fake pgrep glob `*gauntlet*'run --stamp'*` a round-1 test
    used was LOOSER than the real regex the script ran
    (`gauntlet(\\.cli)? run --stamp`), which cannot match the real
    launcher's own argv (`python -c '...from gauntlet.cli import
    main...' run --stamp ...` -- "gauntlet" and "run --stamp" are not
    adjacent there). That let the test pass while the product check failed
    (confirmed live against the real running night-1 runner's argv). These
    tests spawn a REAL dummy process whose argv mimics the launcher's own
    command line and run the REAL system `pgrep` against it -- a smarter
    fake only isolates the darktable checks (this machine may have a real,
    unrelated live gauntlet's darktable up at any time) and forwards
    everything else, including this script's own gauntlet-runner pgrep, to
    the real binary."""

    def _install_pgrep_forwarding_to_real_binary_except_darktable(self) -> None:
        fake_pgrep = self.fake_bin / "pgrep"
        fake_pgrep.write_text(
            "#!/usr/bin/env bash\n"
            'case "$*" in\n'
            "  *darktable*) exit 1 ;;\n"  # isolate from any real, unrelated darktable
            '  *) exec /usr/bin/pgrep "$@" ;;\n'  # everything else: the real binary
            "esac\n",
            encoding="utf-8",
        )
        fake_pgrep.chmod(0o755)

    def _spawn_dummy_launcher(self, stamp: str) -> subprocess.Popen:
        """A real process whose full command line (as pgrep -f sees it)
        mimics the real launcher's: "gauntlet.cli" and "run --stamp
        <stamp>" present, but NOT adjacent -- exactly the shape the old,
        broken pattern could not match."""
        proc = subprocess.Popen(
            [
                "python3",
                "-c",
                f"import time  # stand-in for '...from gauntlet.cli import main...' "
                f"run --stamp {stamp} --resume --wait-on-park\n"
                "time.sleep(20)",
            ]
        )
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            found = (
                subprocess.run(
                    ["pgrep", "-f", f"run --stamp {stamp}"], capture_output=True
                ).returncode
                == 0
            )
            if found:
                break
            time.sleep(0.05)
        else:
            self.fail("dummy launcher process never became visible to pgrep")
        return proc

    def test_refuses_when_a_gauntlet_runner_is_already_running(self) -> None:
        self._install_pgrep_forwarding_to_real_binary_except_darktable()
        stamp = "night2-refusal-test-stamp"
        self._spawn_dummy_launcher(stamp)
        p = self.run_script()
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertIn("gauntlet runner", p.stdout)
        # R2 nit: the script's own `pgrep -fl` (printed before it refuses)
        # must name THIS dummy process specifically.
        self.assertIn(stamp, p.stdout)

    def test_the_refusal_does_not_pin_current_stamp(self) -> None:
        self._install_pgrep_forwarding_to_real_binary_except_darktable()
        self._spawn_dummy_launcher("night2-refusal-test-stamp-2")
        p = self.run_script()
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertFalse(
            (self.runs_root / "night2" / "CURRENT_STAMP").exists(),
            "a refused attempt must not pin today's stamp",
        )

    def test_the_old_narrower_pattern_would_have_missed_the_real_launcher_argv(self) -> None:
        """Regression guard for review R2 itself: proves the OLD pattern
        (`gauntlet(\\.cli)? run --stamp`) really does miss the realistic
        launcher argv this class spawns, and the fixed one
        (`gauntlet.*run --stamp`) really does catch it -- against the real
        pgrep binary, not a fake."""
        self._install_pgrep_forwarding_to_real_binary_except_darktable()
        stamp = "night2-pattern-regression-stamp"
        self._spawn_dummy_launcher(stamp)
        old = subprocess.run(["pgrep", "-f", r"gauntlet(\.cli)? run --stamp"], capture_output=True)
        # BSD/macOS `pgrep -l` lists the full command line; procps (Linux) `-l`
        # lists only the process name and `-a` is the full-command-line flag
        # (while on macOS `-a` means something else entirely).
        list_flag = "-fl" if sys.platform == "darwin" else "-fa"
        new = subprocess.run(["pgrep", list_flag, "gauntlet.*run --stamp"], capture_output=True)
        self.assertEqual(old.returncode, 1, "the old pattern should NOT match")
        self.assertEqual(new.returncode, 0, "the fixed pattern SHOULD match")
        # R2 nit: prove it matched THIS dummy process specifically, not
        # some other, coincidental process on the machine.
        self.assertIn(stamp, new.stdout.decode(), new.stdout)


if __name__ == "__main__":
    unittest.main()


class SubsetWiringTest(unittest.TestCase):
    """(row 501 item H, fix round 1): the generate call passes the
    ruling's own --subset 40 --subset-layer scenarios_agent
    --subset-keys class,genre. A static check on the script source (the
    class above already covers the script's runtime behaviour without a
    real gauntlet corpus; exercising --subset end to end needs a real
    generate step, out of that fixture's scope)."""

    def test_generate_call_passes_the_ruling_subset_flags(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("--subset", text)
        self.assertIn("--subset-layer scenarios_agent", text)
        self.assertIn("--subset-keys class,genre", text)
        self.assertIn("--seed", text)


class SubsetSeedDeterminismTest(_FakeReposCase):
    """(row 501 item H, fix round 2, Pratik's ruling 6, Opus re-review
    finding 6): the earlier seed, `date -j -f '%Y%m%d' "${STAMP#night2-}"
    +%s`, fills hour/minute/second from the CURRENT wall clock (the stamp
    itself carries no time of day) -- two calls seconds apart produced two
    different seeds for the SAME stamp. The fixed formula (a sha1 of the
    stamp text, truncated and read as base-16) must agree every time.
    Runs the REAL script (never a reimplementation of its own seed
    formula) and reads back the `--seed` value each real invocation of the
    (stubbed) `gauntlet generate` actually received."""

    def _capture_seed(self, stamp: str) -> str:
        capture = self.runs_root / "seed_capture.txt"  # outside the repo -- never dirties it
        stub = self.repo / ".venv" / "bin" / "python"
        stub.write_text(
            "#!/usr/bin/env bash\n" f'echo "$@" >> "{capture}"\n' "exit 1\n", encoding="utf-8"
        )
        stub.chmod(0o755)
        git(self.repo, "add", "-A")
        subprocess.run(
            ["git", "commit", "-q", "-m", "stub: capture argv"],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
        )  # a no-op stub rewrite has nothing to
        # commit the second time -- fine either
        # way, only a real failure asserts below
        self.assertEqual(
            subprocess.run(
                ["git", "status", "--porcelain"], cwd=str(self.repo), capture_output=True, text=True
            ).stdout,
            "",
        )
        p = self.run_script(STAMP=stamp)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        lines = [ln for ln in capture.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertTrue(lines, "the stub python was never invoked")
        m = re.search(r"--seed (\d+)", lines[-1])
        self.assertIsNotNone(m, lines[-1])
        return m.group(1)

    def test_two_real_runs_of_the_same_stamp_agree_on_the_seed(self) -> None:
        """A realistic, DATE-SHAPED stamp (`night2-YYYYMMDD`): the earlier
        formula, `date -j -f '%Y%m%d' "${STAMP#night2-}" +%s`, parses this
        one successfully -- and fills hour/minute/second from the CURRENT
        wall clock, so two real calls a couple of seconds apart genuinely
        disagreed (reproduced directly: `date -j -f '%Y%m%d' 20260928 +%s`
        run twice, 2 s apart, gave 1790606360 and 1790606362). A
        non-date-shaped stamp would instead fall into the old formula's
        `|| echo 0` fallback and trivially agree either way -- this uses
        the one shape that actually exercises the bug this fix closes."""
        first = self._capture_seed("night2-20260928")
        time.sleep(1.1)  # real wall-clock gap -- the old formula's own failure mode
        second = self._capture_seed("night2-20260928")
        self.assertEqual(first, second)

    def test_a_different_stamp_can_derive_a_different_seed(self) -> None:
        a = self._capture_seed("night2-20260928")
        b = self._capture_seed("night2-20261005")
        self.assertNotEqual(a, b)
