#!/usr/bin/env bash
# The HTC merge step's actual call site for the test-honesty gate (fix round
# 1: scripts/mutation_check.sh existed but nothing called it). Task 36 Step 2
# ("Fable reviews, merge gate") and the RUNBOOK/README both name this script
# (or `make gate BASE=.. HEAD=..`, which just execs it) as the thing to run
# before merging an implementer's branch to main.
#
#   scripts/merge_gate.sh <base-sha> <head-sha> [repo-root] [--allow-dirty]
#
# Skippable ONLY by an explicit env var, never silently - mirrors
# capsule-corp's OPS_SKIP_MUTATION_CHECK convention exactly (decision row 459):
# `SCOUTER_SKIP_MUTATION_CHECK=1`. A skip is always logged - printed as a
# tagged WARN line on stderr (this repo has no build-time event bus; scouter's
# own events.jsonl is job telemetry, not a place to bury a merge-gate skip) -
# so a skipped test-honesty check can never pass through quietly.
#
# Dirty-tree refusal: the incident this closes is the gate's own wiring
# (Makefile's `gate` target, this script, tests/mutation_check.py) sitting
# UNCOMMITTED for several merge-gate rounds while a run exercised it by file
# path only - a "clean" gate run said nothing about the fact that the thing
# actually running, or the production code (scouter/) it protects, was not
# what HEAD said it was. This script refuses on its own, as a second,
# independent oracle alongside tests/mutation_check.py's own check (Zenkai
# Z2) - both must agree the tree is clean, or both must be told
# --allow-dirty. Escape hatch for local experiments only: --allow-dirty,
# which prints a loud WARN naming the dirty paths and is forwarded to
# tests/mutation_check.py so it does not independently refuse either.
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
ROOT="${3:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

if [[ -n "${SCOUTER_SKIP_MUTATION_CHECK:-}" && "${SCOUTER_SKIP_MUTATION_CHECK}" != "0" ]]; then
  echo "WARN: mutation_check skipped via SCOUTER_SKIP_MUTATION_CHECK=${SCOUTER_SKIP_MUTATION_CHECK} (base=${BASE} head=${HEAD})" >&2
  exit 0
fi

# The gate-critical paths, in the repo UNDER TEST (ROOT) -- never this
# script's own SCRIPT_HOME, which may be a different checkout entirely (a
# fixture repo passed as [repo-root] rarely has any of these; git status
# with an explicit pathspec simply reports nothing for a path that does not
# exist, so that is not an error).
DIRTY="$(git -C "$ROOT" status --porcelain --no-renames -- \
  Makefile scripts tests/mutation_check.py scouter 2>/dev/null | grep -v '^??' || true)"

if [[ -n "$DIRTY" ]]; then
  if [[ -z "$ALLOW_DIRTY" ]]; then
    {
      echo "environment error: uncommitted changes to gate-critical paths in ${ROOT}:"
      echo "$DIRTY" | sed -E 's/^.{3}/  /'
      echo "Commit them first, or pass --allow-dirty for a local experiment only."
    } >&2
    exit 2
  fi
  {
    echo "WARN: --allow-dirty set; running with uncommitted changes to gate-critical paths in ${ROOT} (local experiments only, never a real merge decision):"
    echo "$DIRTY" | sed -E 's/^.{3}/  /'
  } >&2
fi

exec "$(dirname "${BASH_SOURCE[0]}")/mutation_check.sh" "$BASE" "$HEAD" "$ROOT" $ALLOW_DIRTY
