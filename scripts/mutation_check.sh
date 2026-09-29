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
# Same chain as scripts/htc_post_run.sh (Z8, card t_0f1197e6/row 478), now
# the SAME CODE (card t_7a05352a): both source scripts/_python.sh, which
# defines resolve_scouter_python -- $SCOUTER_PYTHON, then this checkout's
# own venv, then the MAIN checkout's venv via git's common dir -- so a bare
# worktree still finds pytest.
THIS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_python.sh
# M4 (review): fail loud (not just on `set -e`, so this reads the same as
# the other caller) if this file is ever missing.
source "$THIS_SCRIPT_DIR/_python.sh" || { echo "missing $THIS_SCRIPT_DIR/_python.sh" >&2; exit 2; }
resolve_scouter_python "$SCRIPT_HOME"

exec "$PYTHON" "$SCRIPT_HOME/tests/mutation_check.py" --base "$BASE" --head "$HEAD" --repo "$ROOT" $ALLOW_DIRTY
