"""Second oracle for tests/mutation_check.py (the scouter test-honesty gate,
mirroring capsule-corp decision row 459): a discriminating test file must PASS
this gate, a tautological one must FAIL it. Runs against a tiny fixture repo
built fresh, per test, in tempfile - never against scouter itself.

Fixtures live under `tests/` in the fixture repo (fix round 1: `is_test_file`
is now scoped to this repo's real test directories - `tests/`,
`gauntlet/tests/`, `tests/gauntlet/` - not basename alone), while
`production.py` stays at the fixture repo's root.

`-k "not host"` excludes scouter's real darktable-requiring suite; this file
carries no `host` in any test name, so it always runs with the rest."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
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


def write_test(root, text):
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_production.py").write_text(text)


def copy_gate_scaffolding(dest, repo_root):
    """Copy the `make gate` / `scripts/merge_gate.sh` / `scripts/mutation_check.sh`
    / `tests/mutation_check.py` scaffolding from `repo_root` (wherever THIS
    checkout happens to live) into a temp fixture dir `dest`, so `make gate`
    runs fully self-contained there. Fix round 8: `MakeGateInvokesTheScript`
    used to run `make gate` against `self.REPO_ROOT` itself - the real,
    possibly-concurrently-used host repo. Its own `git worktree add`/`remove`
    calls then raced any OTHER agent's concurrent git worktree operations on
    the SAME shared `.git` (this environment routinely runs several agents
    in parallel), which is exactly the "assumes a clean git state" isolation
    bug: not reproducible deterministically, and not reproducible at all from
    a checkout path other than the one the test happened to be authored in."""
    (dest / "scripts").mkdir(parents=True, exist_ok=True)
    (dest / "tests").mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo_root / "Makefile", dest / "Makefile")
    shutil.copy2(repo_root / "scripts" / "merge_gate.sh", dest / "scripts" / "merge_gate.sh")
    shutil.copy2(repo_root / "scripts" / "mutation_check.sh", dest / "scripts" / "mutation_check.sh")
    shutil.copy2(repo_root / "tests" / "mutation_check.py", dest / "tests" / "mutation_check.py")
    (dest / "scripts" / "merge_gate.sh").chmod(0o755)
    (dest / "scripts" / "mutation_check.sh").chmod(0o755)


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

IMPORT_ERROR_TEST = (
    "import nonexistent_module_xyz\n"
    "from production import add\n\n\n"
    "def test_add_returns_the_sum():\n"
    "    assert add(2, 3) == 5\n"
)


class DiscriminatingTestPassesTheGate(unittest.TestCase):
    def test_a_real_bug_fix_with_a_real_test_passes(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: buggy add()")

            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, DISCRIMINATING_TEST)
            head = commit_all(root, "fix add(): a - b -> a + b, cover it")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            self.assertEqual(len(rows), 1)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(f, "tests/test_production.py")
            self.assertIn("FAIL (good)", rev_cell)
            self.assertIn("FAIL (good)", weak_cell)
            self.assertEqual(flags_cell, "none")

    def test_cli_exit_code_zero_and_table_on_stdout(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, DISCRIMINATING_TEST)
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
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: buggy add()")

            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, TAUTOLOGICAL_TEST)
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
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, TAUTOLOGICAL_TEST)
            head = commit_all(root, "fix + tautological test")

            p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "mutation_check.py"),
                                 "--base", base, "--head", head, "--repo", str(root)],
                                capture_output=True, text=True)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn("did not fail (BAD)", p.stdout)


class ImportErrorIsNotDiscriminating(unittest.TestCase):
    """Fix round 1: a test file that cannot even be collected must not read as
    "FAIL (good)" just because pytest exits non-zero - that exit happens for
    a genuine assertion failure too, and only a collection error is not
    discrimination at all."""

    def test_an_import_error_reads_as_error_not_fail_good(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: buggy add()")

            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, IMPORT_ERROR_TEST)
            head = commit_all(root, "fix add(), but the new test module cannot import")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("ERROR", rev_cell)
            self.assertNotIn("FAIL (good)", rev_cell)
            self.assertIn("ERROR", weak_cell)
            self.assertNotIn("FAIL (good)", weak_cell)

    def test_cli_exit_code_nonzero_and_table_says_error(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, IMPORT_ERROR_TEST)
            head = commit_all(root, "fix + a test module that cannot import")

            p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "mutation_check.py"),
                                 "--base", base, "--head", head, "--repo", str(root)],
                                capture_output=True, text=True)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn("ERROR (not discriminating)", p.stdout)


class NewModuleImportErrorIsNotAFailure(unittest.TestCase):
    """Fix round 2 (row 459 audit, capsule-corp's capsule/intent-examples
    f3031c3..0e184f7, mirrored here): when the range ADDS a production
    module, reverting it to base means deleting it - the test that imports it
    ERRORs no matter how well it discriminates. That specific shape reads
    `n/a (new module)`, not `ERROR (not discriminating)`, and the requirement
    moves to the assertion-weakening column instead."""

    def _write_newmod_test(self, root, text):
        (root / "tests").mkdir(exist_ok=True)
        (root / "tests" / "test_newmod.py").write_text(text)

    def test_a_new_module_plus_its_test_reads_n_a_new_module_not_error(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: production.py only, no newmod yet")

            (root / "newmod.py").write_text("def double(x):\n    return x * 2\n")
            self._write_newmod_test(
                root,
                "from newmod import double\n\n\n"
                "def test_double():\n"
                "    assert double(3) == 6\n")
            head = commit_all(root, "add newmod.py and cover it")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            row = next(r for r in rows if r[0] == "tests/test_newmod.py")
            f, names, rev_cell, weak_cell, flags_cell = row
            self.assertEqual(rev_cell, "n/a (new module)")
            self.assertIn("FAIL (good)", weak_cell)

    def test_a_new_module_with_no_assert_still_fails_the_gate(self):
        """The n/a on revert must not become a free pass - a test covering a
        brand-new module with no assert at all (nothing for the weaken column
        to even flip) still fails the gate, via the static flag."""
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: production.py only, no newmod yet")

            (root / "newmod.py").write_text("def double(x):\n    return x * 2\n")
            self._write_newmod_test(
                root,
                "from newmod import double\n\n\n"
                "def test_double():\n"
                "    double(3)\n")
            head = commit_all(root, "add newmod.py, but the test asserts nothing")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            row = next(r for r in rows if r[0] == "tests/test_newmod.py")
            f, names, rev_cell, weak_cell, flags_cell = row
            self.assertEqual(rev_cell, "n/a (new module)")
            self.assertIn("no assert", flags_cell)


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


class NonSuiteTestNamedFileIsIgnored(unittest.TestCase):
    """Fix round 1: `is_test_file` is scoped to `tests/`, `gauntlet/tests/`,
    `tests/gauntlet/` - a `test_*.py` OUTSIDE those directories is not part
    of the collected suite and must not be required to discriminate."""

    def test_a_test_named_file_outside_a_test_dir_is_not_treated_as_a_test(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            (root / "test_scratch.py").write_text("x = 1\n")
            head = commit_all(root, "fix add(), plus an unrelated scratch file")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertTrue(ok)
            self.assertEqual(rows, [])
            self.assertIn("no new/changed test files", note)


class TestSupportFilesAreNeverProduction(unittest.TestCase):
    """Fix round 3 (audit, fix/htc-applied-verify): a `.py` file under a test
    directory that is not itself `test_*.py` - `conftest.py`, a
    fakes/fixtures helper, `tests/**/__init__.py` - is test-support, never
    production: never reverted, never mutated, never counted toward "at least
    one production file changed"."""

    def test_is_test_support_file_classification(self):
        support = ["tests/conftest.py", "tests/scouter_fakes.py",
                   "gauntlet/tests/__init__.py", "tests/gauntlet/conftest.py",
                   "tests/fixtures/corpus.py"]
        # Deliberately real harness code that lives under a test directory,
        # not a fake/mock/stub/conftest/fixture - must stay ordinary
        # production (this is the exact pair test_execute.py exists to cover).
        production = ["scouter/paths.py", "gauntlet/evaluate.py", "production.py", "newmod.py",
                       "tests/gauntlet/observe.py", "tests/gauntlet/execute.py", "tests/host_support.py"]
        for p in support:
            self.assertTrue(mc.is_test_support_file(p), p)
            self.assertFalse(mc.is_test_file(p), p)
        for p in production:
            self.assertFalse(mc.is_test_support_file(p), p)

    def test_a_helper_changed_alongside_a_real_fix_is_never_reverted(self):
        """A support helper (`tests/scouter_fakes.py`) co-changes with the real
        production fix. Spy on `revert_to_base` and assert it is called for
        the production file but never for the helper - proof the helper's
        HEAD content survives the revert-mutation run untouched."""
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            (root / "tests").mkdir()
            (root / "tests" / "scouter_fakes.py").write_text("OLD = True\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: buggy add(), old helper")

            (root / "production.py").write_text(PROD_FIXED)
            (root / "tests" / "scouter_fakes.py").write_text("OLD = True\nNEW = True\n")
            write_test(root, DISCRIMINATING_TEST)
            head = commit_all(root, "fix add(), plus a co-changed test helper")

            calls = []
            real_revert = mc.revert_to_base

            def spy(repo_, base_, scratch, path, status):
                calls.append(path)
                return real_revert(repo_, base_, scratch, path, status)

            with unittest.mock.patch.object(mc, "revert_to_base", side_effect=spy):
                ok, rows, note = mc.check_range(root, base, head)

            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            self.assertIn("production.py", calls)
            self.assertNotIn("tests/scouter_fakes.py", calls)


