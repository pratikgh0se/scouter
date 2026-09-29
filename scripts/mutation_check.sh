#!/usr/bin/env bash
# The entry point the HTC merge step calls for the test-honesty gate
# (mirrors capsule-corp decision row 459 / card t_a995978e). Wraps
# tests/mutation_check.py so a merge step needs no Python argument-plumbing:
# just the base and head shas of the range being merged.
#
#   scripts/mutation_check.sh <base-sha> <head-sha> [repo-root] [--allow-dirty]
#
# Exit 0 with a clean markdown table when every new/changed test in the
# range can fail; exit 1 with the table naming what could not otherwise -
# that non-zero is what the merge step should treat as a block. Exit 2,
# "environment error", when the environment isn't even fit to run the check
# (pytest unavailable, or --repo has uncommitted changes to gate-critical
# paths) - --allow-dirty forwards straight through to tests/mutation_check.py
# for the latter, a local-experiment escape hatch only.
set -euo pipefail

ALLOW_DIRTY=""
POSITIONAL=()
for arg in "$@"; do
  case "$arg" in
    --allow-dirty)
      ALLOW_DIRTY="--allow-dirty"
      ;;
    *)
      POSITIONAL+=("$arg")
      ;;
  esac
done
set -- "${POSITIONAL[@]}"

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <base-sha> <head-sha> [repo-root] [--allow-dirty]" >&2
  exit 2
fi

BASE="$1"
HEAD="$2"
# Where the checker script itself lives is NOT the same thing as which repo
# to check (fix round 1: these were conflated, so `[repo-root]` silently had
# no effect - the script always checked its own checkout). SCRIPT_HOME always
# resolves from this file's own location; ROOT (the `--repo` target) defaults
# to SCRIPT_HOME but a caller (e.g. this script's own tests, against a
# fixture repo) can point it elsewhere.
SCRIPT_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="${3:-$SCRIPT_HOME}"

# Fix round 9 (integrator-reproduced failure): a bare "python3" resolves via
# whatever PATH the CALLER happens to have, which may not be this repo's own
# venv (e.g. invoked via `.venv/bin/python -m pytest ...` - that puts no
# venv `bin/` on PATH at all) - the wrong interpreter here has no pytest,
# and every check this script runs then fails to even start. Prefer this
# repo's own venv (resolved from SCRIPT_HOME, so it is never the CALLER's
# working directory's venv by accident), falling back to PATH only when
# there is none - so the whole chain is self-sufficient from any checkout.
#
# Fix round 10 (card t_0f1197e6, row 478): SCRIPT_HOME is this file's own
# checkout, which in a bare worktree (added via `git worktree add`, no
# `.venv` symlink of its own) has no venv at all, so the chain above fell
# straight through to a system python3 with no pytest installed - `make
# gate` failed the B0 precheck everywhere except the main checkout. Two
# escape hatches ahead of that, both usability, not honesty (B0 still fires
# loudly on a real miss): an explicit $SCOUTER_PYTHON override, then the
# MAIN checkout's venv found via git's common dir (shared by every
# worktree, so this is never a guess at a sibling directory's layout).
PYTHON="${SCOUTER_PYTHON:-}"
if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then
  COMMON_DIR="$(git -C "$SCRIPT_HOME" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  PYTHON="${COMMON_DIR:+$COMMON_DIR/../.venv/bin/python3}"
fi
[[ -n "$PYTHON" && -x "$PYTHON" ]] || PYTHON="$SCRIPT_HOME/.venv/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON="python3"

exec "$PYTHON" "$SCRIPT_HOME/tests/mutation_check.py" --base "$BASE" --head "$HEAD" --repo "$ROOT" $ALLOW_DIRTY
