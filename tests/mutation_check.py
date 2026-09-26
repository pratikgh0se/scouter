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

Usage: tests/mutation_check.py --base <sha> --head <sha> [--repo .]
Exit 0 and a clean markdown table when every changed test file discriminates
and no static flag fired; exit 1 and the table naming what didn't otherwise.
stdlib only for the checking logic - `pytest` itself is scouter's existing
test dependency (pyproject.toml), not a new one this script adds.
"""
import argparse
import ast
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
    try:
        p = subprocess.run(argv, cwd=str(cwd) if cwd else None, capture_output=True,
                            text=True, timeout=timeout, input=input_text)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
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
    """{(class_or_None, funcname): (lineno, end_lineno)} for every test* def -
    pytest collects both bare module-level functions and TestCase-style
    methods, so both are in scope here."""
    out = {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.cls = None

        def visit_ClassDef(self, node):
            prev, self.cls = self.cls, node.name
            self.generic_visit(node)
            self.cls = prev

        def _fn(self, node):
            if node.name.startswith("test"):
                out[(self.cls, node.name)] = (node.lineno, getattr(node, "end_lineno", node.lineno))
            self.generic_visit(node)

        visit_FunctionDef = _fn
        visit_AsyncFunctionDef = _fn

    Visitor().visit(tree)
    return out


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


def run_pytest(root, node_ids, timeout=180):
    """Run exactly `node_ids` with `-k "not host"` (host tests need real
    darktable and never run in this check). Returns (rc, out); rc 5 means
    "every selected test was filtered out by -k", distinct from a real pass
    or a real failure - callers must not read it as either."""
    if not node_ids:
        return 0, "no tests selected"
    argv = ["python3", "-m", "pytest", *node_ids, "-k", "not host", "-q"]
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
MARKER_RE = re.compile(
    r"^#\s*mutation-check:\s*targets-predate-range\s+([0-9a-fA-F]{7,40})\s+--\s*(.+?)\s*$",
    re.MULTILINE)


def extract_marker(source, top_lines=20):
    """(marker_sha, reason) from a top-of-file `# mutation-check:
    targets-predate-range <sha> -- <reason>` comment, or None. Only the first
    `top_lines` are scanned - "top-of-file", not "anywhere in the file" (a
    marker buried mid-file would be too easy to miss in review)."""
    if not source:
        return None
    head_block = "\n".join(source.splitlines()[:top_lines])
    m = MARKER_RE.search(head_block)
    return (m.group(1), m.group(2)) if m else None


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


def _import_names(source):
    """Set of module-name strings a test file's `import`/`from ... import`
    statements reference - the full dotted path, every dotted prefix, and the
    bare last component (the last covers a `sys.path` hack that imports a
    submodule by its bare stem, same as `new_module_import_error`'s bare-stem
    match). A bare relative import (`from . import x`, no module name) is
    skipped - too ambiguous to resolve without walking the package tree, and
    skipping it only means this file falls through to running the revert
    check as before (never a false n/a)."""
    names = set()
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return names

    def add_dotted(dotted):
        names.add(dotted)
        parts = dotted.split(".")
        names.add(parts[-1])
        for i in range(1, len(parts)):
            names.add(".".join(parts[:i]))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add_dotted(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and not node.module:
                continue
            base = node.module or ""
            if base:
                add_dotted(base)
            for alias in node.names:
                names.add(alias.name)
                if base:
                    names.add(f"{base}.{alias.name}")
    return names


def references_prod_file(test_import_names, prod_file):
    """True when `test_import_names` (from `_import_names`) plausibly
    references `prod_file` - matched the same generous way
    `new_module_import_error` matches a missing-module message, since both
    are working from the same "a name the test imports vs. a file path"
    mismatch."""
    dotted, attr, package = _prod_module_candidates(prod_file)
    candidates = {dotted, attr}
    if package:
        candidates.add(package)
    parts = dotted.split(".")
    for i in range(1, len(parts) + 1):
        candidates.add(".".join(parts[:i]))
    return bool(test_import_names & candidates)


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


def static_flags(source, lineno, end_lineno):
    lines = source.splitlines()
    body = "\n".join(lines[lineno - 1:end_lineno])
    flags = []
    if "assert" not in body:
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
    explicit, sha-bound marker says so - fix round 5, `why` is the marker's
    reason) or "marker_mismatch" (a marker is present but its sha does not
    match this gate's --base - fix round 5, never silently ignored),
    "fallback_ok"/"fallback_bad" (the weaken column, round 6: the changed
    function(s) had no assertion at all, so the mutation ran against the
    file's first assertion anywhere instead - always labelled, pass or fail,
    so a reviewer sees it ran against code the range didn't touch). Every
    state but plain True/False, fallback_bad and marker_mismatch is n/a, not
    a fail, and not counted against the gate - the weaken column carries the
    requirement instead."""
    if state is None:
        return "n/a"
    if state == "new_module":
        return "n/a (new module)"
    if state == "no_prod_change":
        return "n/a (no production change)"
    if state == "targets_predate_range":
        return f"n/a (targets predate range: {why})"
    if state == "marker_mismatch":
        return f"marker mismatch (BAD): {why}" if why else "marker mismatch (BAD)"
    if state == "fallback_ok":
        return f"FAIL (good) [{why}]" if why else "FAIL (good) [weaken: file-level fallback]"
    if state == "fallback_bad":
        return f"did not fail (BAD) [{why}]" if why else "did not fail (BAD) [weaken: file-level fallback]"
    if state == "error":
        return "ERROR (not discriminating)"
    return "FAIL (good)" if state else "did not fail (BAD)"