class SharedProductionModulePlusNewModule(unittest.TestCase):
    """Fix round 3, second half: `errors.py`/`types.py`-style shared
    production modules that already existed at base are treated as ordinary
    production (reverted normally) - but when a test also imports a module
    that's NEW in the range, the new-module rule still applies to THAT half
    of the collection error."""

    def test_existing_shared_module_plus_a_new_module_only_new_module_gets_n_a(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "errors.py").write_text("class BadInput(Exception):\n    pass\n")
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: errors.py + buggy production.py, no newmod yet")

            (root / "errors.py").write_text(
                "class BadInput(Exception):\n    pass\n\n\nclass Extra(Exception):\n    pass\n")
            (root / "production.py").write_text(PROD_FIXED)
            (root / "newmod.py").write_text("def double(x):\n    return x * 2\n")
            (root / "tests" / "test_production.py").write_text(
                "from errors import BadInput\n"
                "from production import add\n"
                "from newmod import double\n\n\n"
                "def test_add_and_double():\n"
                "    assert add(2, 3) == 5\n"
                "    assert double(3) == 6\n"
                "    assert issubclass(BadInput, Exception)\n")
            head = commit_all(root, "extend errors.py, fix add(), add newmod.py, cover all three")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(rev_cell, "n/a (new module)")
            self.assertIn("FAIL (good)", weak_cell)


