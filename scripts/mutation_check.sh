#!/usr/bin/env bash
# The entry point the HTC merge step calls for the test-honesty gate
# (mirrors capsule-corp decision row 459 / card t_a995978e). Wraps
# tests/mutation_check.py so a merge step needs no Python argument-plumbing:
# just the base and head shas of the range being merged.
#
#   scripts/mutation_check.sh <base-sha> <head-sha> [repo-root]
#
# Exit 0 with a clean markdown table when every new/changed test in the
# range can fail; exit 1 with the table naming what could not otherwise -
# that non-zero is what the merge step should treat as a block.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <base-sha> <head-sha> [repo-root]" >&2
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

exec python3 "$SCRIPT_HOME/tests/mutation_check.py" --base "$BASE" --head "$HEAD" --repo "$ROOT"
