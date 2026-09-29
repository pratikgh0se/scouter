# mutation-check: targets-post-date-range -- t_0f1197e6 and its follow-up
# t_7a05352a both land in this range, so no marker is needed.
"""Runs tests/intent/gate_venv_resolution.json (Zenkai Z1). Two oracles per
example (Z2): the resolved interpreter's own reported path, and its own
sys.prefix when actually run. "direct" examples source the real
scripts/_python.sh via a bash -c harness; "mutation_check_sh" /
"htc_post_run_sh" examples run a real copy (or, for htc_post_run.sh, the
real file in place) to confirm each caller wires in the shared resolver.
Fixtures: a tempfile.TemporaryDirectory() with real git repos/worktrees and
real venvs (python3 -m venv --without-pip), torn down via addClassCleanup."""

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
    # I2, R2-I1: never inherit the CALLING shell's own SCOUTER_PYTHON/PYTHON.
    env = os.environ.copy()
    env.pop("SCOUTER_PYTHON", None)
    env.pop("PYTHON", None)
    env.update(env_extra)
    return env

def _make_fixture_repo(base: Path, name: str, with_venv: bool) -> tuple[Path, str]:
    """A throwaway git repo with real copies of mutation_check.sh +
    _python.sh and a stub tests/mutation_check.py, optionally with a real
    venv at <repo>/.venv."""
    repo = base / name
    (repo / "scripts").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    for src in (REAL_MUTATION_CHECK_SH, REAL_PYTHON_LIB_SH):
        shutil.copyfile(src, repo / "scripts" / src.name)
        os.chmod(repo / "scripts" / src.name, 0o755)
    (repo / "tests" / "mutation_check.py").write_text(STUB_MUTATION_CHECK_PY)

    _run_git(["init", "-q", "-b", "main"], repo)
    _run_git(["config", "user.email", "fixture@example.com"], repo)
    _run_git(["config", "user.name", "fixture"], repo)
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-q", "-m", "fixture"], repo)
    sha = _head_sha(repo)

    if with_venv:
        _make_venv(repo / ".venv")
    return repo, sha

def _make_bare_sibling_fixture(base: Path, name: str) -> tuple[Path, str, str]:
    # A bare clone with a sibling .venv beside it -- the .git-basename
    # guard (M1) must exclude this, unlike a real main checkout's .venv.
    src, main_sha = _make_fixture_repo(base, name + "_src", with_venv=False)
    _run_git(["checkout", "-q", "-b", "batch"], src)
    _run_git(["commit", "-q", "--allow-empty", "-m", "batch"], src)
    batch_sha = _head_sha(src)
    _run_git(["checkout", "-q", "main"], src)

    bare_repo = base / (name + ".git")
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(src), str(bare_repo)],
        check=True, capture_output=True, text=True,
    )
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
    # Dir realpath + basename; never resolve() the final symlink, it
    # collapses venvs onto their shared base interpreter.
    path = Path(p)
    return str(path.parent.resolve() / path.name)

def _python3_via_path(env_extra: dict) -> tuple[str, str]:
    """What a bare python3 resolves to via PATH, computed independently so
    the fallback example doesn't hardcode a path."""
    env = _clean_env(env_extra)
    proc = subprocess.run(
        ["python3", "-c", "import sys; print(sys.executable); print(sys.prefix)"],
        env=env, check=True, capture_output=True, text=True, timeout=30,
    )
    out = proc.stdout.splitlines()
    return out[0], out[1]