class NoProductionChangeForThisTestFile(unittest.TestCase):
    """Fix round 4 (window-5 gate run, gate-w5.txt): a changed test file whose
    SUBJECT module(s) have no production change in this range - e.g. new
    tests added for pre-existing, untouched code, while some UNRELATED
    production file also changed - must not read "did not fail (BAD)" just
    because *some* production file changed in the range. Detected via the
    test file's own imports vs. the set of changed production files."""

    def test_new_tests_for_unchanged_code_read_n_a_no_production_change(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)  # never changes
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")  # buggy, unrelated
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: production.py (already correct) + buggy unrelated.py")

            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
            write_test(root, DISCRIMINATING_TEST)
            head = commit_all(root, "fix unrelated.py; add new tests for production.py, which did not change")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(rev_cell, "n/a (no production change)")
            self.assertIn("FAIL (good)", weak_cell)

    def test_cli_exit_code_zero(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
            write_test(root, DISCRIMINATING_TEST)
            head = commit_all(root, "fix unrelated.py; new tests for untouched production.py")

            p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "mutation_check.py"),
                                 "--base", base, "--head", head, "--repo", str(root)],
                                capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("n/a (no production change)", p.stdout)


class NewModuleUnderATestDirectoryNotItselfATest(unittest.TestCase):
    """Fix round 4, second half (window-5 gate run): a NEW production file
    that lives under a test directory but is not a test module itself (e.g. a
    tool under `tests/`) is still a new module for the new-module rule -
    including when a test imports it via a `sys.path` hack and a bare-stem
    name."""

    def test_a_new_tool_under_tests_imported_by_bare_stem_reads_n_a_new_module(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: no toolkit.py yet")

            # A new NON-test .py file under tests/ (a tool, not test_*.py).
            (root / "tests" / "toolkit.py").write_text("def triple(x):\n    return x * 3\n")
            write_test(root,
                       "import sys\nfrom pathlib import Path\n\n"
                       "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
                       "import toolkit\n\n\n"
                       "def test_triple():\n"
                       "    assert toolkit.triple(3) == 9\n")
            head = commit_all(root, "add tests/toolkit.py (a tool, not a test) and cover it by bare-stem import")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(rev_cell, "n/a (new module)")
            self.assertIn("FAIL (good)", weak_cell)


class TargetsPredateRangeMarker(unittest.TestCase):
    """Fix round 5: an explicit, auditable `# mutation-check:
    targets-predate-range <base-sha> -- <reason>` top-of-file marker. A
    matching sha reads n/a with the reason shown; any other use (wrong sha)
    is reported, never silently ignored."""

    def test_a_marker_matching_base_reads_n_a_with_the_reason_and_requires_weaken(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)  # pre-existing, correct, never changes
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: production.py already correct, buggy unrelated.py")

            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
            marked_test = (
                f"# mutation-check: targets-predate-range {base} -- "
                "new tests for pre-existing production.py, killed 3 hand mutants\n"
                + DISCRIMINATING_TEST)
            write_test(root, marked_test)
            head = commit_all(root, "fix unrelated.py; add marked tests for untouched production.py")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(rev_cell, "n/a (targets predate range: new tests for pre-existing "
                                        "production.py, killed 3 hand mutants)")
            self.assertIn("FAIL (good)", weak_cell)

    def test_a_marker_with_the_wrong_sha_is_reported_not_silently_ignored(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")

            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
            wrong_sha = "0" * 40
            marked_test = (
                f"# mutation-check: targets-predate-range {wrong_sha} -- stale marker\n"
                + DISCRIMINATING_TEST)
            write_test(root, marked_test)
            head = commit_all(root, "fix unrelated.py; test file carries a marker for the wrong base")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("marker mismatch (BAD)", rev_cell)

    def test_cli_exit_code_zero_with_a_matching_marker(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
            marked_test = (f"# mutation-check: targets-predate-range {base} -- pre-existing coverage\n"
                            + DISCRIMINATING_TEST)
            write_test(root, marked_test)
            head = commit_all(root, "fix unrelated.py; add marked tests")

            p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "mutation_check.py"),
                                 "--base", base, "--head", head, "--repo", str(root)],
                                capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("targets predate range: pre-existing coverage", p.stdout)

    def test_cli_exit_code_nonzero_with_a_mismatched_marker(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
            wrong_sha = "1" * 40
            marked_test = (f"# mutation-check: targets-predate-range {wrong_sha} -- stale marker\n"
                            + DISCRIMINATING_TEST)
            write_test(root, marked_test)
            head = commit_all(root, "fix unrelated.py; stale marker")

            p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "mutation_check.py"),
                                 "--base", base, "--head", head, "--repo", str(root)],
                                capture_output=True, text=True)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn("marker mismatch (BAD)", p.stdout)

    def test_extract_marker_only_scans_the_top_of_the_file(self):
        buried = "\n".join([f"# line {i}" for i in range(30)]) + \
            "\n# mutation-check: targets-predate-range abc1234 -- too late, buried\n"
        self.assertIsNone(mc.extract_marker(buried))
        top = "# mutation-check: targets-predate-range abc1234 -- right at the top\nimport os\n"
        self.assertEqual(mc.extract_marker(top), ("abc1234", "right at the top"))


