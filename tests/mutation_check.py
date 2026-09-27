#!/usr/bin/env python3
"""Test-honesty merge-gate step for scouter, mirroring capsule-corp's
`tests/mutation_check.py` (decision row 459 in the capsule-corp vault,
card t_a995978e, memory rule `test-honesty-gate`): a new or changed test that
can never fail proves nothing. Same three checks, run with pytest instead of
unittest (scouter's suite is `python3 -m pytest tests -k "not host"`):

  (b) revert-mutation: build a scratch worktree at `--head`, then check out
      the `--base` version of every non-test `.py` file the range touched
      (the new tests stay as HEAD wrote them). Run only the new/changed tests
      via their pytest node ids, with `-k "not host"` (host tests need real
      darktable and never run here). When the range touched at least one
      production file, every changed test FILE must show at least one
      failure - a file whose tests still pass with the production change
      reverted is not discriminating and is reported by name. A file whose
      only new/changed tests are host-only (filtered out by `-k "not host"`)
      is reported `n/a`, not a failure.

  (c) assertion-weakening mutation: for each changed test file, in a second,
      unmodified (HEAD) scratch worktree, flip the first `assertEqual` to
      `assertNotEqual` (or `assertTrue` to `assertFalse`, or a bare
      `assert a == b` to `assert a != b`) INSIDE one of that file's own
      new/changed tests, and confirm that alone makes the file's changed
      tests fail.

  (d) static flags on the new/changed test functions: no assert at all; a
      `@pytest.mark.skip`/`skipif`/`xfail` decorator; a bare `except: pass`
      (or `except Exception: pass`); `assertTrue(True)` / a bare `assert True`.

Zenkai Z1/Z2 equivalent for this script: `tests/test_mutation_check.py`,
exercised against a tiny fixture repo built in tempfile - a discriminating
test passes this gate, a tautological one fails it.

A test file whose subject genuinely predates the range it is committed in can
opt out of (b) revert-mutation with a `# mutation-check: targets-predate-range
<sha> -- <reason> :: Class.test_a, Class.test_b` top-of-file marker (round 5,
mirrored from capsule-corp). Round 13: this marker is per-TEST, not per-file -
it must name the exact `Class.test_name` (or bare `test_name`) ids it covers
after a literal `::`; only those ids read `n/a (targets predate range)`,
every OTHER new/changed test in the same file is still revert-checked below
it. A marker with no `::` ids at all (the old file-level shape), or one
naming an id that is not in this range's new/changed set for that file, is
`marker_stale` - a gate failure, exactly like a sha that does not match
`--base`, never silently downgraded to n/a.

Usage: tests/mutation_check.py --base <sha> --head <sha> [--repo .]
Exit 0 and a clean markdown table when every changed test file discriminates
and no static flag fired; exit 1 and the table naming what didn't otherwise.
stdlib only for the checking logic - `pytest` itself is scouter's existing
test dependency (pyproject.toml), not a new one this script adds.

Every internal `pytest` subprocess (`run_pytest`) uses a deterministic
per-call timeout, `max(180, 10 * len(node_ids))` seconds (round 11,
`pytest_timeout_budget`) - no per-file markers or overrides. A selection of
N test ids scales its own budget instead of sharing one fixed 180s ceiling
regardless of size, which this repo's OWN 48-test self-suite (below) can
exceed when a merge_gate run over a large range selects nearly all of them
as "new/changed" at once.
"""
import argparse
import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TAUTOLOGY_RE = re.compile(r"assertTrue\(\s*True\s*\)|(?<!\w)assert\s+True\s*(?:,|$)")
SKIP_MARK_RE = re.compile(r"@pytest\.mark\.(skip|skipif|xfail)\b")
BARE_EXCEPT_RE = re.compile(r"except(?:\s+Exception)?\s*(?:as\s+\w+\s*)?:\s*\n(\s+)pass\b")
ASSERT_EQ_RE = re.compile(r"\bassertEqual\(")
ASSERT_TRUE_RE = re.compile(r"\bassertTrue\(")
BARE_ASSERT_EQ_RE = re.compile(r"(?<!\w)assert\s+(.+?)\s==\s(.+?)\s*(#.*)?$", re.MULTILINE)
NO_TESTS_COLLECTED_RC = 5


def sh(argv, cwd=None, timeout=300, input_text=None):
    # Round 12 (B5 debugging): the revert- and weaken-mutation phases rewrite
    # the SAME on-disk path several times in quick succession (revert -> run
    # -> restore; then each weaken candidate in turn) - and two different
    # mutations can produce a file of the IDENTICAL byte size (e.g.
    # `assertEqual`->`assertNotEqual` on line N vs the same flip on line M).
    # Python/pytest's bytecode cache keys a cached `.pyc` on (mtime, size);
    # with an identical size and a write fast enough to land on the same
    # mtime tick, the second run can silently reuse the FIRST mutation's
    # already-compiled (and already-exercised) bytecode instead of the file
    # that is actually on disk right now - a real, observed flake (the same
    # fixture passed or failed depending on timing alone). Every subprocess
    # this gate spawns must therefore never write - and never trust a
    # pre-existing - `.pyc`: force `PYTHONDONTWRITEBYTECODE=1` regardless of
    # the caller's own environment.
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        p = subprocess.run(argv, cwd=str(cwd) if cwd else None, capture_output=True,
                            text=True, timeout=timeout, input=input_text, env=env)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as exc:
        # B0: an interpreter path that does not exist (or is not executable)
        # must read as a runner problem, never crash the gate.
        return 127, str(exc)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# Fix round 1 (review): basename alone (`test_*.py` anywhere in the tree) was
# too loose - scoped to this repo's actual pytest collection roots.
TEST_DIRS = ("tests/", "gauntlet/tests/", "tests/gauntlet/")


def is_test_file(rel_path):
    rel_path = str(rel_path).replace("\\", "/")
    name = Path(rel_path).name
    if not (name.startswith("test_") and name.endswith(".py")):
        return False
    return rel_path.startswith(TEST_DIRS)


_SUPPORT_BASENAMES = ("conftest.py", "__init__.py")
_SUPPORT_NAME_HINTS = ("fake", "mock", "stub")


def is_test_support_file(rel_path):
    """A `.py` file under a test directory that is test-DOUBLE support, not
    production - `conftest.py`, `tests/**/__init__.py`, anything under a
    `fixtures/` directory, or a fakes/mocks/stubs module (e.g.
    `tests/scouter_fakes.py`). Fix round 3 (audit, fix/htc-applied-verify):
    these must never be reverted (that would test against a stale double, not
    the actual bug) or required to "discriminate" (they are not tests).

    Deliberately narrower than "any non-test_*.py file under a test
    directory": `tests/gauntlet/observe.py` and `tests/gauntlet/execute.py`
    are real harness code with real behavior (and real bugs to catch) that
    merely lives under `tests/` for organizational reasons - excluding them
    from mutation testing entirely would silently blind the gate on exactly
    the files `tests/gauntlet/test_execute.py` exists to cover."""
    rel_path = str(rel_path).replace("\\", "/")
    if not rel_path.endswith(".py"):
        return False
    if not rel_path.startswith(TEST_DIRS) or is_test_file(rel_path):
        return False
    name = Path(rel_path).name
    if name in _SUPPORT_BASENAMES:
        return True
    if "fixtures" in rel_path.split("/"):
        return True
    stem = name[:-3].lower()
    return any(hint in stem for hint in _SUPPORT_NAME_HINTS)


def changed_files(repo, base, head):
    rc, out = sh(["git", "diff", "--name-only", base, head], cwd=repo)
    if rc != 0:
        raise RuntimeError(f"git diff --name-only {base} {head} failed: {out}")
    return [l.strip() for l in out.splitlines() if l.strip()]


