#!/usr/bin/env bash
# The HTC merge step's actual call site for the test-honesty gate (fix round
# 1: scripts/mutation_check.sh existed but nothing called it). Task 36 Step 2
# ("Fable reviews, merge gate") and the RUNBOOK/README both name this script
# (or `make gate BASE=.. HEAD=..`, which just execs it) as the thing to run
# before merging an implementer's branch to main.
#
#   scripts/merge_gate.sh <base-sha> <head-sha> [repo-root]
#
# Skippable ONLY by an explicit env var, never silently - mirrors
# capsule-corp's OPS_SKIP_MUTATION_CHECK convention exactly (decision row 459):
# `SCOUTER_SKIP_MUTATION_CHECK=1`. A skip is always logged - printed as a
# tagged WARN line on stderr (this repo has no build-time event bus; scouter's
# own events.jsonl is job telemetry, not a place to bury a merge-gate skip) -
# so a skipped test-honesty check can never pass through quietly.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <base-sha> <head-sha> [repo-root]" >&2
  exit 2
fi

BASE="$1"
HEAD="$2"
ROOT="${3:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

if [[ -n "${SCOUTER_SKIP_MUTATION_CHECK:-}" && "${SCOUTER_SKIP_MUTATION_CHECK}" != "0" ]]; then
  echo "WARN: mutation_check skipped via SCOUTER_SKIP_MUTATION_CHECK=${SCOUTER_SKIP_MUTATION_CHECK} (base=${BASE} head=${HEAD})" >&2
  exit 0
fi

exec "$(dirname "${BASH_SOURCE[0]}")/mutation_check.sh" "$BASE" "$HEAD" "$ROOT"