class WeakeningTargetsOnlyTheChangedFunction(unittest.TestCase):
    """Fix round 6: the assertion-weakening mutation must land INSIDE a test
    function that is new or changed in the range - an old, untouched test
    earlier in the same file must never be the one mutated, even when its
    own assertion would otherwise be the first one `apply_weakening` would
    find by scanning the whole file top to bottom."""

    def test_an_old_test_first_then_a_new_test_below_it_only_mutates_the_new_one(self):
        text = (
            "def test_old():\n"
            "    assert 1 == 1\n\n\n"
            "def test_new():\n"
            "    assert 2 == 2\n"
        )
        # test_old's def is line 1, body line 2; test_new's def is line 5,
        # body line 6. Only the new function's range is passed - exactly
        # what check_range does (func_ranges comes from
        # changed_test_functions, new/changed only).
        new_func_range = [(5, 6)]
        mutated, what = mc.apply_weakening(text, new_func_range)
        self.assertIsNotNone(mutated)
        self.assertEqual(what, "assert a == b -> assert a != b")
        self.assertIn("    assert 1 == 1\n", mutated)
        self.assertIn("    assert 2 != 2\n", mutated)
        self.assertNotIn("assert 1 != 1", mutated)

    def test_end_to_end_old_test_first_new_test_below_still_discriminates_correctly(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, "from production import add\n\n\ndef test_placeholder():\n    assert callable(add)\n")
            base = commit_all(root, "base: buggy add(), one old test")

            (root / "production.py").write_text(PROD_FIXED)
            write_test(root,
                       "from production import add\n\n\n"
                       "def test_placeholder():\n"
                       "    assert callable(add)\n\n\n"
                       "def test_add_returns_the_sum():\n"
                       "    assert add(2, 3) == 5\n")
            head = commit_all(root, "fix add(); old test untouched, new test added below it")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(names, "test_add_returns_the_sum")
            self.assertIn("FAIL (good)", weak_cell)
            self.assertNotIn("fallback", weak_cell)