def _parse_probe_output(stdout: str) -> tuple[str, str]:
    pyexe = pyprefix = None
    for line in stdout.splitlines():
        if line.startswith("PYEXE:"):
            pyexe = line[len("PYEXE:"):]
        elif line.startswith("PYPREFIX:"):
            pyprefix = line[len("PYPREFIX:"):]
    assert pyexe and pyprefix, f"probe did not report both oracles: {stdout!r}"
    return pyexe, pyprefix


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
        # I1: a distinct real interpreter, never sys.executable itself.
        cls.override_venv = base / "override_venv"
        _make_venv(cls.override_venv)

    def _scenario_repo_and_sha(self, scenario: str) -> tuple[Path, str]:
        if scenario == "main_venv":
            return self.main_repo, self.main_sha
        if scenario == "no_venv":
            return self.no_venv_repo, self.no_venv_sha
        if scenario == "bare_repo_sibling":
            return self.bare_sibling_repo, self.bare_sibling_main_sha
        raise AssertionError(f"unknown scenario: {scenario}")

    def _env_extra(self, inp: dict) -> dict:
        # R2-I1: ambient PYTHON must be ignored; both point at the override
        # venv so a leak (either variable) fails loudly, not silently.
        env_extra: dict = {}
        if inp["scouter_python"] == "override":
            env_extra["SCOUTER_PYTHON"] = str(self.override_venv / "bin" / "python3")
        if inp.get("ambient_python"):
            env_extra["PYTHON"] = str(self.override_venv / "bin" / "python3")
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
        sys_exe, sys_prefix = _python3_via_path(env_extra)  # "system"
        return _identity(sys_exe), _identity(sys_prefix)

    def _command(self, target: str, wt: Path, ex: dict) -> tuple[list[str], Path | None, dict]:
        """Returns (argv, cwd, extra_env) for `target`."""
        if target == "direct":
            return (
                ["bash", "-c", DIRECT_PROBE_SCRIPT, "probe", str(REAL_PYTHON_LIB_SH), str(wt)],
                None, {},
            )
        if target == "mutation_check_sh":
            return (["scripts/mutation_check.sh", "x", "y", str(wt)], wt, {})
        if target == "htc_post_run_sh":
            run_dir = self.base / ("rundir_" + ex["id"])
            run_dir.mkdir()
            (run_dir / "progress.json").write_text(json.dumps({
                "status": "done", "layers": {"offline": {"status": "done"}},
            }))
            extra_env = {
                "HTC_REPO": str(wt),
                "BATCH": self.bare_sibling_batch_sha,
                "RUN_DIR": str(run_dir),
                "HTC_EXPECTED_LAYERS": "offline",
                "PGREP": "/usr/bin/false",
            }
            return (["bash", str(REAL_HTC_POST_RUN_SH)], None, extra_env)
        raise AssertionError(f"unknown target: {target}")

    def _parse(self, target: str, stdout: str) -> tuple[str, str]:
        if target in ("direct", "mutation_check_sh"):
            return _parse_probe_output(stdout)
        # htc_post_run_sh is print-only: read the interpreter out of its
        # printed gate command, then run THAT interpreter for both oracles.
        py_path = next((line.split()[0] for line in stdout.splitlines()
                         if "mutation_check.py" in line), None)
        assert py_path, f"no printed gate command found: {stdout!r}"
        probe = subprocess.run(
            [py_path, "-c", "import sys; print(sys.executable); print(sys.prefix)"],
            env=_clean_env({}), check=True, capture_output=True, text=True, timeout=30,
        )
        pyexe, pyprefix = probe.stdout.splitlines()
        return pyexe, pyprefix

    def run_example(self, ex: dict) -> None:
        inp, want, target = ex["input"], ex["expect"], ex["target"]
        repo, sha = self._scenario_repo_and_sha(inp["scenario"])
        wt = self.base / ("wt_" + ex["id"])
        _add_worktree(repo, sha, wt)
        try:
            own_venv_dir = None
            if inp.get("own_venv"):
                own_venv_dir = wt / ".venv"
                _make_venv(own_venv_dir)

            env_extra = self._env_extra(inp)
            env = _clean_env(env_extra)
            argv, cwd, extra_env = self._command(target, wt, ex)
            env.update(extra_env)
            proc = subprocess.run(argv, cwd=cwd, env=env, check=True,
                                   capture_output=True, text=True, timeout=30)
            pyexe, pyprefix = self._parse(target, proc.stdout)

            want_exe, want_prefix = self._expected_identity(want["resolved_is"], own_venv_dir, env_extra)
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
