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
ROOT="${3:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

exec python3 "$ROOT/tests/mutation_check.py" --base "$BASE" --head "$HEAD" --repo "$ROOT"