class WeakeningFallsBackToFileLevelWhenTheChangedFunctionHasNoAssert(unittest.TestCase):
    """Fix round 6, second half: when the new/changed test function(s)
    contain literally no assertion at all (the static flag already reports
    this separately), the weaken column falls back to the file's first
    assertion anywhere and always says so in the table - pass or fail."""

    def test_no_assert_in_the_new_test_falls_back_and_is_labelled(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root,
                       "from production import add\n\n\n"
                       "def test_old_with_a_real_check():\n"
                       "    assert add(2, 3) == 5\n")
            base = commit_all(root, "base: one old test with a real assertion")

            write_test(root,
                       "from production import add\n\n\n"
                       "def test_old_with_a_real_check():\n"
                       "    assert add(2, 3) == 5\n\n\n"
                       "def test_new_makes_a_call_but_checks_nothing():\n"
                       "    add(2, 3)\n")
            head = commit_all(root, "add a new test with no assert at all")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)  # the static "no assert" flag alone fails the gate
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("no assert", flags_cell)
            self.assertIn("weaken: file-level fallback", weak_cell)
            # It ran against the old test's real assertion and that DID fail
            # (good) - still labelled, so a reviewer knows it wasn't the new
            # test.
            self.assertIn("FAIL (good)", weak_cell)


