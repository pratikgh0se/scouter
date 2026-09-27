#!/usr/bin/env bash
# HTC post-run runbook (rows 458/461/463: night-1's exit hands off into the
# post-run batch -- merge, push, host probes, the stale-history live check,
# then the targeted gate rerun -- before night-2 starts).
#
# Usage:
#   BATCH=<sha> RUN_DIR=<path/to/run-dir> scripts/htc_post_run.sh [--execute]
#
# Idempotent: rerunning is always safe -- this script only reads
# progress.json and a pid file, and prints (never runs) the merge, the
# push, the host probes and the stale-history live check. Per
# common-rules.md this implementer never merges, pushes, or touches
# darktable itself, so those four steps are ALWAYS print-only, with or
# without --execute. `--execute` additionally runs the one step that is
# genuinely non-darktable and safe to run for real here: the targeted gate
# rerun (`make gate`, plain pytest + the mutation check, no darktable, no
# network). Exit 0 when preconditions hold (and, with --execute, the gate
# passes); exit 1 when either check fails; exit 2 on a usage error.
set -uo pipefail

usage() { echo "usage: BATCH=<sha> RUN_DIR=<run-dir> $0 [--execute]" >&2; }

EXECUTE=0
for arg in "$@"; do
  case "$arg" in
    --execute) EXECUTE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "${BATCH:-}" ]]; then
  echo "BATCH=<sha> is required" >&2; usage; exit 2
fi
if [[ -z "${RUN_DIR:-}" ]]; then
  echo "RUN_DIR=<run-dir> is required" >&2; usage; exit 2
fi
if [[ ! -d "$RUN_DIR" ]]; then
  echo "RUN_DIR does not exist: $RUN_DIR" >&2; exit 2
fi

SCRIPT_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$SCRIPT_HOME/.venv/bin/python3"
[[ -x "$PY" ]] || PY=python3

say() { echo "[$(date '+%F %T')] $*"; }

# -- (a) precondition 1: progress.json says the run is done -----------------
PROGRESS_JSON="$RUN_DIR/progress.json"
ok=1
if [[ ! -f "$PROGRESS_JSON" ]]; then
  say "BLOCKED: no progress.json under $RUN_DIR"
  ok=0
else
  status="$("$PY" -c '
import json, sys
try:
    doc = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as e:
    print("<unreadable: %s>" % e)
    raise SystemExit(0)
print(doc.get("status", "<no status field>"))
' "$PROGRESS_JSON" 2>/dev/null)"
  if [[ "$status" == "done" ]]; then
    say "progress.json status: done"
  else
    say "BLOCKED: progress.json status is '$status', not done"
    ok=0
  fi
fi

# -- (a) precondition 2: the runner pid is gone ------------------------------
# The night-N launchers (htc-night1.sh, scripts/htc-night2.sh) keep their
# pidfile at RUNS_ROOT/<night-prefix>/runner.pid, a SIBLING of the stamped
# RUN_DIR (RUNS_ROOT/<stamp>), not inside it -- <night-prefix> is the stamp's
# prefix before its first "-" (e.g. stamp "night1-20260927" -> "night1").
# PIDFILE can be overridden directly (tests; an unconventional layout).
STAMP_NAME="$(basename "$RUN_DIR")"
NIGHT_PREFIX="${STAMP_NAME%%-*}"
PIDFILE="${PIDFILE:-$(dirname "$RUN_DIR")/$NIGHT_PREFIX/runner.pid}"
if [[ -f "$PIDFILE" ]]; then
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    say "BLOCKED: runner pid $pid ($PIDFILE) is still running"
    ok=0
  else
    say "runner pid gone (stale $PIDFILE, pid '${pid:-<empty>}' not running)"
  fi
else
  say "runner pid gone (no $PIDFILE)"
fi

if [[ "$ok" -ne 1 ]]; then
  say "preconditions not met -- refusing the post-run batch for $BATCH"
  exit 1
fi

say "preconditions met for batch $BATCH ($RUN_DIR)"
echo
echo "# --- post-run batch: run these IN ORDER, by hand -----------------------"
echo
echo "# 1) ff-merge the batch into main, then push (never run by this script)"
echo "git -C \"$SCRIPT_HOME\" fetch origin main"
echo "git -C \"$SCRIPT_HOME\" checkout main"
echo "git -C \"$SCRIPT_HOME\" merge --ff-only $BATCH"
echo "git -C \"$SCRIPT_HOME\" push origin main"
echo
echo "# 2) host probes (quit any running darktable first; never run by this script)"
echo "SCOUTER_HOST_TESTS=1 $SCRIPT_HOME/.venv/bin/python -m pytest tests/test_host_phase3.py::ProbeCandidatePathsHostTest -q -s"
echo "SCOUTER_HOST_TESTS=1 $SCRIPT_HOME/.venv/bin/python -m pytest tests/test_host_colorcal.py -q -s"
echo
echo "# 3) stale-history live check (never run by this script):"
echo "#    darkroom_say \"faded blacks a touch\""
echo "#    then verify: history_end >= before, newest is exposure,"
echo "#    black == target, strategy == view_bounce_action"
echo
echo "# 4) targeted gate rerun"
echo "make -C \"$SCRIPT_HOME\" gate BASE=$BATCH HEAD=\$(git -C \"$SCRIPT_HOME\" rev-parse HEAD)"
echo

if [[ "$EXECUTE" -eq 1 ]]; then
  say "--execute: running step 4 (the gate rerun) for real; steps 1-3 stay print-only"
  HEAD_SHA="$(git -C "$SCRIPT_HOME" rev-parse HEAD)"
  GATE_CMD=${GATE_CMD:-"make -C \"$SCRIPT_HOME\" gate BASE=\"$BATCH\" HEAD=\"$HEAD_SHA\""}
  say "+ $GATE_CMD"
  eval "$GATE_CMD"
  rc=$?
  say "gate rerun exit $rc"
  exit "$rc"
fi

exit 0
