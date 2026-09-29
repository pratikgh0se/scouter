# mutation-check: targets-post-date-range -- the venv-resolution fix is new
# in this range (card t_0f1197e6) and its shared-resolver follow-up
# (t_7a05352a) also lands in this range, so no marker is needed; this test
# exercises scripts/_python.sh directly, plus one wiring check per caller
# (scripts/mutation_check.sh, scripts/htc_post_run.sh).
"""Runs tests/intent/gate_venv_resolution.json (Zenkai Rules Z1). Two real
oracles per example (Z2): the resolved interpreter's own reported path
(dir realpath + basename, never `Path.resolve()` -- that follows a venv's
`bin/python3` symlink back to its base interpreter and so can't tell one
venv apart from another built from the same base), AND that interpreter's
own `sys.prefix` when actually run.

Examples with target "direct" exercise scripts/_python.sh's
resolve_scouter_python directly (the real, unmodified file, sourced fresh
per example via a tiny `bash -c` harness -- the harness is not the subject
under test, it only sources the real function and then execs whatever
python it resolved). Examples with target "mutation_check_sh" /
"htc_post_run_sh" are wiring checks: they run a byte-for-byte copy of the
real caller script (production code, not a rewrite or mock) and confirm it
actually sources scripts/_python.sh and surfaces its result -- for
mutation_check.sh via a stub tests/mutation_check.py that reports both
oracles; for htc_post_run.sh (print-only outside --execute, which this
implementer never runs) by reading the interpreter path out of its printed
merge-time-gate command and then independently running THAT interpreter to
get its own sys.prefix.

Fixtures are built fresh in a `tempfile.TemporaryDirectory()` (never inside
this checkout, never a walk to any parent directory) and torn down via
`addClassCleanup`: throwaway git repos/worktrees and real venvs built by
`python3 -m venv --without-pip` (real interpreters, not stub executables --
pip is not needed for a sys.prefix probe).
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
REAL_MUTATION_CHECK_SH = REPO_ROOT / "scripts" / "mutation_check.sh"
REAL_HTC_POST_RUN_SH = REPO_ROOT / "scripts" / "htc_post_run.sh"
REAL_PYTHON_LIB_SH = REPO_ROOT / "scripts" / "_python.sh"

EXAMPLES = json.loads((Path(__file__).parent / "gate_venv_resolution.json").read_text())["examples"]

STUB_MUTATION_CHECK_PY = (
    "import sys\n"
    "print('PYEXE:' + sys.executable)\n"
    "print('PYPREFIX:' + sys.prefix)\n"
)

DIRECT_PROBE_SCRIPT = (
    'source "$1"\n'
    'resolve_scouter_python "$2"\n'
    'exec "$PYTHON" -c "import sys; print(\'PYEXE:\'+sys.executable); print(\'PYPREFIX:\'+sys.prefix)"\n'
)


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True, text=True)


def _head_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _make_venv(path: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(path)],
        check=True, capture_output=True,
    )


def _clean_env(env_extra: dict) -> dict:
    """Never inherits the CALLING shell's own $SCOUTER_PYTHON or $PYTHON
    (I2, R2-I1): each example controls those variables explicitly."""
    env = os.environ.copy()
    env.pop("SCOUTER_PYTHON", None)
    env.pop("PYTHON", None)
    env.update(env_extra)
    return env


def _make_fixture_repo(base: Path, name: str, with_venv: bool) -> tuple[Path, str]:
    """A throwaway git repo with real (unmodified) copies of the two
    subject scripts + scripts/_python.sh and a stub tests/mutation_check.py,
    optionally with a real venv at <repo>/.venv. Returns (repo_path,
    head_sha)."""
    repo = base / name
    (repo / "scripts").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    for src in (REAL_MUTATION_CHECK_SH, REAL_HTC_POST_RUN_SH, REAL_PYTHON_LIB_SH):
        shutil.copyfile(src, repo / "scripts" / src.name)
        os.chmod(repo / "scripts" / src.name, 0o755)
    (repo / "tests" / "mutation_check.py").write_text(STUB_MUTATION_CHECK_PY)

    _run_git(["init", "-q"], repo)
    _run_git(["config", "user.email", "fixture@example.com"], repo)
    _run_git(["config", "user.name", "fixture"], repo)
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-q", "-m", "fixture"], repo)
    sha = _head_sha(repo)

    if with_venv:
        _make_venv(repo / ".venv")
    return repo, sha


def _make_bare_sibling_fixture(base: Path, name: str) -> tuple[Path, str, str]:
    """A bare repo (`<name>.git`) with a `main` commit and a second,
    UNMERGED `batch` commit, plus a `.venv` sitting BESIDE the bare repo
    (its sibling, not any checkout's own venv) -- exactly the shape the
    `.git`-basename guard (M1) must exclude. Returns
    (bare_repo_path, main_sha, batch_sha)."""
    src = base / (name + "_src")
    src.mkdir()
    (src / "README.txt").write_text("fixture\n")
    _run_git(["init", "-q", "-b", "main"], src)
    _run_git(["config", "user.email", "fixture@example.com"], src)
    _run_git(["config", "user.name", "fixture"], src)
    _run_git(["add", "-A"], src)
    _run_git(["commit", "-q", "-m", "main commit"], src)
    main_sha = _head_sha(src)

    _run_git(["checkout", "-q", "-b", "batch"], src)
    (src / "batch.txt").write_text("batch\n")
    _run_git(["add", "-A"], src)
    _run_git(["commit", "-q", "-m", "batch commit"], src)
    batch_sha = _head_sha(src)
    _run_git(["checkout", "-q", "main"], src)

    bare_repo = base / (name + ".git")
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(src), str(bare_repo)],
        check=True, capture_output=True, text=True,
    )

    # Sibling venv: in the bare repo's OWN parent directory, i.e. beside
    # `<name>.git`, never inside a checkout of it.
    _make_venv(base / ".venv")

    return bare_repo, main_sha, batch_sha


def _add_worktree(repo: Path, sha: str, dest: Path) -> None:
    _run_git(["worktree", "add", "--detach", "-q", str(dest), sha], repo)


def _remove_worktree(repo: Path, dest: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(dest)],
        cwd=repo, check=False, capture_output=True, text=True,
    )


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


def _python3_via_path(env_extra: dict) -> tuple[str, str]:
    """What a bare `python3` resolves to via PATH, computed independently
    (never assumed) so the fallback example doesn't hardcode a path."""
    env = _clean_env(env_extra)
    proc = subprocess.run(
        ["python3", "-c", "import sys; print(sys.executable); print(sys.prefix)"],
        env=env, check=True, capture_output=True, text=True, timeout=30,
    )
    out = proc.stdout.splitlines()
    return out[0], out[1]


class GateVenvResolutionIntentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="gate-venv-intent-")
        cls.addClassCleanup(cls._tmp.cleanup)
        base = Path(cls._tmp.name)
        cls.base = base
        cls.main_repo, cls.main_sha = _make_fixture_repo(base, "main_venv_repo", with_venv=True)
        cls.no_venv_repo, cls.no_venv_sha = _make_fixture_repo(base, "no_venv_repo", with_venv=False)
        cls.bare_sibling_repo, cls.bare_sibling_main_sha, cls.bare_sibling_batch_sha = (
            _make_bare_sibling_fixture(base, "sibling_repo")
        )
        # A dedicated SECOND venv for the $SCOUTER_PYTHON example (I1): it
        # must be a distinct real interpreter from BOTH the main venv and
        # the running test's own sys.executable, never sys.executable
        # itself (that is the same interpreter the OLD, buggy script would
        # already fall back to, which made the old example pass by
        # accident regardless of whether the override was honoured).
        cls.override_venv = base / "override_venv"
        _make_venv(cls.override_venv)

    # -- scenario -> (script_home repo/worktree, is-bare-common-dir?) ------
    def _scenario_repo_and_sha(self, scenario: str) -> tuple[Path, str]:
        if scenario == "main_venv":
            return self.main_repo, self.main_sha
        if scenario == "no_venv":
            return self.no_venv_repo, self.no_venv_sha
        if scenario == "bare_repo_sibling":
            return self.bare_sibling_repo, self.bare_sibling_main_sha
        raise AssertionError(f"unknown scenario: {scenario}")

    def _env_extra(self, inp: dict, override_venv: Path) -> dict:
        env_extra: dict = {}
        if inp["scouter_python"] == "override":
            env_extra["SCOUTER_PYTHON"] = str(override_venv / "bin" / "python3")
        if inp.get("ambient_python"):
            # R2-I1: an ambient $PYTHON (e.g. `make gate ... PYTHON=x`, or
            # just a variable left set in a dev shell) must be ignored.
            # Pointed at the override venv precisely so this example fails
            # loudly (picks the wrong, distinguishable venv) if the ambient
            # value ever leaks through again.
            env_extra["PYTHON"] = str(override_venv / "bin" / "python3")
        return env_extra

    def _expected_identity(self, want_resolved_is: str, own_venv_dir: Path | None,
                            env_extra: dict) -> tuple[str, str]:
        if want_resolved_is == "main_venv":
            return (
                _identity(str(self.main_repo / ".venv" / "bin" / "python3")),
                _identity(str(self.main_repo / ".venv")),
            )
        if want_resolved_is == "override":
            return (
                _identity(str(self.override_venv / "bin" / "python3")),
                _identity(str(self.override_venv)),
            )
        if want_resolved_is == "own_venv":
            assert own_venv_dir is not None
            return (
                _identity(str(own_venv_dir / "bin" / "python3")),
                _identity(str(own_venv_dir)),
            )
        # "system" -- computed independently, never hardcoded
        sys_exe, sys_prefix = _python3_via_path(env_extra)
        return _identity(sys_exe), _identity(sys_prefix)

    # -- target: direct (scripts/_python.sh itself) ------------------------
    def _run_direct(self, ex: dict) -> None:
        inp, want = ex["input"], ex["expect"]
        repo, sha = self._scenario_repo_and_sha(inp["scenario"])
        wt = self.base / ("wt_" + ex["id"])
        _add_worktree(repo, sha, wt)
        try:
            own_venv_dir = None
            if inp.get("own_venv"):
                own_venv_dir = wt / ".venv"
                _make_venv(own_venv_dir)

            env_extra = self._env_extra(inp, self.override_venv)
            env = _clean_env(env_extra)
            proc = subprocess.run(
                ["bash", "-c", DIRECT_PROBE_SCRIPT, "probe", str(REAL_PYTHON_LIB_SH), str(wt)],
                env=env, check=True, capture_output=True, text=True, timeout=30,
            )
            pyexe, pyprefix = self._parse_probe_output(proc.stdout)

            want_exe, want_prefix = self._expected_identity(want["resolved_is"], own_venv_dir, env_extra)
            # Oracle 1: the resolved interpreter's own reported path.
            self.assertEqual(_identity(pyexe), want_exe, ex["id"])
            # Oracle 2: that interpreter's own sys.prefix when actually run.
            self.assertEqual(_identity(pyprefix), want_prefix, ex["id"])
        finally:
            _remove_worktree(repo, wt)

    @staticmethod
    def _parse_probe_output(stdout: str) -> tuple[str, str]:
        pyexe = pyprefix = None
        for line in stdout.splitlines():
            if line.startswith("PYEXE:"):
                pyexe = line[len("PYEXE:"):]
            elif line.startswith("PYPREFIX:"):
                pyprefix = line[len("PYPREFIX:"):]
        assert pyexe and pyprefix, f"probe did not report both oracles: {stdout!r}"
        return pyexe, pyprefix

    # -- target: mutation_check_sh (wiring check) ---------------------------
    def _run_mutation_check_sh(self, ex: dict) -> None:
        inp, want = ex["input"], ex["expect"]
        repo, sha = self._scenario_repo_and_sha(inp["scenario"])
        wt = self.base / ("wt_" + ex["id"])
        _add_worktree(repo, sha, wt)
        try:
            own_venv_dir = None
            if inp.get("own_venv"):
                own_venv_dir = wt / ".venv"
                _make_venv(own_venv_dir)

            env_extra = self._env_extra(inp, self.override_venv)
            env = _clean_env(env_extra)
            proc = subprocess.run(
                ["scripts/mutation_check.sh", "x", "y", str(wt)],
                cwd=wt, env=env, check=True, capture_output=True, text=True, timeout=30,
            )
            pyexe, pyprefix = self._parse_probe_output(proc.stdout)

            want_exe, want_prefix = self._expected_identity(want["resolved_is"], own_venv_dir, env_extra)
            self.assertEqual(_identity(pyexe), want_exe, ex["id"])
            self.assertEqual(_identity(pyprefix), want_prefix, ex["id"])
        finally:
            _remove_worktree(repo, wt)

    # -- target: htc_post_run_sh (wiring check) ------------------------------
    def _run_htc_post_run_sh(self, ex: dict) -> None:
        inp, want = ex["input"], ex["expect"]
        assert inp["scenario"] == "bare_repo_sibling", "htc_post_run_sh wiring example expects bare_repo_sibling"
        wt = self.base / ("wt_" + ex["id"])
        _add_worktree(self.bare_sibling_repo, self.bare_sibling_main_sha, wt)
        try:
            run_dir = self.base / ("rundir_" + ex["id"])
            run_dir.mkdir()
            (run_dir / "progress.json").write_text(json.dumps({
                "status": "done",
                "layers": {"offline": {"status": "done"}},
            }))

            env_extra = self._env_extra(inp, self.override_venv)
            env = _clean_env(env_extra)
            env.update({
                "HTC_REPO": str(wt),
                "BATCH": self.bare_sibling_batch_sha,
                "RUN_DIR": str(run_dir),
                "HTC_EXPECTED_LAYERS": "offline",
                "PGREP": "/usr/bin/false",
            })
            proc = subprocess.run(
                ["bash", str(REAL_HTC_POST_RUN_SH.parent / "htc_post_run.sh")],
                env=env, check=True, capture_output=True, text=True, timeout=30,
            )

            py_path = None
            for line in proc.stdout.splitlines():
                if "mutation_check.py" in line:
                    py_path = line.split()[0]
                    break
            assert py_path, f"no printed gate command found: {proc.stdout!r}"

            probe = subprocess.run(
                [py_path, "-c", "import sys; print(sys.executable); print(sys.prefix)"],
                env=_clean_env({}), check=True, capture_output=True, text=True, timeout=30,
            )
            pyexe, pyprefix = probe.stdout.splitlines()

            want_exe, want_prefix = self._expected_identity(want["resolved_is"], None, env_extra)
            # Oracle 1: the printed interpreter's own reported path when
            # actually run (py_path may be a bare `python3` resolved via
            # PATH -- sys.executable is that resolution's real, absolute
            # result, so it is the identity that is actually comparable).
            self.assertEqual(_identity(pyexe), want_exe, ex["id"])
            # Oracle 2: that same interpreter's own sys.prefix when run.
            self.assertEqual(_identity(pyprefix), want_prefix, ex["id"])
        finally:
            _remove_worktree(self.bare_sibling_repo, wt)

    def run_example(self, ex: dict) -> None:
        target = ex["target"]
        if target == "direct":
            self._run_direct(ex)
        elif target == "mutation_check_sh":
            self._run_mutation_check_sh(ex)
        elif target == "htc_post_run_sh":
            self._run_htc_post_run_sh(ex)
        else:
            raise AssertionError(f"unknown target: {target}")


def _make(ex: dict):
    def test(self: GateVenvResolutionIntentTest) -> None:
        self.run_example(ex)
    return test


for _ex in EXAMPLES:
    setattr(GateVenvResolutionIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))