class ReferencesProdFileResolvesToSpecificSubmodules(unittest.TestCase):
    """Fix round 7: `from pkg import a, b` must resolve to the specific
    submodules `pkg/a.py`, `pkg/b.py` (and subpackages) - never to the bare
    `pkg` standing in for some OTHER, unrelated submodule under the same
    package. Mirrors the literal case from the audit: a test doing
    `from capsule import db, incidents` with `db.py` changed and
    `incidents.py` unchanged must count only `db.py`."""

    def _make_pkg(self, root, db_body, incidents_body):
        (root / "pkg").mkdir(exist_ok=True)
        (root / "pkg" / "__init__.py").write_text("")
        (root / "pkg" / "db.py").write_text(db_body)
        (root / "pkg" / "incidents.py").write_text(incidents_body)

    def test_importing_both_names_with_only_one_changed_counts_only_that_one(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            self._make_pkg(root, "def get(x):\n    return x - x\n", "INCIDENTS = []\n")
            write_test(root, "from pkg import db, incidents\n\n\ndef test_placeholder():\n    assert db\n")
            base = commit_all(root, "base: buggy db.get(), incidents.py untouched throughout")

            (root / "pkg" / "db.py").write_text("def get(x):\n    return x\n")
            write_test(root,
                       "from pkg import db, incidents\n\n\n"
                       "def test_get_returns_its_argument():\n"
                       "    assert db.get(3) == 3\n"
                       "    assert incidents.INCIDENTS == []\n")
            head = commit_all(root, "fix db.get(); incidents.py never changes in this range")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("FAIL (good)", rev_cell)  # correctly recognized as referencing db.py
            self.assertIn("FAIL (good)", weak_cell)

    def test_importing_the_unchanged_sibling_only_reads_no_production_change(self):
        """The regression this round actually guards against: incidents.py
        (unrelated) changes; the test imports ONLY db (unchanged) from the
        same package. The OLD bare-package match would have treated "pkg" as
        a stand-in for incidents.py too and wrongly run (and fail) the
        revert; the fix must read n/a (no production change) instead."""
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            self._make_pkg(root, "def get(x):\n    return x\n", "INCIDENTS = []\n")
            write_test(root, "from pkg import db\n\n\ndef test_placeholder():\n    assert db\n")
            base = commit_all(root, "base: db.py already correct, incidents.py about to change")

            (root / "pkg" / "incidents.py").write_text("INCIDENTS = ['fixed']\n")
            write_test(root,
                       "from pkg import db\n\n\n"
                       "def test_get_returns_its_argument():\n"
                       "    assert db.get(3) == 3\n")
            head = commit_all(root, "change incidents.py (unrelated); new test only imports db")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(rev_cell, "n/a (no production change)")
            self.assertIn("FAIL (good)", weak_cell)


class ReferencesProdFileHandlesInitReexports(unittest.TestCase):
    """Fix round 7, the `__init__` case: `pkg/__init__.py` doing `from
    pkg._core import shared` re-exports `shared` from `_core`, not from a
    same-named `pkg/shared.py` (which does not exist) - `from pkg import
    shared` must still be recognized as referencing `pkg/_core.py`."""

    def test_a_name_reexported_through_init_is_traced_to_its_real_submodule(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "pkg").mkdir()
            (root / "pkg" / "__init__.py").write_text("from pkg._core import shared\n")
            (root / "pkg" / "_core.py").write_text("def shared(x):\n    return x - x\n")
            write_test(root, "from pkg import shared\n\n\ndef test_placeholder():\n    assert shared\n")
            base = commit_all(root, "base: buggy shared(), re-exported through __init__")

            (root / "pkg" / "_core.py").write_text("def shared(x):\n    return x\n")
            write_test(root,
                       "from pkg import shared\n\n\n"
                       "def test_shared_returns_its_argument():\n"
                       "    assert shared(3) == 3\n")
            head = commit_all(root, "fix _core.shared(), only ever imported via the pkg re-export")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("FAIL (good)", rev_cell)  # NOT "n/a (no production change)"
            self.assertIn("FAIL (good)", weak_cell)


class NoChangedTestFunctionsIsNotAFailure(unittest.TestCase):
    """Fix round 8 (HTC integration report): a changed test FILE whose only
    edit in-range is NOT inside any `def test_*` (a decorator-only edit, or a
    fixture/helper function's signature changing - e.g. three local
    `fake_commit` fixtures gaining an `expect_module=None` keyword, the
    integration's real example) has zero new/changed test functions - there
    is nothing to select and run. `ids = []` must never reach `run_pytest`
    and be misread as `rc == 0` -> "did not fail (BAD)"; it must read
    n/a (no test functions changed), and must NOT count against the gate."""

    def test_a_helper_signature_change_with_no_test_function_touched_reads_n_a(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")
            write_test(root, "def test_placeholder():\n    assert True\n")
            base = commit_all(root, "base: buggy unrelated.py, one placeholder test")

            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
            write_test(root,
                       "def test_placeholder():\n    assert True\n\n\n"
                       "def fake_commit(x, expect_module=None):\n"
                       "    return x\n")
            head = commit_all(root, "fix unrelated.py; add a non-test helper, no test function touched")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(names, "-")  # no new/changed test function at all
            self.assertEqual(rev_cell, "n/a (no test functions changed)")
            self.assertEqual(weak_cell, "n/a")

    def test_cli_exit_code_zero(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")
            write_test(root, "def test_placeholder():\n    assert True\n")
            base = commit_all(root, "base")
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
            write_test(root,
                       "def test_placeholder():\n    assert True\n\n\n"
                       "def fake_commit(x, expect_module=None):\n"
                       "    return x\n")
            head = commit_all(root, "fix unrelated.py; helper-only edit")

            p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "mutation_check.py"),
                                 "--base", base, "--head", head, "--repo", str(root)],
                                capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("n/a (no test functions changed)", p.stdout)
            self.assertNotIn("did not fail (BAD)", p.stdout)

    def test_the_file_level_fallback_still_runs_for_files_that_DO_have_changed_functions(self):
        """Round 6's fallback rule must stay intact - this guard only fires
        when funcs is genuinely empty, never for a file that has a
        new/changed test function (even one with no assert of its own)."""
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root,
                       "from production import add\n\n\n"
                       "def test_old_with_a_real_check():\n"
                       "    assert add(2, 3) == 5\n")
            base = commit_all(root, "base: one old test with a real assertion")

            write_test(root,
                       "from production import add\n\n\n"
                       "def test_old_with_a_real_check():\n"
                       "    assert add(2, 3) == 5\n\n\n"
                       "def test_new_makes_a_call_but_checks_nothing():\n"
                       "    add(2, 3)\n")
            head = commit_all(root, "add a new test with no assert at all")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertNotEqual(names, "-")
            self.assertIn("weaken: file-level fallback", weak_cell)