def file_status(repo, base, head, path):
    rc, out = sh(["git", "diff", "--name-status", base, head, "--", path], cwd=repo)
    if rc != 0 or not out.strip():
        return "M"
    return out.strip().splitlines()[0].split("\t", 1)[0][0]


def show_file(repo, ref, path):
    rc, out = sh(["git", "show", f"{ref}:{path}"], cwd=repo)
    return out if rc == 0 else None


def added_line_numbers(repo, base, head, path):
    rc, out = sh(["git", "diff", "-U0", base, head, "--", path], cwd=repo)
    if rc != 0:
        return set()
    lines = set()
    for line in out.splitlines():
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                lines.update(range(start, start + count))
    return lines


def test_defs(source):
    """{(class_or_None, funcname): (lineno, end_lineno)} for every test* def
    pytest would actually collect - a module-level function, or a method
    directly in a class body. Fix round 10 (B3, HTC gate-rows audit): a
    `def test(self)` NESTED inside another function (e.g. a `_make()`
    factory whose closures get `setattr`-copied onto a class, one per data
    row) is NOT itself a collected item - pytest only ever sees the closure
    under whatever name `setattr` gave it. Counting the nested def as a
    fake "test" node produced real false positives: a bogus "no assert"
    static flag on the factory closure, and a weaken run against a node id
    (`Class::test`) pytest reports "no match" for (rc 4, silently read as a
    false-green "FAIL (good)" before fix round 10's B0). `func_depth` tracks
    nesting inside ANY function/method (not just class methods), so a
    closure is excluded regardless of whether its enclosing function is
    itself module-level or a method."""
    out = {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.cls = None
            self.func_depth = 0

        def visit_ClassDef(self, node):
            prev, self.cls = self.cls, node.name
            self.generic_visit(node)
            self.cls = prev

        def _fn(self, node):
            if self.func_depth == 0 and node.name.startswith("test"):
                out[(self.cls, node.name)] = (node.lineno, getattr(node, "end_lineno", node.lineno))
            self.func_depth += 1
            self.generic_visit(node)
            self.func_depth -= 1

        visit_FunctionDef = _fn
        visit_AsyncFunctionDef = _fn

    Visitor().visit(tree)
    return out


def nested_test_like_defs(source):
    """{(enclosing_class_or_None, funcname): (lineno, end_lineno)} for every
    `def test*` NESTED inside another function - the shape `test_defs`
    deliberately excludes (B3). Used only to notice "a changed range touched
    a nested test-shaped closure" so its enclosing class's REAL collected
    items (dynamically generated, one per data row) can be treated as
    changed too - see `changed_test_functions`."""
    out = {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.cls = None
            self.func_depth = 0

        def visit_ClassDef(self, node):
            prev, self.cls = self.cls, node.name
            self.generic_visit(node)
            self.cls = prev

        def _fn(self, node):
            if self.func_depth > 0 and node.name.startswith("test"):
                out[(self.cls, node.name)] = (node.lineno, getattr(node, "end_lineno", node.lineno))
            self.func_depth += 1
            self.generic_visit(node)
            self.func_depth -= 1

        visit_FunctionDef = _fn
        visit_AsyncFunctionDef = _fn

    Visitor().visit(tree)
    return out


def collected_item_names(root, rel_path, python, timeout=60):
    """Real pytest node ids `pytest --collect-only -q` reports for
    `rel_path`, as a `{class_or_None: [method_or_function, ...]}` map (B3).
    Never raises - a failed collection just yields `{}`, so callers fall
    back to the AST-only view rather than crash."""
    rc, out = sh([python, "-m", "pytest", "--collect-only", "-q", rel_path],
                 cwd=root, timeout=timeout)
    result = {}
    for line in (out or "").splitlines():
        line = line.strip()
        if "::" not in line or not line.startswith(rel_path):
            continue
        parts = line.split("::")
        if len(parts) == 2:
            result.setdefault(None, []).append(parts[1])
        elif len(parts) >= 3:
            result.setdefault(parts[1], []).append(parts[-1])
    return result


def changed_test_functions(repo, base, head, path, status, added):
    """[(class_or_None, name, lineno, end_lineno)] new/changed test functions."""
    head_src = show_file(repo, head, path)
    if head_src is None:
        return []
    defs = test_defs(head_src)
    if status == "A":
        return [(cls, name, s, e) for (cls, name), (s, e) in defs.items()]
    out = []
    for (cls, name), (s, e) in defs.items():
        if any(s <= ln <= e for ln in added):
            out.append((cls, name, s, e))
    return out


def node_id(rel_path, cls, name):
    return f"{rel_path}::{cls}::{name}" if cls else f"{rel_path}::{name}"


def make_worktree(repo, ref, base_dir):
    wt = base_dir / "wt"
    rc, out = sh(["git", "worktree", "add", "--detach", "-q", str(wt), ref], cwd=repo)
    if rc != 0:
        raise RuntimeError(f"git worktree add {ref} failed: {out}")
    return wt


def remove_worktree(repo, wt):
    sh(["git", "worktree", "remove", "--force", str(wt)], cwd=repo)


def revert_to_base(repo, base, scratch, path, status):
    target = scratch / path
    if status == "A":
        if target.exists():
            target.unlink()
        return
    content = show_file(repo, base, path)
    if content is None:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def gate_python(repo):
    """The interpreter EVERY pytest subprocess this gate spawns must use:
    `<repo>/.venv/bin/python` when the repo being checked has one, else
    `sys.executable` (whichever interpreter is running this gate process
    right now) - never a bare "python3" resolved via the CALLER's ambient
    PATH (fix round 9's root cause, promoted to round 10's B0: the HTC
    integrator's gate ran under Homebrew's system python3, which has no
    pytest; every internal pytest subprocess then exited 1 with "No module
    named pytest", and the tool read that bare non-zero exit as a genuine
    test failure - "FAIL (good)" on every single row, a false green/red
    that never actually discriminated anything). Resolved once, from the
    ORIGINAL `--repo` (which is what a real caller's venv lives next to) -
    a scratch worktree never gets a copy of an untracked `.venv/` of its
    own, so reusing that same absolute path for every subprocess, regardless
    of which throwaway worktree is the `cwd`, is what makes this work."""
    venv_python = Path(repo).resolve() / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def check_pytest_available(python, cwd):
    """(ok, detail). B0's mandatory precheck, run once before any check:
    `<python> -m pytest --version`. A gate that cannot even confirm pytest
    is importable must refuse outright (exit 2, "environment error") -
    never silently produce a table that looks like a real verdict."""
    rc, out = sh([python, "-m", "pytest", "--version"], cwd=str(cwd), timeout=30)
    if rc != 0:
        return False, f"pytest unavailable under {python}: {(out or '').strip()[:300]}"
    return True, (out or "").strip()


# B0: pytest's own summary line ("3 failed, 2 passed in 0.12s", "1 error in
# 0.01s", ...). A run counts as a genuine, discriminating FAILURE only when
# rc == 1 AND this line reports >=1 "failed" - never rc alone, which a
# broken interpreter, an internal error or a bad node id can also produce.
PYTEST_FAILED_SUMMARY_RE = re.compile(r"\b(\d+) failed\b")


def classify_pytest_outcome(rc, out):
    """"passed" (rc 0: nothing failed - the mutation did NOT discriminate),
    "failed" (rc 1 AND the summary line reports >=1 failed: a genuine,
    discriminating failure), or "error_runner" (anything else: rc 2/3/4,
    "no tests ran", "not found"/"no match in any of", or no recognizable
    summary line at all - a RUNNER problem, never a verdict about the test,
    and never read as either a pass or a fail)."""
    out = out or ""
    if rc == 0:
        return "passed"
    if rc == 1 and PYTEST_FAILED_SUMMARY_RE.search(out):
        return "failed"
    return "error_runner"


def pytest_timeout_budget(node_ids):
    """Round 11: `max(180, 10 * len(node_ids))` seconds - deterministic,
    scales with the selection, no per-file markers or overrides. Fixed at
    180s regardless of selection size, `run_pytest` timed out on this repo's
    OWN 48-test self-suite when a merge_gate run over a large, 14-branch
    range selected nearly all of them at once as "new/changed" (a genuine
    resource limit, not a logic bug - see round 10's report) - a selection
    of N ids realistically needs on the order of N * (a worst-case per-test
    second), not a constant."""
    return max(180, 10 * len(node_ids))


def run_pytest(root, node_ids, python, timeout=None):
    """Run exactly `node_ids` with `-k "not host"` (host tests need real
    darktable and never run in this check) under `python` (from
    `gate_python`, NEVER a bare "python3" - see `gate_python`'s docstring).
    `timeout` defaults to `pytest_timeout_budget(node_ids)` (round 11) - a
    caller only overrides it in a test that deliberately wants a SHORT
    timeout to exercise the timeout path itself. Returns (rc, out); rc 5
    means "every selected test was filtered out by -k", distinct from a real
    pass or a real failure - callers must not read it as either."""
    if not node_ids:
        return 0, "no tests selected"
    if timeout is None:
        timeout = pytest_timeout_budget(node_ids)
    argv = [python, "-m", "pytest", *node_ids, "-k", "not host", "-q"]
    return sh(argv, cwd=root, timeout=timeout)


# pytest's own signature for "this test module could not even be collected"
# (an ImportError/SyntaxError at import time) - distinct from a genuine
# assertion failure, which never prints either string. Matched on OUTPUT, not
# rc: a collection error and a real failure can both exit non-zero (fix
# round 1).
COLLECTION_ERROR_RE = re.compile(r"ERROR collecting|ImportError while importing test module|"
                                  r"errors? during collection")


def is_collection_error(out):
    return bool(COLLECTION_ERROR_RE.search(out or ""))


# Fix round 2 (row 459 audit, capsule-corp's capsule/intent-examples
# f3031c3..0e184f7, mirrored here): when a production file is NEW in the
# range, reverting it to base means deleting it - every test that imports it
# then ERRORs no matter how well it discriminates, which is not a
# test-quality signal. Distinguish that specific shape from a genuine,
# unrelated import error.
NO_MODULE_RE = re.compile(r"No module named '([\w.]+)'")
CANNOT_IMPORT_RE = re.compile(r"cannot import name '(\w+)' from '([\w.]+)'")

# Fix round 5: an explicit, auditable opt-out for a test file whose subject
# predates the range entirely (new tests for old code - fix round 4's
# no-production-change heuristic already catches the common "some unrelated
# file changed too" shape via imports, but a human author can assert this
# directly and cite WHY, which the table then shows a reviewer). Bound to a
# specific base sha so it cannot silently outlive the range it was written
# for.
#
# Round 13 (mirrored from capsule-corp): a marker naming no `::` ids used to
# switch off revert-mutation for EVERY test in the file, including brand-new
# tests whose subject genuinely changed in this range - now the marker must
# name the specific `Class.test_name` (or bare `test_name`) ids it covers,
# comma-separated after a literal `::`. `ids_group` is optional in the regex
# only so that a marker with no `::` at all still PARSES (as `ids=None`)
# instead of simply failing to match - `_check_range_impl` is what turns
# that shape into the `marker_stale` gate failure; this function only reads
# what is on disk.
MARKER_RE = re.compile(
    r"^#\s*mutation-check:\s*targets-predate-range\s+([0-9a-fA-F]{7,40})\s+--\s*(.+?)"
    r"(?:\s*::\s*([\w.]+(?:\s*,\s*[\w.]+)*))?\s*$",
    re.MULTILINE)


def extract_marker(source, top_lines=20):
    """(marker_sha, reason, ids) from a top-of-file `# mutation-check:
    targets-predate-range <sha> -- <reason> :: Class.test_a, Class.test_b`
    comment, or None. Only the first `top_lines` are scanned - "top-of-file",
    not "anywhere in the file" (a marker buried mid-file would be too easy to
    miss in review). `ids` is a tuple of the comma-separated ids exactly as
    written (whitespace-stripped), or None when the marker carries no `::`
    ids section at all (round 13: that shape is a `marker_stale` gate failure
    downstream, not silently accepted as file-level)."""
    if not source:
        return None
    head_block = "\n".join(source.splitlines()[:top_lines])
    m = MARKER_RE.search(head_block)
    if not m:
        return None
    sha, reason, ids_str = m.group(1), m.group(2), m.group(3)
    ids = tuple(i.strip() for i in ids_str.split(",")) if ids_str else None
    return (sha, reason, ids)


def resolve_sha(repo, ref):
    rc, out = sh(["git", "rev-parse", f"{ref}^{{commit}}"], cwd=repo)
    return out.strip() if rc == 0 else None


def _prod_module_candidates(path):
    """(dotted, attr, package) for a production .py file path - `dotted` is
    the fully-qualified module name; `attr`/`package` is the "from package
    import attr" shape a `from pkg import newmodule` statement produces when
    newmodule.py is missing (Python reports the parent package name, not the
    missing module, in that phrasing)."""
    p = str(path)
    p = p[: -len("/__init__.py")] if p.endswith("/__init__.py") else p[:-3]
    dotted = p.replace("/", ".")
    parts = dotted.split(".")
    attr = parts[-1]
    package = ".".join(parts[:-1])
    return dotted, attr, package


def new_module_import_error(out, new_prod_files):
    """True when the collection error in `out` is explained by one of
    `new_prod_files` (production files added in this range, so absent after a
    revert-to-base) simply not existing - as opposed to a genuinely broken or
    unrelated import."""
    out = out or ""
    m1 = NO_MODULE_RE.search(out)
    m2 = CANNOT_IMPORT_RE.search(out)
    for f in new_prod_files:
        dotted, attr, package = _prod_module_candidates(f)
        if m1 and (m1.group(1) == dotted or dotted.startswith(m1.group(1) + ".")
                   or m1.group(1).startswith(dotted + ".")
                   # Fix round 4 (window-5 gate run, gate-w5.txt): a test file
                   # that hacks sys.path to import a submodule by its bare
                   # stem gets "No module named '<stem>'", not the dotted
                   # path - match the bare stem too.
                   or m1.group(1) == attr):
            return True
        if m2 and m2.group(1) == attr and m2.group(2) in (package, dotted.rsplit(".", 1)[0] if "." in dotted else ""):
            return True
    return False


# B1 (round 10, HTC gate-rows audit): `cannot import name 'X' from 'M'` (or
# the AttributeError-shaped equivalent) is not the same failure as `M` not
# existing at all (new_module_import_error, above) - `M` DOES exist at base,
# it just does not define `X` yet, because the range ADDED `X` to `M`. That
# is not a test-quality signal either.
ATTRIBUTE_ERROR_RE = re.compile(r"AttributeError: module '([\w.]+)' has no attribute '(\w+)'")


def _module_path_for_dotted(dotted, candidate_files):
    for f in candidate_files:
        d, _, _ = _prod_module_candidates(f)
        if d == dotted:
            return f
    return None


def _top_level_names(source):
    """Set of names bound at module top level: function/class defs and
    simple `NAME = ...` / `NAME: T = ...` assignments. Enough to recognize
    "the range added a new class/function/constant to this module"."""
    names = set()
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return names
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def analyze_symbol_import_error(out, repo, base, head, reverted_prod_files):
    """None (not this shape - stays a plain "error"), or
    `(state, module_path, symbol, additions_only)` for the B1 shape: the
    collection error names a symbol `X` missing from module `M`, `M` is one
    of `reverted_prod_files` (so IT exists at base, just not with `X` yet),
    and `X` is a name HEAD's `M` defines but base's `M` does not - i.e. the
    range itself added `X` to `M`. `additions_only` is True when every one of
    base's top-level names is still present in HEAD's `M` (a pure addition,
    safe to restore in full) - the caller reruns with `M` kept at HEAD in
    that case; otherwise the caller reads it as an unresolvable n/a."""
    out = out or ""
    m = CANNOT_IMPORT_RE.search(out) or ATTRIBUTE_ERROR_RE.search(out)
    if not m:
        return None
    if m.re is ATTRIBUTE_ERROR_RE:
        module_dotted, symbol = m.group(1), m.group(2)
    else:
        symbol, module_dotted = m.group(1), m.group(2)
    module_path = _module_path_for_dotted(module_dotted, reverted_prod_files)
    if module_path is None:
        return None
    base_src = show_file(repo, base, module_path) or ""
    head_src = show_file(repo, head, module_path) or ""
    base_names = _top_level_names(base_src)
    head_names = _top_level_names(head_src)
    if symbol not in head_names or symbol in base_names:
        return None  # not something the range added to this module
    additions_only = base_names <= head_names
    return module_path, symbol, additions_only


def _import_names(source):
    """Set of dotted MODULE paths a test file's `import`/`from ... import`
    statements genuinely execute - fix round 7: no bare last-component
    addition and no "the whole base package is a candidate for any of its
    submodules" - `import a.b.c` really does execute `a`, `a.b` and `a.b.c`
    (Python imports every ancestor package), so all three are real
    candidates; but `from pkg import x, y` must resolve to `pkg.x` and
    `pkg.y` specifically (the two submodules x/y might be), NEVER to bare
    `pkg` standing in for some OTHER, unrelated submodule under the same
    package. `pkg` itself IS a legitimate candidate here (pkg/__init__.py
    genuinely executes to make the from-import work), so it is one of the
    dotted PREFIXES added for the base - it is just never added as a stand-in
    for `pkg.x`/`pkg.y` themselves. A bare relative import (`from . import
    x`, no module name) is skipped - too ambiguous to resolve without
    walking the package tree, and skipping it only means this file falls
    through to running the revert check as before (never a false n/a).

    Returns `(dotted, bare)`: `dotted` is matched by exact dotted-path
    equality (round 7's precision fix); `bare` is a SEPARATE set of
    genuinely unqualified, dot-free `import x` names (the `sys.path` hack
    case: a test that inserts a tools dir onto `sys.path` and does a plain
    `import toolkit` carries no package-qualifying information at all, so
    that one case is still matched against a production file's bare last
    component - it is not the "shared package" ambiguity round 7 fixes,
    since there is no package name here to be ambiguous about)."""
    dotted = set()
    bare = set()
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return dotted, bare

    def add_prefixes(name):
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            dotted.add(".".join(parts[:i]))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add_prefixes(alias.name)
                if "." not in alias.name:
                    bare.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and not node.module:
                continue
            base = node.module or ""
            if base:
                add_prefixes(base)
            for alias in node.names:
                if base:
                    dotted.add(f"{base}.{alias.name}")
                else:
                    bare.add(alias.name)
    return dotted, bare


def expand_via_init_reexports(repo, head, dotted_names):
    """Fix round 7, the `__init__` case: `from pkg import name` where
    `pkg/__init__.py` itself does `from pkg.sub import name` (or `from .sub
    import name`) re-exports `name` from `sub`, not from a same-named
    `pkg/name.py` that may not even exist - add `pkg.sub` (and `pkg.sub.name`)
    as additional candidates for every "pkg.name"-shaped entry in
    `dotted_names` whose `pkg/__init__.py` does this. Only ever ADDS
    candidates - a package with no such re-export, or no `__init__.py` at
    all, is untouched. Takes and returns just the `dotted` half of
    `_import_names`'s result - the `bare` half needs no expansion, since it
    already carries no package to resolve through an `__init__.py`."""
    expanded = set(dotted_names)
    for cand in list(dotted_names):
        pkg, sep, attr = cand.rpartition(".")
        if not sep:
            continue
        init_path = pkg.replace(".", "/") + "/__init__.py"
        src = show_file(repo, head, init_path)
        if not src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                exported = alias.asname or alias.name
                if exported != attr:
                    continue
                if node.level:
                    sub_base = node.module or ""
                    resolved = pkg if not sub_base else f"{pkg}.{sub_base}"
                else:
                    resolved = node.module or ""
                if resolved:
                    expanded.add(resolved)
                    expanded.add(f"{resolved}.{alias.name}")
    return expanded


def references_prod_file(test_import_names, prod_file):
    """True when `test_import_names` (a `(dotted, bare)` pair - `dotted` from
    `_import_names`, optionally widened by `expand_via_init_reexports`;
    `bare` from `_import_names` unchanged) references `prod_file` - fix
    round 7: exact dotted-path membership for anything package-qualified
    (never a bare shared top-level package: a test doing `from pkg import a`
    must not look like it also references an unrelated `pkg/b.py` merely
    because both live under `pkg` - only `pkg` itself, i.e. `pkg/__init__.py`,
    and `pkg.a` are real candidates for that import), plus a bare
    last-component match ONLY for genuinely unqualified `import x` names
    (the `sys.path`-hack case, which carries no package to be ambiguous
    about in the first place)."""
    dotted_names, bare_names = test_import_names
    dotted, attr, package = _prod_module_candidates(prod_file)
    return dotted in dotted_names or attr in bare_names


def any_assert_in_ranges(text, func_ranges):
    """True when at least one of `func_ranges` (the new/changed test
    functions) contains the word `assert` anywhere in its body - i.e. the
    static-flags "no assert" case, computed the same way, but as a boolean
    gate for round 6's file-level fallback rather than a report line."""
    lines = text.splitlines(keepends=True)
    for start, end in func_ranges:
        body = "".join(lines[start - 1:min(end, len(lines))])
        if "assert" in body:
            return True
    return False


def apply_weakening(text, func_ranges):
    """Flip the FIRST discriminating assertion INSIDE one of `func_ranges`.
    Returns (new_text, what) or (None, None)."""
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for l in lines:
        offsets.append(offsets[-1] + len(l))
    for start, end in sorted(func_ranges):
        body_start = offsets[start - 1]
        body_end = offsets[min(end, len(lines))]
        body = text[body_start:body_end]
        for pattern, repl, what in (
            (ASSERT_EQ_RE, "assertNotEqual(", "assertEqual -> assertNotEqual"),
            (ASSERT_TRUE_RE, "assertFalse(", "assertTrue -> assertFalse"),
        ):
            m = pattern.search(body)
            if m:
                new_body = body[:m.start()] + repl + body[m.end():]
                return text[:body_start] + new_body + text[body_end:], what
        m = BARE_ASSERT_EQ_RE.search(body)
        if m:
            line = m.group(0)
            flipped = line.replace(" == ", " != ", 1)
            new_body = body[:m.start()] + flipped + body[m.end():]
            return text[:body_start] + new_body + text[body_end:], "assert a == b -> assert a != b"
    return None, None


def _assertion_occurrences(body):
    """[(start, end, replacement_text, what), ...], in source order, for
    every recognized assertion pattern occurrence in `body` - B5: lets a
    caller try the 1st, 2nd, ... occurrence in turn, not just the first."""
    occ = []
    for m in ASSERT_EQ_RE.finditer(body):
        occ.append((m.start(), m.end(), "assertNotEqual(", "assertEqual -> assertNotEqual"))
    for m in ASSERT_TRUE_RE.finditer(body):
        occ.append((m.start(), m.end(), "assertFalse(", "assertTrue -> assertFalse"))
    for m in BARE_ASSERT_EQ_RE.finditer(body):
        occ.append((m.start(), m.end(), m.group(0).replace(" == ", " != ", 1), "assert a == b -> assert a != b"))
    occ.sort(key=lambda o: o[0])
    return occ


def apply_weakening_at(text, region_start_line, region_end_line, index):
    """B5: flip the `index`-th (0-based, source order) recognized assertion
    occurrence within the line range [region_start_line, region_end_line] of
    `text` (1-based, inclusive - same convention as `func_ranges` elsewhere).
    Returns (new_text, what) or (None, None) if there is no such occurrence -
    the caller tries index 0, 1, 2, ... until this returns None, meaning
    every assertion in that range has been tried."""
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for l in lines:
        offsets.append(offsets[-1] + len(l))
    body_start = offsets[region_start_line - 1]
    body_end = offsets[min(region_end_line, len(lines))]
    body = text[body_start:body_end]
    occ = _assertion_occurrences(body)
    if index >= len(occ):
        return None, None
    start, end, new_span, what = occ[index]
    new_body = body[:start] + new_span + body[end:]
    return text[:body_start] + new_body + text[body_end:], what


HELPER_CALL_RE = re.compile(r"\bself\.(\w+)\(|(?<![.\w])([a-zA-Z_]\w*)\(")


def _all_top_level_functions(source):
    """{(class_or_None, name): (lineno, end_lineno)} for EVERY function or
    method defined at module level or directly in a class body - any name,
    not just test* (B4: used to resolve a same-file helper a test body
    calls, one level deep)."""
    out = {}
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return out

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.cls = None
            self.func_depth = 0

        def visit_ClassDef(self, node):
            prev, self.cls = self.cls, node.name
            self.generic_visit(node)
            self.cls = prev

        def _fn(self, node):
            if self.func_depth == 0:
                out[(self.cls, node.name)] = (node.lineno, getattr(node, "end_lineno", node.lineno))
            self.func_depth += 1
            self.generic_visit(node)
            self.func_depth -= 1

        visit_FunctionDef = _fn
        visit_AsyncFunctionDef = _fn

    Visitor().visit(tree)
    return out


def _find_helper_with_assert(source, body, own_class, all_funcs):
    """B4/B5: a test body with no `assert` of its own still counts as
    asserting when it calls a helper - `self.<name>(...)` (a method on the
    SAME class, or its base class within this file, when the base's own
    name ends in `Fixture`/`Base` - a common test-base-class convention) or
    a bare `<name>(...)` (a module-level function in this file) - whose OWN
    body contains `assert`. Returns `(name, lineno, end_lineno)` for the
    first such helper found, or None (a genuinely assert-free chain still
    flags)."""
    for m in HELPER_CALL_RE.finditer(body):
        name = m.group(1) or m.group(2)
        if not name or name.startswith("assert"):
            continue
        candidates = [(own_class, name), (None, name)]
        for cls, fname in all_funcs:
            if fname == name and cls and cls != own_class and (cls.endswith("Fixture") or cls.endswith("Base")):
                candidates.append((cls, name))
        for key in candidates:
            if key in all_funcs:
                s2, e2 = all_funcs[key]
                lines = source.splitlines()
                helper_body = "\n".join(lines[s2 - 1:e2])
                if "assert" in helper_body:
                    return name, s2, e2
    return None


def _asserts_via_helper(source, body, own_class, all_funcs):
    """B4 wrapper: just the helper name, or None."""
    hit = _find_helper_with_assert(source, body, own_class, all_funcs)
    return hit[0] if hit else None


def static_flags(source, lineno, end_lineno, own_class=None, all_funcs=None):
    lines = source.splitlines()
    body = "\n".join(lines[lineno - 1:end_lineno])
    flags = []
    if "assert" not in body:
        helper = _asserts_via_helper(source, body, own_class, all_funcs or {})
        if helper:
            flags.append(f"info: asserts via helper {helper}")
        else:
            flags.append("no assert in the function body")
    if TAUTOLOGY_RE.search(body):
        flags.append("assertTrue(True) / assert True - a tautology")
    if BARE_EXCEPT_RE.search(body):
        flags.append("bare `except: pass` could swallow the subject's own error")
    deco_window = "\n".join(lines[max(0, lineno - 6):lineno - 1])
    m = SKIP_MARK_RE.search(deco_window)
    if m:
        flags.append(f"@pytest.mark.{m.group(1)} on a new/changed test")
    return flags


def _cell(state, why=None):
    """`state` is None (n/a), True (failed - good), False (did not fail - bad),
    "error" (collection error - not discriminating, fix round 1),
    "new_module" (the reverted file doesn't exist yet - fix round 2),
    "no_prod_change" (none of the production files changed in this range are
    imported by this test file - fix round 4), "targets_predate_range" (an
    explicit, sha-bound, per-test marker says so - fix round 5/13, `why` is
    the marker's reason), "marker_mismatch" (a marker is present but its sha
    does not match this gate's --base - fix round 5, never silently
    ignored), or "marker_stale" (fix round 13: the marker has no `::` ids at
    all - the old file-level shape - or names an id that is not in this
    range's new/changed set for that file; a gate failure, exactly like
    marker_mismatch),
    "fallback_ok"/"fallback_bad" (the weaken column, round 6: the changed
    function(s) had no assertion at all, so the mutation ran against the
    file's first assertion anywhere instead - always labelled, pass or fail,
    so a reviewer sees it ran against code the range didn't touch), or
    "no_functions" (round 8: the changed test FILE has zero new/changed test
    FUNCTIONS - a decorator-only or fixture-signature edit - so there was
    nothing to select and run at all), "error_runner" (round 10 B0: rc 2/3/4,
    no tests ran, not found/no match, or no recognizable pytest summary line
    at all - a RUNNER problem, BAD in both columns, never a pass or a fail),
    "reverted_symbol_napt"/"symbol_kept_ok"/"symbol_kept_bad" (round 10 B1:
    the collection error is a name the range itself added to a reverted
    module - see `analyze_symbol_import_error`), "helper_ok"/"helper_bad"
    (round 12 B5: the changed function asserts only via a same-file helper -
    the mutation tried each of the HELPER's own assertions in turn, same
    idea as the file-level fallback but scoped to the helper actually being
    exercised, so it is tried FIRST). Every state but plain
    True/False, fallback_bad, marker_mismatch, marker_stale, error_runner and
    symbol_kept_bad is n/a, not a fail, and not counted against the gate -
    the weaken column carries the requirement instead."""
    if state is None:
        return "n/a"
    if state == "new_module":
        return "n/a (new module)"
    if state == "no_prod_change":
        return "n/a (no production change)"
    if state == "no_functions":
        return "n/a (no test functions changed)"
    if state == "targets_predate_range":
        return f"n/a (targets predate range: {why})"
    if state == "marker_mismatch":
        return f"marker mismatch (BAD): {why}" if why else "marker mismatch (BAD)"
    if state == "marker_stale":
        return f"marker stale (BAD): {why}" if why else "marker stale (BAD)"
    if state == "fallback_ok":
        return f"FAIL (good) [{why}]" if why else "FAIL (good) [weaken: file-level fallback]"
    if state == "fallback_bad":
        return f"did not fail (BAD) [{why}]" if why else "did not fail (BAD) [weaken: file-level fallback]"
    if state == "helper_ok":
        return f"FAIL (good) [{why}]"
    if state == "helper_bad":
        return f"did not fail (BAD) [{why}]"
    if state == "error":
        return "ERROR (not discriminating)"
    if state == "error_runner":
        return f"error (runner): {why}" if why else "error (runner)"
    if state == "reverted_symbol_napt":
        return f"n/a (reverted symbol {why} imported at collection)"
    if state == "symbol_kept_ok":
        return f"FAIL (good) [revert: {why}]"
    if state == "symbol_kept_bad":
        return f"did not fail (BAD) [revert: {why}]"
    return "FAIL (good)" if state else "did not fail (BAD)"


# Round 13 (mirrored from capsule-corp): the sentinel key
# `revert_by_file[f]` uses for a state that applies to the whole test FILE
# rather than to any specific test id - the only case left where that
# happens is "no_functions" (the file has zero new/changed test functions at
# all, so there is no id to key a per-test state under). Every other state
# below is always assigned per test id, even when every id in the file
# happens to share the same one (`render_revert_cell` collapses that
# identical-everywhere case back to the old, un-prefixed cell text, so a
# file with no marker and one uniform outcome renders exactly as every
# pre-round-13 fixture already expects).
WHOLE_FILE_KEY = "*file*"


def _ids_for(funcs):
    """Fix round 13: the same `{cls}.{name}`-or-bare-`name` id shape used
    everywhere else in this file (for the table's "names" column, NOT the
    pytest node id `node_id` builds to actually run something), as a set,
    for whatever `funcs` list (possibly already filtered down to the ids a
    marker did NOT cover) is in scope."""
    return {f"{cls}.{name}" if cls else name for cls, name, _, _ in funcs}


def _assign_revert_state(revert_by_file, f, funcs, state, why):
    """Round 13: record `(state, why)` for every id in `funcs` under
    `revert_by_file[f]` - one dict entry per test id, so a file can carry
    DIFFERENT revert-mutation outcomes for different tests (some n/a via a
    per-test marker, others genuinely revert-checked). Falls back to
    `WHOLE_FILE_KEY` only when `funcs` has no ids at all (the round-8
    no-functions-changed shape)."""
    ids_here = sorted(_ids_for(funcs))
    bucket = revert_by_file.setdefault(f, {})
    if not ids_here:
        bucket[WHOLE_FILE_KEY] = (state, why)
        return
    for tid in ids_here:
        bucket[tid] = (state, why)


def render_revert_cell(entries, ids):
    """Round 13: render the (b) column for one test file from its per-test
    `entries` dict (`{test_id: (state, why)}`, plus possibly the
    `WHOLE_FILE_KEY` sentinel) and `ids` (every new/changed test id in the
    file, sorted). When every id shares the identical `(state, why)` - true
    of every file this gate ever produced before round 13, since a whole file
    always got one uniform outcome - this renders EXACTLY the old, bare
    `_cell(...)` text (no id prefix), so every pre-round-13 fixture keeps
    passing unchanged. Only a file with a per-test marker that covers some
    but not all of its new/changed ids (the new round-13 shape) ever produces
    a mixed cell, one `id: cell` segment per id, in id order."""
    if not entries:
        return _cell(None, "-")
    if WHOLE_FILE_KEY in entries:
        state, why = entries[WHOLE_FILE_KEY]
        return _cell(state, why)
    values = {entries[tid] for tid in ids if tid in entries}
    if len(values) == 1 and all(tid in entries for tid in ids):
        state, why = next(iter(values))
        return _cell(state, why)
    parts = []
    for tid in ids:
        if tid in entries:
            state, why = entries[tid]
            parts.append(f"{tid}: {_cell(state, why)}")
    return "; ".join(parts) if parts else _cell(None, "-")


def check_range(repo, base, head):
    """Round 12 (worktree hygiene): every scratch worktree this gate ever
    creates - (collect), (b) revert, (c) weaken - is already removed in its
    own `finally`, but a hard process kill (SIGKILL) or a bare git failure
    inside `remove_worktree` (which swallows its own exit code - see `sh`)
    can still leave a stale `.git/worktrees/*` entry behind (346 of 388
    scratch entries were found prunable in a single scouter checkout, the
    HTC audit's own evidence). `git worktree prune` is therefore run
    UNCONDITIONALLY when this function returns, raises, or is interrupted
    (Ctrl-C/KeyboardInterrupt) - it is a pure cleanup pass over metadata for
    worktrees whose directory is already gone, never a mutation of a live
    one, so running it even when nothing leaked is always safe."""
    try:
        return _check_range_impl(repo, base, head)
    finally:
        sh(["git", "worktree", "prune"], cwd=Path(repo).resolve())


def _check_range_impl(repo, base, head):
    repo = Path(repo).resolve()
    python = gate_python(repo)
    changed = changed_files(repo, base, head)
    test_files = [f for f in changed if is_test_file(f)]
    prod_files = [f for f in changed if f.endswith(".py") and not is_test_file(f) and not is_test_support_file(f)]

    rows = []
    ok = True

    if not test_files:
        return True, [], "no new/changed test files in this range"

    per_file_funcs = {}
    pending_generated = {}  # f -> a representative (lineno, end_lineno) to resolve (B3)
    for f in test_files:
        status = file_status(repo, base, head, f)
        added = added_line_numbers(repo, base, head, f)
        funcs = changed_test_functions(repo, base, head, f, status, added)
        per_file_funcs[f] = (status, funcs)
        head_src = show_file(repo, head, f)
        nested = nested_test_like_defs(head_src) if head_src is not None else {}
        # No lexical link is assumed between a nested closure and the class
        # it ends up attached to (a `setattr(SomeClass, name, factory(...))`
        # pattern, the audit's real shape, has NONE - the factory is usually
        # a plain MODULE-level function) - `cls` here is whatever lexically
        # encloses the closure, almost always None, and is not used as a
        # filter for that reason.
        for (_n_cls, _n_name), (s, e) in nested.items():
            if status == "A" or any(s <= ln <= e for ln in added):
                pending_generated[f] = (s, e)
                break

    # B3 (HTC gate-rows audit): a changed test-shaped closure NESTED inside a
    # factory (e.g. `_make()`, one call per JSON example, `setattr`-copied
    # onto some class at module load time) is not itself a collected item -
    # resolve what pytest REALLY collects for the whole file and union in
    # every real item not already known from the AST-only view, using the
    # changed closure's own line range (where the shared assertion logic
    # actually lives) as their location for the weaken step below.
    if pending_generated:
        with tempfile.TemporaryDirectory(prefix="mutation-check-collect-") as tmp:
            cwt = make_worktree(repo, head, Path(tmp))
            try:
                for f, (s, e) in pending_generated.items():
                    real = collected_item_names(cwt, f, python)
                    status, funcs = per_file_funcs[f]
                    existing = {(cls, name) for cls, name, _, _ in funcs}
                    added_funcs = list(funcs)
                    for r_cls, r_names in real.items():
                        for r_name in r_names:
                            if (r_cls, r_name) not in existing:
                                added_funcs.append((r_cls, r_name, s, e))
                                existing.add((r_cls, r_name))
                    per_file_funcs[f] = (status, added_funcs)
            finally:
                remove_worktree(repo, cwt)

    # ---- (d) static flags, read straight from HEAD ----
    static_by_file = {}
    for f, (status, funcs) in per_file_funcs.items():
        src = show_file(repo, head, f)
        all_funcs = _all_top_level_functions(src)
        flags = []
        hard_flag = False
        for cls, name, s, e in funcs:
            for flag in static_flags(src, s, e, own_class=cls, all_funcs=all_funcs):
                flags.append(f"{cls + '.' if cls else ''}{name}: {flag}")
                if not flag.startswith("info:"):
                    hard_flag = True
        static_by_file[f] = flags
        if hard_flag:
            ok = False

    # ---- (b) revert-mutation, one worktree, production reverted to base ----
    revert_by_file = {}

    # Fix round 5 (per-test as of round 13): an explicit, sha-bound marker
    # overrides revert-mutation - checked first, and regardless of whether
    # any production file changed at all, so a wrong-sha or stale marker is
    # caught (never silently ignored) even on a range with no production
    # changes. Round 13: a marker with no `::` ids used to switch off
    # revert-mutation for the WHOLE file, including brand-new tests whose
    # subject genuinely changed - it must now name the specific ids it
    # covers; every OTHER new/changed id in the same file still falls
    # through to the ordinary revert-mutation check below. A marker with no
    # ids at all, or one naming an id that is not in this range's
    # new/changed set for that file, is `marker_stale` - a gate failure,
    # exactly like a sha mismatch.
    resolved_base = resolve_sha(repo, base)
    for f, (status, funcs) in per_file_funcs.items():
        marker = extract_marker(show_file(repo, head, f))
        if marker is None:
            continue
        marker_sha, reason, marker_ids = marker
        file_ids = _ids_for(funcs)
        resolved_marker = resolve_sha(repo, marker_sha)
        if not (resolved_marker and resolved_base and resolved_marker == resolved_base):
            why = f"marker names {marker_sha}, gate --base is {base} ({reason})"
            _assign_revert_state(revert_by_file, f, funcs, "marker_mismatch", why)
            ok = False
            continue
        if not marker_ids:
            why = (f"marker has no `:: <ids>` list ({reason}) - a file-level marker with "
                   "no ids is stale as of round 13")
            _assign_revert_state(revert_by_file, f, funcs, "marker_stale", why)
            ok = False
            continue
        unknown = [i for i in marker_ids if i not in file_ids]
        if unknown:
            why = (f"marker names test id(s) not in this range's new/changed set for "
                   f"{f}: {', '.join(unknown)} ({reason})")
            _assign_revert_state(revert_by_file, f, funcs, "marker_stale", why)
            ok = False
            continue
        covered = [(cls, name, s, e) for cls, name, s, e in funcs
                   if (f"{cls}.{name}" if cls else name) in set(marker_ids)]
        _assign_revert_state(revert_by_file, f, covered, "targets_predate_range", reason)

    # Every id NOT already given a state above (no marker at all, or a marker
    # that covers only some of this file's new/changed ids) still needs the
    # ordinary revert-mutation check - `remaining` carries just those ids'
    # `funcs`, file by file; a file every one of whose ids the marker loop
    # above already handled (fully covered, or failed outright as
    # marker_mismatch/marker_stale) is dropped entirely, exactly as before.
    remaining = {}
    for f, (status, funcs) in per_file_funcs.items():
        handled = set(revert_by_file.get(f, {}).keys())
        uncovered_ids = _ids_for(funcs) - handled
        if f in revert_by_file and not uncovered_ids:
            continue
        if handled:
            funcs = [(cls, name, s, e) for cls, name, s, e in funcs
                     if (f"{cls}.{name}" if cls else name) in uncovered_ids]
        remaining[f] = (status, funcs)

    if prod_files and remaining:
        new_prod_files = [f for f in prod_files if file_status(repo, base, head, f) == "A"]
        with tempfile.TemporaryDirectory(prefix="mutation-check-revert-") as tmp:
            wt = make_worktree(repo, head, Path(tmp))
            try:
                for f in prod_files:
                    status = file_status(repo, base, head, f)
                    revert_to_base(repo, base, wt, f, status)
                for f, (status, funcs) in remaining.items():
                    # Fix round 8 (integrator report, gate-w5-style): a
                    # changed test FILE with zero new/changed test FUNCTIONS
                    # (a decorator-only or fixture-signature edit) has no
                    # test ids to select at all. Without this guard,
                    # `ids = []` -> `run_pytest([])`'s early return `(0, "no
                    # tests selected")` reads `failed = rc != 0` -> False ->
                    # "did not fail (BAD)", exactly as if a real revert had
                    # failed to break a real test - a false alarm, checked
                    # BEFORE the no-production-change import check since
                    # there is nothing to import-match against either.
                    if not funcs:
                        _assign_revert_state(revert_by_file, f, funcs, "no_functions",
                                             "no new/changed test function in this file "
                                             "(decorator-only or fixture-signature edit)")
                        continue
                    # Fix round 4: this test file may not even be ABOUT any of
                    # the production files this range changed (e.g. new tests
                    # added for pre-existing code) - reverting production it
                    # never touches can never discriminate it, and reading
                    # that as "did not fail (BAD)" would be a false alarm.
                    dotted_names, bare_names = _import_names(show_file(repo, head, f))
                    dotted_names = expand_via_init_reexports(repo, head, dotted_names)
                    test_imports = (dotted_names, bare_names)
                    if not any(references_prod_file(test_imports, pf) for pf in prod_files):
                        _assign_revert_state(revert_by_file, f, funcs, "no_prod_change",
                                             "no production file changed in this range is imported by this test file")
                        continue
                    ids = sorted({node_id(f, cls, name) for cls, name, _, _ in funcs})
                    rc, out = run_pytest(wt, ids, python)
                    if is_collection_error(out):
                        symbol_hit = analyze_symbol_import_error(out, repo, base, head, prod_files)
                        if symbol_hit is not None:
                            module_path, symbol, additions_only = symbol_hit
                            if not additions_only:
                                # B1 fallback: M's diff is not a pure
                                # addition (removed/changed existing names
                                # too) - restoring it whole is not safe to
                                # call a clean revert. n/a; the weaken column
                                # carries the requirement, as with new_module.
                                _assign_revert_state(revert_by_file, f, funcs, "reverted_symbol_napt", symbol)
                                continue
                            # B1 (row 459 audit, HTC gate-rows): `M` exists at
                            # base, it just doesn't define `X` yet - the range
                            # ADDED `X` to `M`. Keep M at HEAD (restoring only
                            # what the range purely added) and rerun once -
                            # this measures the test's OWN discrimination
                            # without also blaming it for a collection error
                            # that isn't its fault.
                            head_module_src = show_file(repo, head, module_path)
                            (wt / module_path).write_text(head_module_src, encoding="utf-8")
                            try:
                                rc2, out2 = run_pytest(wt, ids, python)
                            finally:
                                revert_to_base(repo, base, wt, module_path,
                                               file_status(repo, base, head, module_path))
                            outcome2 = classify_pytest_outcome(rc2, out2)
                            label = f"{module_path} kept at head for import of {symbol}"
                            if outcome2 == "error_runner":
                                _assign_revert_state(revert_by_file, f, funcs, "error_runner", out2[-400:])
                                ok = False
                            elif outcome2 == "failed":
                                _assign_revert_state(revert_by_file, f, funcs, "symbol_kept_ok", label)
                            else:
                                _assign_revert_state(revert_by_file, f, funcs, "symbol_kept_bad", label)
                                ok = False
                            continue
                        if new_module_import_error(out, new_prod_files):
                            # n/a, not a fail: the module the test imports
                            # simply doesn't exist yet at base - the weaken
                            # column (below) carries the requirement.
                            _assign_revert_state(revert_by_file, f, funcs, "new_module", out[-800:])
                        else:
                            _assign_revert_state(revert_by_file, f, funcs, "error", out[-800:])
                            ok = False
                        continue
                    if rc == NO_TESTS_COLLECTED_RC:
                        _assign_revert_state(revert_by_file, f, funcs, None,
                                             "n/a - only host-only tests changed (filtered by -k 'not host')")
                        continue
                    outcome = classify_pytest_outcome(rc, out)
                    if outcome == "error_runner":
                        _assign_revert_state(revert_by_file, f, funcs, "error_runner", out[-400:])
                        ok = False
                    else:
                        failed = outcome == "failed"
                        _assign_revert_state(revert_by_file, f, funcs, failed, out[-800:])
                        if not failed:
                            ok = False
            finally:
                remove_worktree(repo, wt)
    else:
        for f, (status, funcs) in remaining.items():
            _assign_revert_state(revert_by_file, f, funcs, None,
                                 "no production .py file changed in this range - (b) skipped")

    # ---- (c) assertion-weakening, per file, same HEAD worktree reused ----
    weaken_by_func = {}
    with tempfile.TemporaryDirectory(prefix="mutation-check-weaken-") as tmp:
        wt = make_worktree(repo, head, Path(tmp))
        try:
            for f, (status, funcs) in per_file_funcs.items():
                target = wt / f
                if not target.exists() or not funcs:
                    continue
                original = target.read_text(encoding="utf-8")
                func_ranges = [(s, e) for _, _, s, e in funcs]
                mutated, what = apply_weakening(original, func_ranges)

                if mutated is None:
                    # B5 (round 12, HTC integration audit): the changed
                    # function(s) may assert only via a same-file helper (the
                    # exact B4 shape) - try EACH of the helper's own
                    # assertions in turn before ever falling back to
                    # file-level (which would mutate something the range
                    # never even exercises). Only a function with NO helper
                    # at all falls through to the file-level fallback below.
                    all_funcs = _all_top_level_functions(original)
                    lines = original.splitlines()
                    helper_hit = None
                    for cls, name, s, e in funcs:
                        body = "\n".join(lines[s - 1:e])
                        if "assert" in body:
                            continue
                        hit = _find_helper_with_assert(original, body, cls, all_funcs)
                        if hit:
                            helper_hit = hit
                            break
                    if helper_hit is not None:
                        h_name, h_start, h_end = helper_hit
                        ids = sorted({node_id(f, cls, name) for cls, name, _, _ in funcs})
                        idx = 0
                        resolved = None
                        while True:
                            candidate, cand_what = apply_weakening_at(original, h_start, h_end, idx)
                            if candidate is None:
                                break
                            target.write_text(candidate, encoding="utf-8")
                            try:
                                rc, out = run_pytest(wt, ids, python)
                            finally:
                                target.write_text(original, encoding="utf-8")
                            if is_collection_error(out):
                                resolved = ("error", out[-800:])
                                break
                            if rc == NO_TESTS_COLLECTED_RC:
                                resolved = (None, "n/a - only host-only tests changed (filtered by -k 'not host')")
                                break
                            outcome = classify_pytest_outcome(rc, out)
                            if outcome == "error_runner":
                                resolved = ("error_runner", out[-400:])
                                break
                            if outcome == "failed":
                                resolved = ("helper_ok", f"weaken: helper {h_name} assertion {idx + 1} ({cand_what})")
                                break
                            idx += 1
                        if resolved is None:
                            resolved = ("helper_bad",
                                        f"weaken: helper {h_name} - none of its {idx} assertion(s) discriminate")
                        weaken_by_func[f] = resolved
                        if resolved[0] in ("helper_bad", "error", "error_runner"):
                            ok = False
                        continue

                fallback = False
                if mutated is None and not any_assert_in_ranges(original, func_ranges):
                    # Round 6: the changed function(s) contain no assertion at
                    # all (the static flag (d) already reports this
                    # separately) - the ONLY thing left to weaken is
                    # somewhere else in the file. Never silent: `what`
                    # carries "weaken: file-level fallback" into the table
                    # either way, so a reviewer sees this ran against code
                    # the range didn't even touch.
                    all_lines = len(original.splitlines())
                    mutated, what = apply_weakening(original, [(1, all_lines)])
                    if mutated is not None:
                        what = f"weaken: file-level fallback ({what})"
                        fallback = True
                ids = sorted({node_id(f, cls, name) for cls, name, _, _ in funcs})
                if mutated is None:
                    weaken_by_func[f] = (None, "no assertEqual/assertTrue/`==` assert found to flip")
                    continue
                if fallback:
                    # The file-level fallback may have mutated an assertion
                    # OUTSIDE the changed function(s) - run every test in the
                    # file, not just `ids`, or a fallback mutation to an old,
                    # unselected test could never be observed failing.
                    run_ids = sorted({node_id(f, cls, name) for cls, name in test_defs(original)})
                else:
                    run_ids = ids
                target.write_text(mutated, encoding="utf-8")
                try:
                    rc, out = run_pytest(wt, run_ids, python)
                finally:
                    target.write_text(original, encoding="utf-8")
                if is_collection_error(out):
                    weaken_by_func[f] = ("error", out[-800:])
                    ok = False
                    continue
                if rc == NO_TESTS_COLLECTED_RC:
                    weaken_by_func[f] = (None, "n/a - only host-only tests changed (filtered by -k 'not host')")
                    continue
                outcome = classify_pytest_outcome(rc, out)
                if outcome == "error_runner":
                    weaken_by_func[f] = ("error_runner", out[-400:])
                    ok = False
                    continue
                failed = outcome == "failed"
                if fallback:
                    weaken_by_func[f] = ("fallback_ok" if failed else "fallback_bad", what)
                else:
                    weaken_by_func[f] = (failed, what if failed else f"{what}, but the file still passed: {out[-400:]}")
                if not failed:
                    ok = False
        finally:
            remove_worktree(repo, wt)

    for f in test_files:
        status, funcs = per_file_funcs[f]
        ids_sorted = sorted(_ids_for(funcs))
        names = ", ".join(ids_sorted) or "-"
        weak_ok, weak_why = weaken_by_func.get(f, (None, "-"))
        flags = static_by_file.get(f, [])
        rev_cell = render_revert_cell(revert_by_file.get(f, {}), ids_sorted)
        weak_cell = _cell(weak_ok, weak_why)
        flags_cell = "; ".join(flags) if flags else "none"
        rows.append((f, names, rev_cell, weak_cell, flags_cell))

    return ok, rows, None


def render_table(rows):
    header = "| test file | new/changed tests | revert-mutation (b) | assertion-weaken (c) | static flags (d) |"
    sep = "|---|---|---|---|---|"
    lines = [header, sep]
    for f, names, rev_cell, weak_cell, flags_cell in rows:
        lines.append(f"| `{f}` | {names} | {rev_cell} | {weak_cell} | {flags_cell} |")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--repo", default=".")
    a = ap.parse_args(argv)
    # B0 (round 10): refuse outright, before any check, if this gate cannot
    # even confirm pytest is importable under the interpreter it is about to
    # use - never silently produce a table that looks like a real verdict
    # (the HTC integrator's exact failure: every row read "FAIL (good)"
    # because the interpreter had no pytest at all).
    python = gate_python(a.repo)
    env_ok, env_detail = check_pytest_available(python, a.repo)
    if not env_ok:
        print(f"environment error: {env_detail}")
        return 2
    ok, rows, note = check_range(a.repo, a.base, a.head)
    if note:
        print(note)
        return 0
    print(render_table(rows))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
