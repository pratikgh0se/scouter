# mutation-check: targets-post-date-range -- the venv-resolution fix is new
# in this range (card t_0f1197e6), so no marker is needed; this test
# exercises scripts/mutation_check.sh directly.
"""Runs tests/intent/gate_venv_resolution.json (Zenkai Rules Z1). Two real
oracles per example (Z2): the resolved interpreter's own reported path
(dir realpath + basename, never `Path.resolve()` -- that follows a venv's
`bin/python3` symlink back to its base interpreter and so can't tell one
venv apart from another built from the same base), AND that interpreter's
own `sys.prefix` when actually run (scripts/mutation_check.sh's PYTHON
resolution chain, card t_0f1197e6 / row 478 fix round 1).

Fixtures are built fresh in a `tempfile.TemporaryDirectory()` (never inside
this checkout, never a walk to any parent directory) and torn down via
`addClassCleanup`: a throwaway git repo holding a byte-for-byte copy of the
real scripts/mutation_check.sh (production code, not a rewrite or mock),
real venvs built by `python3 -m venv --without-pip` (real interpreters, not
stub executables -- pip is not needed for a sys.prefix probe), and a linked
`git worktree` with no venv of its own -- the exact shape of the bug.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_SCRIPT = REPO_ROOT / "scripts" / "mutation_check.sh"

EXAMPLES = json.loads((Path(__file__).parent / "gate_venv_resolution.json").read_text())["examples"]

STUB_MUTATION_CHECK_PY = (
    "import sys\n"
    "print('PYEXE:' + sys.executable)\n"
    "print('PYPREFIX:' + sys.prefix)\n"
)


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True, text=True)


def _make_venv(path: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(path)],
        check=True, capture_output=True,
    )


def _make_fixture_repo(base: Path, name: str, with_venv: bool) -> tuple[Path, str]:
    """A throwaway git repo with a real (unmodified) copy of the subject
    script and a stub tests/mutation_check.py, optionally with a real venv
    at <repo>/.venv. Returns (repo_path, head_sha)."""
    repo = base / name
    (repo / "scripts").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    shutil.copyfile(REAL_SCRIPT, repo / "scripts" / "mutation_check.sh")
    os.chmod(repo / "scripts" / "mutation_check.sh", 0o755)
    (repo / "tests" / "mutation_check.py").write_text(STUB_MUTATION_CHECK_PY)

    _run_git(["init", "-q"], repo)
    _run_git(["config", "user.email", "fixture@example.com"], repo)
    _run_git(["config", "user.name", "fixture"], repo)
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-q", "-m", "fixture"], repo)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    if with_venv:
        _make_venv(repo / ".venv")
    return repo, sha


def _add_worktree(repo: Path, sha: str, dest: Path) -> None:
    _run_git(["worktree", "add", "--detach", "-q", str(dest), sha], repo)


def _remove_worktree(repo: Path, dest: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(dest)],
        cwd=repo, check=False, capture_output=True, text=True,
    )


def _resolve(worktree: Path, env_extra: dict) -> tuple[str, str]:
    """Runs the subject script from `worktree` and returns
    (resolved_pyexe, resolved_pyprefix) as reported by the stub it execs.
    Never inherits the CALLING shell's own $SCOUTER_PYTHON or $PYTHON (I2,
    R2-I1): each example controls those variables explicitly via
    env_extra."""
    env = os.environ.copy()
    env.pop("SCOUTER_PYTHON", None)
    env.pop("PYTHON", None)
    env.update(env_extra)
    proc = subprocess.run(
        ["scripts/mutation_check.sh", "x", "y", str(worktree)],
        cwd=worktree, env=env, check=True, capture_output=True, text=True, timeout=30,
    )
    pyexe = pyprefix = None
    for line in proc.stdout.splitlines():
        if line.startswith("PYEXE:"):
            pyexe = line[len("PYEXE:"):]
        elif line.startswith("PYPREFIX:"):
            pyprefix = line[len("PYPREFIX:"):]
    assert pyexe and pyprefix, f"stub did not report both oracles: {proc.stdout!r} / {proc.stderr!r}"
    return pyexe, pyprefix


def _python3_via_path(env_extra: dict) -> tuple[str, str]:
    """What a bare `python3` resolves to via PATH, computed independently
    (never assumed) so the fallback example doesn't hardcode a path. Also
    never inherits the caller's own $SCOUTER_PYTHON or $PYTHON (I2,
    R2-I1)."""
    env = os.environ.copy()
    env.pop("SCOUTER_PYTHON", None)
    env.pop("PYTHON", None)
    env.update(env_extra)
    proc = subprocess.run(
        ["python3", "-c", "import sys; print(sys.executable); print(sys.prefix)"],
        env=env, check=True, capture_output=True, text=True, timeout=30,
    )
    out = proc.stdout.splitlines()
    return out[0], out[1]


def _identity(p: str) -> str:
    """Oracle-safe path identity (I1): realpath of the CONTAINING
    directory (collapses macOS's /var -> /private/var etc.) joined with
    the reported basename, WITHOUT following the final path component if
    it is itself a symlink -- `Path.resolve()` chases a venv's
    `bin/python3` symlink all the way back to its base interpreter, which
    makes every venv built from the same base interpreter compare equal
    to that interpreter (and to each other). A venv's own `sys.executable`
    already reports the venv's own path (not the base interpreter's), so
    this identity is enough to tell two venvs -- or a venv and the bare
    PATH interpreter -- apart."""
    path = Path(p)
    return str(path.parent.resolve() / path.name)


class GateVenvResolutionIntentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="gate-venv-intent-")
        cls.addClassCleanup(cls._tmp.cleanup)
        base = Path(cls._tmp.name)
        cls.base = base
        cls.main_repo, cls.main_sha = _make_fixture_repo(base, "main_venv_repo", with_venv=True)
        cls.bare_repo, cls.bare_sha = _make_fixture_repo(base, "no_venv_repo", with_venv=False)
        # A dedicated SECOND venv for the $SCOUTER_PYTHON example (I1): it
        # must be a distinct real interpreter from BOTH the main venv and
        # the running test's own sys.executable, never sys.executable
        # itself (that is the same interpreter the OLD, buggy script would
        # already fall back to, which made the old example pass by
        # accident regardless of whether the override was honoured).
        cls.override_venv = base / "override_venv"
        _make_venv(cls.override_venv)

    def run_example(self, ex: dict) -> None:
        inp, want = ex["input"], ex["expect"]
        wt = self.base / ("wt_" + ex["id"])
        if inp["scenario"] == "main_venv":
            repo, sha = self.main_repo, self.main_sha
        else:
            repo, sha = self.bare_repo, self.bare_sha
        _add_worktree(repo, sha, wt)
        try:
            own_venv_dir = None
            if inp.get("own_venv"):
                own_venv_dir = wt / ".venv"
                _make_venv(own_venv_dir)

            env_extra = {}
            if inp["scouter_python"] == "override":
                env_extra["SCOUTER_PYTHON"] = str(self.override_venv / "bin" / "python3")
            if inp.get("ambient_python"):
                # R2-I1: an ambient $PYTHON (e.g. `make gate ... PYTHON=x`,
                # or just a variable left set in a dev shell) must be
                # ignored -- the script's own resolution chain always
                # decides PYTHON, never the caller's pre-existing value.
                # Pointed at the override venv precisely so this example
                # fails loudly (picks the wrong, distinguishable venv) if
                # the ambient value ever leaks through again.
                env_extra["PYTHON"] = str(self.override_venv / "bin" / "python3")
            pyexe, pyprefix = _resolve(wt, env_extra)

            if want["resolved_is"] == "main_venv":
                want_exe = _identity(str(self.main_repo / ".venv" / "bin" / "python3"))
                want_prefix = _identity(str(self.main_repo / ".venv"))
            elif want["resolved_is"] == "override":
                want_exe = _identity(str(self.override_venv / "bin" / "python3"))
                want_prefix = _identity(str(self.override_venv))
            elif want["resolved_is"] == "own_venv":
                want_exe = _identity(str(own_venv_dir / "bin" / "python3"))
                want_prefix = _identity(str(own_venv_dir))
            else:  # "system" -- computed independently, never hardcoded
                sys_exe, sys_prefix = _python3_via_path(env_extra)
                want_exe = _identity(sys_exe)
                want_prefix = _identity(sys_prefix)

            # Oracle 1: the resolved interpreter's own reported path.
            self.assertEqual(_identity(pyexe), want_exe, ex["id"])
            # Oracle 2: that interpreter's own sys.prefix when actually run.
            self.assertEqual(_identity(pyprefix), want_prefix, ex["id"])
        finally:
            _remove_worktree(repo, wt)


def _make(ex: dict):
    def test(self: GateVenvResolutionIntentTest) -> None:
        self.run_example(ex)
    return test


for _ex in EXAMPLES:
    setattr(GateVenvResolutionIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))