class SkippedTestIsFlagged(unittest.TestCase):
    def test_xfail_mark_on_a_new_test_is_a_static_flag(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root,
                       "import pytest\nfrom production import add\n\n\n"
                       "@pytest.mark.xfail\n"
                       "def test_add_returns_the_sum():\n"
                       "    assert add(2, 3) == 5\n")
            head = commit_all(root, "fix add(), but mark the new test xfail")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertFalse(ok, rows)
            self.assertIn("xfail", rows[0][-1])


class MakeGateInvokesTheScript(unittest.TestCase):
    """Fix round 1, critical: `scripts/mutation_check.sh` existed but nothing
    called it. `make gate` -> `scripts/merge_gate.sh` -> `scripts/mutation_check.sh`
    is the chain the HTC merge step (and RUNBOOK/README) name - assert each
    link actually runs the next, against a real fixture repo, not just that
    the files exist."""

    REPO_ROOT = Path(__file__).resolve().parents[1]

    def test_scripts_merge_gate_sh_runs_the_real_check_and_exits_nonzero_on_a_tautology(self):
        # `make gate BASE= HEAD=` always gates THIS repo (scripts/merge_gate.sh
        # takes no repo argument from the Makefile) - so exercise the chain
        # `make gate` actually execs (scripts/merge_gate.sh -> mutation_check.sh
        # -> tests/mutation_check.py) directly against a fixture repo, passing
        # the optional repo-root argument `make gate` never does but
        # `scripts/merge_gate.sh` supports for exactly this reason.
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: buggy add()")
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, TAUTOLOGICAL_TEST)
            head = commit_all(root, "fix + tautological test")

            p = subprocess.run(
                [str(self.REPO_ROOT / "scripts" / "merge_gate.sh"), base, head, str(root)],
                capture_output=True, text=True)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn("did not fail (BAD)", p.stdout)

    def test_make_gate_invokes_scripts_merge_gate_sh_on_this_repos_own_smoke_range(self):
        # The real end-to-end wire: `make gate` with NO repo-root override
        # (`scripts/merge_gate.sh`'s `ROOT` then defaults to `SCRIPT_HOME`,
        # i.e. wherever the scaffolding lives) - confirms the Makefile
        # target really execs scripts/merge_gate.sh (not just that the
        # target parses). Fix round 8: this used to run against
        # self.REPO_ROOT itself (the real host repo), whose git worktree
        # operations could race any OTHER agent's concurrent git worktree
        # use on that SAME shared .git, and which only worked from whatever
        # checkout path the test happened to be authored in. A temp copy of
        # the scaffolding, gating a temp fixture repo's own history, is
        # deterministic and reproducible from any checkout.
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: buggy add()")
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, DISCRIMINATING_TEST)
            head = commit_all(root, "fix + a real, discriminating test")

            copy_gate_scaffolding(root, self.REPO_ROOT)
            p = subprocess.run(["make", "gate", f"BASE={base}", f"HEAD={head}"],
                                cwd=root, capture_output=True, text=True, timeout=120)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("| test file |", p.stdout)

    def test_merge_gate_sh_skip_env_var_is_logged_and_exits_zero(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, TAUTOLOGICAL_TEST)
            head = commit_all(root, "fix + tautological test, but skipped")

            p = subprocess.run(
                [str(self.REPO_ROOT / "scripts" / "merge_gate.sh"), base, head, str(root)],
                capture_output=True, text=True, env={**os.environ, "SCOUTER_SKIP_MUTATION_CHECK": "1"})
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("SCOUTER_SKIP_MUTATION_CHECK", p.stderr)
            self.assertIn("WARN", p.stderr)

    def test_make_gate_target_exists_and_requires_base_and_head(self):
        p = subprocess.run(["make", "gate"], cwd=self.REPO_ROOT, capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("BASE", p.stdout + p.stderr)


class DarktableOnlyTestsAreNotAFailure(unittest.TestCase):
    def test_a_darktable_only_changed_test_reads_n_a_not_bad(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root,
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
