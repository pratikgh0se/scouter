#!/usr/bin/env bash
# HTC post-run runbook (rows 458/461/463: night-1's exit hands off into the
# post-run batch -- the merge-time gate, the merge and push, host probes,
# the stale-history live check, then the targeted gauntlet rerun -- before
# night-2 starts).
#
# Usage:
#   BATCH=<sha> RUN_DIR=<path/to/run-dir> scripts/htc_post_run.sh [--execute]
#
# Idempotent: rerunning is always safe -- this script only reads
# progress.json, a pid file, and computes a merge-base; it prints (never
# runs) the merge, the push, the host probes, the stale-history live check
# and the targeted gauntlet rerun. Per common-rules.md this implementer
# never merges, pushes, or touches darktable itself, so those steps are
# ALWAYS print-only, with or without --execute. `--execute` additionally
# runs the one step that is genuinely non-darktable and safe to run for
# real here: the merge-time gate (fix round 1, review I6 -- the mutation
# check PLUS pytest, over the batch's own range, not `make gate` alone).
#
# Fix round 2 (review R3): the gate is step 1, BEFORE the merge (so it runs
# from a batch checkout/worktree that may have no `.venv`, hence the PY
# resolution below); once BATCH is already merged into main, a rerun
# reports that plainly and still prints the remaining steps, rather than
# refusing -- "idempotent, rerun anytime" holds either way.
#
# Exit 0 when preconditions hold (and, with --execute, the gate and pytest
# pass, or BATCH is already merged); exit 1 when a precondition fails or
# --execute's checks fail; exit 2 on a usage error.
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

# I6 (review): overridable so a fake two-commit repo can stand in for the
# real scouter checkout in tests, without this script ever guessing wrong
# about which repo BATCH/main belong to.
SCRIPT_HOME="${HTC_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# Fix round 2 (review R3): before the merge, this script typically runs
# from the batch's OWN checkout or a bare worktree, neither of which has
# its own `.venv` -- resolve, in order: an explicit $SCOUTER_PYTHON, this
# repo's own `.venv`, then the MAIN checkout's `.venv` (found via git's
# common dir, which a worktree always has and a plain checkout's own root
# equals), before falling back to a bare `python3` on PATH.
if [[ -n "${SCOUTER_PYTHON:-}" && -x "${SCOUTER_PYTHON}" ]]; then
  PY="$SCOUTER_PYTHON"
elif [[ -x "$SCRIPT_HOME/.venv/bin/python3" ]]; then
  PY="$SCRIPT_HOME/.venv/bin/python3"
else
  GIT_COMMON_DIR="$(git -C "$SCRIPT_HOME" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  MAIN_ROOT="$([[ -n "$GIT_COMMON_DIR" ]] && dirname "$GIT_COMMON_DIR" || true)"
  if [[ -n "$MAIN_ROOT" && -x "$MAIN_ROOT/.venv/bin/python3" ]]; then
    PY="$MAIN_ROOT/.venv/bin/python3"
  else
    PY=python3
  fi
fi

say() { echo "[$(date '+%F %T')] $*"; }

# -- (a) precondition 1: progress.json says the run is done, AND (I8)
#    every expected layer -- including scenarios_agent, the second pass --
#    itself terminated cleanly. `doc.status == "done"` alone is NOT
#    sufficient: it is also written when an individual layer ended
#    `stopped_crash` (the loop only stops on a refused precondition, not a
#    crashed layer), and the two-pass launchers (htc-night1.sh/
#    htc-night2.sh) call `gauntlet run` twice against the SAME
#    progress.json, so pass 1's "done" can be read after pass 1 alone,
#    before pass 2 (scenarios_agent) ever starts. -----------------------
PROGRESS_JSON="$RUN_DIR/progress.json"
#: HTC_EXPECTED_LAYERS overridable (tests; a smoke run with fewer layers).
#: Default matches htc-night1.sh/htc-night2.sh's own pass-1 + pass-2 order.
EXPECTED_LAYERS="${HTC_EXPECTED_LAYERS:-offline,coverage,scenarios,bulk,looks,robustness,parity,install,guide,scenarios_agent}"
ok=1
if [[ ! -f "$PROGRESS_JSON" ]]; then
  say "BLOCKED: no progress.json under $RUN_DIR"
  ok=0
