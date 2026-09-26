"""Second oracle for tests/mutation_check.py (the scouter test-honesty gate,
mirroring capsule-corp decision row 459): a discriminating test file must PASS
this gate, a tautological one must FAIL it. Runs against a tiny fixture repo
built fresh, per test, in tempfile - never against scouter itself.

`-k "not host"` excludes scouter's real darktable-requiring suite; this file
carries no `host` in any test name, so it always runs with the rest."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_check as mc  # noqa: E402


def git(cwd, *args):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, f"git {args} failed: {p.stdout}{p.stderr}"
    return p.stdout


def init_repo(root):
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")


def commit_all(root, message):
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD").strip()


PROD_BUGGY = "def add(a, b):\n    return a - b\n"
PROD_FIXED = "def add(a, b):\n    return a + b\n"

PLACEHOLDER_TEST = "from production import add\n\n\ndef test_placeholder():\n    assert callable(add)\n"

DISCRIMINATING_TEST = (
    "from production import add\n\n\n"
    "def test_add_returns_the_sum():\n"
    "    assert add(2, 3) == 5\n"
)

TAUTOLOGICAL_TEST = (
    "from production import add\n\n\n"
    "def test_add_exists():\n"
    "    add(2, 3)\n"
    "    assert True\n"
)


class DiscriminatingTestPassesTheGate(unittest.TestCase):
    def test_a_real_bug_fix_with_a_real_test_passes(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            (root / "test_production.py").write_text(PLACEHOLDER_TEST)
            base = commit_all(root, "base: buggy add()")

            (root / "production.py").write_text(PROD_FIXED)
            (root / "test_production.py").write_text(DISCRIMINATING_TEST)
            head = commit_all(root, "fix add(): a - b -> a + b, cover it")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            self.assertEqual(len(rows), 1)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(f, "test_production.py")
            self.assertIn("FAIL (good)", rev_cell)
            self.assertIn("FAIL (good)", weak_cell)
            self.assertEqual(flags_cell, "none")

    def test_cli_exit_code_zero_and_table_on_stdout(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            (root / "test_production.py").write_text(PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            (root / "test_production.py").write_text(DISCRIMINATING_TEST)
            head = commit_all(root, "fix + real test")

            p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "mutation_check.py"),
                                 "--base", base, "--head", head, "--repo", str(root)],
                                capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("| test file |", p.stdout)


class TautologicalTestFailsTheGate(unittest.TestCase):
    def test_a_no_op_assert_true_test_is_caught(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            (root / "test_production.py").write_text(PLACEHOLDER_TEST)
            base = commit_all(root, "base: buggy add()")

            (root / "production.py").write_text(PROD_FIXED)
            (root / "test_production.py").write_text(TAUTOLOGICAL_TEST)
            head = commit_all(root, "fix add(), but the new test asserts nothing real")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("did not fail (BAD)", rev_cell)
            self.assertIn("tautology", flags_cell)

    def test_cli_exit_code_nonzero(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            (root / "test_production.py").write_text(PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            (root / "test_production.py").write_text(TAUTOLOGICAL_TEST)
            head = commit_all(root, "fix + tautological test")

            p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "mutation_check.py"),
                                 "--base", base, "--head", head, "--repo", str(root)],
                                capture_output=True, text=True)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn("did not fail (BAD)", p.stdout)


class NoTestFilesChanged(unittest.TestCase):
    def test_range_with_no_test_changes_is_a_noop_pass(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            base = commit_all(root, "base")
            (root / "readme.md").write_text("docs only\n")
            head = commit_all(root, "docs only, no test touched")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertTrue(ok)
            self.assertEqual(rows, [])
            self.assertIn("no new/changed test files", note)


class SkippedTestIsFlagged(unittest.TestCase):
    def test_xfail_mark_on_a_new_test_is_a_static_flag(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            (root / "test_production.py").write_text(
                "import pytest\nfrom production import add\n\n\n"
                "@pytest.mark.xfail\n"
                "def test_add_returns_the_sum():\n"
                "    assert add(2, 3) == 5\n")
            head = commit_all(root, "fix add(), but mark the new test xfail")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertFalse(ok, rows)
            self.assertIn("xfail", rows[0][-1])


class DarktableOnlyTestsAreNotAFailure(unittest.TestCase):
    def test_a_darktable_only_changed_test_reads_n_a_not_bad(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            (root / "test_production.py").write_text(
                "from production import add\n\n\n"
                "def test_host_needs_real_darktable():\n"
                "    assert add(2, 3) == 5\n")
            head = commit_all(root, "fix add(), only a host-only test covers it")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(rev_cell, "n/a")
            self.assertEqual(weak_cell, "n/a")


if __name__ == "__main__":
    unittest.main()
