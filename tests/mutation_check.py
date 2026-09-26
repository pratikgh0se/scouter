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


def is_test_file(rel_path):
    name = Path(rel_path).name
    return name.startswith("test_") and name.endswith(".py")


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


def check_range(repo, base, head):
    repo = Path(repo).resolve()
    changed = changed_files(repo, base, head)
    test_files = [f for f in changed if is_test_file(f)]
    prod_files = [f for f in changed if f.endswith(".py") and not is_test_file(f)]

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
    if prod_files:
        with tempfile.TemporaryDirectory(prefix="mutation-check-revert-") as tmp:
            wt = make_worktree(repo, head, Path(tmp))
            try:
                for f in prod_files:
                    status = file_status(repo, base, head, f)
                    revert_to_base(repo, base, wt, f, status)
                for f, (status, funcs) in per_file_funcs.items():
                    ids = sorted({node_id(f, cls, name) for cls, name, _, _ in funcs})
                    rc, out = run_pytest(wt, ids)
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
        for f in per_file_funcs:
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
                ids = sorted({node_id(f, cls, name) for cls, name, _, _ in funcs})
                if mutated is None:
                    weaken_by_func[f] = (None, "no assertEqual/assertTrue/`==` assert found to flip")
                    continue
                target.write_text(mutated, encoding="utf-8")
                try:
                    rc, out = run_pytest(wt, ids)
                finally:
                    target.write_text(original, encoding="utf-8")
                if rc == NO_TESTS_COLLECTED_RC:
                    weaken_by_func[f] = (None, "n/a - only host-only tests changed (filtered by -k 'not host')")
                    continue
                failed = rc != 0
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
        rev_cell = "n/a" if rev_ok is None else ("FAIL (good)" if rev_ok else "did not fail (BAD)")
        weak_cell = "n/a" if weak_ok is None else ("FAIL (good)" if weak_ok else "did not fail (BAD)")
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