else
  status_out="$("$PY" -c '
import json, sys
try:
    doc = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as e:
    print("<unreadable: %s>" % e)
    print("")
    raise SystemExit(0)
expected = [x for x in sys.argv[2].split(",") if x]
layers = doc.get("layers", {})
bad = []
for name in expected:
    st = (layers.get(name) or {}).get("status")
    if st != "done":
        bad.append("%s=%s" % (name, st))
print(doc.get("status", "<no status field>"))
print(",".join(bad))
' "$PROGRESS_JSON" "$EXPECTED_LAYERS" 2>/dev/null)"
  doc_status="$(sed -n '1p' <<<"$status_out")"
  bad_layers="$(sed -n '2p' <<<"$status_out")"
  if [[ "$doc_status" != "done" ]]; then
    say "BLOCKED: progress.json status is '$doc_status', not done"
    ok=0
  elif [[ -n "$bad_layers" ]]; then
    say "BLOCKED: not every expected layer is done ($bad_layers)"
    ok=0
  else
    say "progress.json status: done, every expected layer done ($EXPECTED_LAYERS)"
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

# -- (a) precondition 3 (review I8): the LAUNCHER process itself -- the
#    pidfile above only tracks the inner `gauntlet run` subprocess, which
#    run_pass deletes BETWEEN pass 1 and pass 2, a window where nothing
#    guards against the launcher (htc-night1.sh/htc-night2.sh) still being
#    mid-script (checkpoint loop, about to start pass 2). A direct pgrep on
#    the command line every pass actually runs (`... run --stamp <STAMP>`)
#    catches an ACTIVELY running pass; combined with precondition 1's
#    per-layer check (which catches the gap between passes), the run is
#    genuinely over. Overridable for tests that fake `pgrep`.
if pgrep -f "run --stamp $STAMP_NAME" >/dev/null 2>&1; then
  say "BLOCKED: a gauntlet runner for stamp $STAMP_NAME is still running"
  ok=0
else
  say "no live runner for stamp $STAMP_NAME"
fi

if [[ "$ok" -ne 1 ]]; then
  say "preconditions not met -- refusing the post-run batch for $BATCH"
  exit 1
fi

# I6 (review): BASE for the merge-time gate is merge-base(main, BATCH),
# captured HERE, before any merge -- never `git rev-parse HEAD` after a
# ff-merge (HEAD == BATCH by then, an empty range that gates nothing) and
# never BATCH's own HEAD run from an unmerged main (a reversed range).
MERGE_BASE="$(git -C "$SCRIPT_HOME" merge-base main "$BATCH" 2>/dev/null || true)"
BATCH_SHA="$(git -C "$SCRIPT_HOME" rev-parse "$BATCH" 2>/dev/null || true)"
if [[ -z "$MERGE_BASE" || -z "$BATCH_SHA" ]]; then
  say "BLOCKED: could not compute merge-base(main, $BATCH) in $SCRIPT_HOME"
  exit 1
fi
# Fix round 2 (review R3): merge-base(main, BATCH) == BATCH can only happen
# when BATCH is already an ancestor of main (git's own definition of
# merge-base) -- i.e. BATCH is ALREADY MERGED, never an error. Round 1
# treated this as BLOCKED/exit 1, which broke "idempotent, rerun anytime":
# rerunning this script right after the human ran step 2 (the merge) used
# to refuse. Report it plainly instead and keep going -- steps 2-5 (the
# merge itself is a no-op by then, but harmless to print; the host probes,
# stale-history check and targeted rerun still apply) print as usual, and
# step 1 explains why there is nothing new to gate rather than printing a
# vacuous BASE==HEAD command.
ALREADY_MERGED=0
if [[ "$MERGE_BASE" == "$BATCH_SHA" ]]; then
  ALREADY_MERGED=1
  say "$BATCH_SHA is already merged into main (merge-base(main, $BATCH) == $BATCH_SHA) -- nothing new to gate for it"