def check_range(repo, base, head):
    repo = Path(repo).resolve()
    changed = changed_files(repo, base, head)
    test_files = [f for f in changed if is_test_file(f)]
    prod_files = [f for f in changed if f.endswith(".py") and not is_test_file(f) and not is_test_support_file(f)]

    rows = []
    ok = True

    if not test_files:
        return True, [], "no new/changed test files in this range"

    per_file_funcs = {}
    for f in test_files:
        status = file_status(repo, base, head, f)
        added = added_line_numbers(repo, base, head, f)
        funcs = changed_test_functions(repo, base, head, f, status, added)
        per_file_funcs[f] = (status, funcs)

    # ---- (d) static flags, read straight from HEAD ----
    static_by_file = {}
    for f, (status, funcs) in per_file_funcs.items():
        src = show_file(repo, head, f)
        flags = []
        for cls, name, s, e in funcs:
            for flag in static_flags(src, s, e):
                flags.append(f"{cls + '.' if cls else ''}{name}: {flag}")
        static_by_file[f] = flags
        if flags:
            ok = False

    # ---- (b) revert-mutation, one worktree, production reverted to base ----
    revert_by_file = {}

    # Fix round 5: an explicit, sha-bound marker overrides everything else
    # below, for exactly the files that carry it - checked first, and
    # regardless of whether any production file changed at all, so a
    # wrong-sha marker is caught (never silently ignored) even on a range
    # with no production changes.
    resolved_base = resolve_sha(repo, base)
    for f in per_file_funcs:
        marker = extract_marker(show_file(repo, head, f))
        if marker is None:
            continue
        marker_sha, reason = marker
        resolved_marker = resolve_sha(repo, marker_sha)
        if resolved_marker and resolved_base and resolved_marker == resolved_base:
            revert_by_file[f] = ("targets_predate_range", reason)
        else:
            revert_by_file[f] = ("marker_mismatch",
                                  f"marker names {marker_sha}, gate --base is {base} ({reason})")
            ok = False

    remaining = {f: v for f, v in per_file_funcs.items() if f not in revert_by_file}

    if prod_files and remaining:
        new_prod_files = [f for f in prod_files if file_status(repo, base, head, f) == "A"]
        with tempfile.TemporaryDirectory(prefix="mutation-check-revert-") as tmp:
            wt = make_worktree(repo, head, Path(tmp))
            try:
                for f in prod_files:
                    status = file_status(repo, base, head, f)
                    revert_to_base(repo, base, wt, f, status)
                for f, (status, funcs) in remaining.items():
                    # Fix round 4: this test file may not even be ABOUT any of
                    # the production files this range changed (e.g. new tests
                    # added for pre-existing code) - reverting production it
                    # never touches can never discriminate it, and reading
                    # that as "did not fail (BAD)" would be a false alarm.
                    test_imports = _import_names(show_file(repo, head, f))
                    if not any(references_prod_file(test_imports, pf) for pf in prod_files):
                        revert_by_file[f] = ("no_prod_change",
                                              "no production file changed in this range is imported by this test file")
                        continue
                    ids = sorted({node_id(f, cls, name) for cls, name, _, _ in funcs})
                    rc, out = run_pytest(wt, ids)
                    if is_collection_error(out):
                        if new_module_import_error(out, new_prod_files):
                            # n/a, not a fail: the module the test imports
                            # simply doesn't exist yet at base - the weaken
                            # column (below) carries the requirement.
                            revert_by_file[f] = ("new_module", out[-800:])
                        else:
                            revert_by_file[f] = ("error", out[-800:])
                            ok = False
                        continue
                    if rc == NO_TESTS_COLLECTED_RC:
                        revert_by_file[f] = (None, "n/a - only host-only tests changed (filtered by -k 'not host')")
                        continue
                    failed = rc != 0
                    revert_by_file[f] = (failed, out[-800:])
                    if not failed:
                        ok = False
            finally:
                remove_worktree(repo, wt)
    else:
        for f in remaining:
            revert_by_file[f] = (None, "no production .py file changed in this range - (b) skipped")

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
                    rc, out = run_pytest(wt, run_ids)
                finally:
                    target.write_text(original, encoding="utf-8")
                if is_collection_error(out):
                    weaken_by_func[f] = ("error", out[-800:])
                    ok = False
                    continue
                if rc == NO_TESTS_COLLECTED_RC:
                    weaken_by_func[f] = (None, "n/a - only host-only tests changed (filtered by -k 'not host')")
                    continue
                failed = rc != 0
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
        names = ", ".join(sorted({f"{cls}.{name}" if cls else name for cls, name, _, _ in funcs})) or "-"
        rev_ok, rev_why = revert_by_file.get(f, (None, "-"))
        weak_ok, weak_why = weaken_by_func.get(f, (None, "-"))
        flags = static_by_file.get(f, [])
        rev_cell = _cell(rev_ok, rev_why)
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
    ok, rows, note = check_range(a.repo, a.base, a.head)
    if note:
        print(note)
        return 0
    print(render_table(rows))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