fi

say "preconditions met for batch $BATCH ($RUN_DIR)"
echo
echo "# --- post-run batch: run these IN ORDER, by hand -----------------------"
echo
if [[ "$ALREADY_MERGED" -eq 1 ]]; then
  echo "# 1) merge-time gate: SKIPPED -- $BATCH_SHA is already merged into main"
  echo "#    (a NEW batch's own BASE is this run's current main tip; rerun this"
  echo "#    script with that batch's sha to get a real gate command)"
else
  echo "# 1) merge-time gate: mutation check + pytest, BASE computed BEFORE the merge"
  echo "make -C \"$SCRIPT_HOME\" gate BASE=$MERGE_BASE HEAD=$BATCH_SHA"
  echo "$PY -m pytest -q -m \"not host\""
fi
echo
echo "# 2) ff-merge the batch into main, then push (never run by this script)"
echo "git -C \"$SCRIPT_HOME\" fetch origin main"
echo "git -C \"$SCRIPT_HOME\" checkout main"
echo "git -C \"$SCRIPT_HOME\" merge --ff-only $BATCH"
echo "git -C \"$SCRIPT_HOME\" push origin main"
echo
echo "# 3) host probes (quit any running darktable first; never run by this script)"
echo "SCOUTER_HOST_TESTS=1 $PY -m pytest tests/test_host_phase3.py::ProbeCandidatePathsHostTest -q -s"
echo "SCOUTER_HOST_TESTS=1 $PY -m pytest tests/test_host_colorcal.py -q -s"
echo
echo "# 4) stale-history live check (never run by this script):"
echo "#    darkroom_say \"faded blacks a touch\""
echo "#    then verify: history_end >= before, newest is exposure,"
echo "#    black == target, strategy == view_bounce_action"
echo
echo "# 5) targeted GAUNTLET rerun (not 'make gate') of night-1's failed/"
echo "#    changed case ids, via the gauntlet runner (RULING: this is what"
echo "#    'targeted gate rerun' means in the post-run batch -- an id-list"
echo "#    file, not the mutation-check gate above, which is step 1)"
echo "$PY -m gauntlet run --stamp <new-stamp> --ids-file <path/to/targeted-ids.txt>"
echo

if [[ "$EXECUTE" -eq 1 ]]; then
  if [[ "$ALREADY_MERGED" -eq 1 ]]; then
    say "--execute: $BATCH_SHA is already merged; nothing to gate, exiting 0"
    exit 0
  fi
  say "--execute: running step 1 (the merge-time gate + pytest) for real; steps 2-5 stay print-only"
  GATE_CMD=${GATE_CMD:-"make -C \"$SCRIPT_HOME\" gate BASE=\"$MERGE_BASE\" HEAD=\"$BATCH_SHA\""}
  say "+ $GATE_CMD"
  # A subshell, not a bare `eval`: GATE_CMD/PYTEST_CMD may themselves be (or
  # contain) a literal `exit N` -- a bare eval would run that in THIS
  # script's own process and terminate the runbook early, before the
  # second command ever runs.
  ( eval "$GATE_CMD" )
  rc=$?
  say "gate rerun exit $rc"
  if [[ "$rc" -ne 0 ]]; then
    exit "$rc"
  fi
  PYTEST_CMD=${PYTEST_CMD:-"\"$PY\" -m pytest -q -m \"not host\""}
  say "+ $PYTEST_CMD"
  ( cd "$SCRIPT_HOME" && eval "$PYTEST_CMD" )
  rc=$?
  say "pytest exit $rc"
  exit "$rc"
fi

exit 0
