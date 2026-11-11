#!/usr/bin/env bash
# Hyperbolic Time Chamber -- night-2 launcher. Modelled directly on
# htc-night1.sh (rows 436/438); only the stamp/dir prefix changes from
# night1 to night2, so night-1's log and progress files are never touched
# by this run and a night-1 runner still alive is caught before darktable
# ever launches twice.
# Usage:  bash scripts/htc-night2.sh          (first start, or resume after a stop/park)
#         STAMP=night2-20260928 bash scripts/htc-night2.sh   (force a run stamp)
# Stop:   kill -INT "$(cat ~/Pictures/ScouterScratch/gauntlet/night2/runner.pid)"
#         (the runner closes its session and quits ITS darktable; rerun this script to resume)
# Exit:   0 done | 75 parked (rerun after the usage reset) | 3 refused/blocked |
#         4 originals changed | 5 precondition failed | other = run-level crash
set -uo pipefail

REPO="${HTC_REPO:-$HOME/CodeBase/scouter}"
PY="$REPO/.venv/bin/python"
RUNS_ROOT="${HTC_RUNS_ROOT:-$HOME/Pictures/ScouterScratch/gauntlet}"
TRIAL="${HTC_TRIAL:-$HOME/Pictures/EditingTrial}"
NIGHT="$RUNS_ROOT/night2"
mkdir -p "$NIGHT"

# One stamp for the whole night, reused by every resume (even after
# midnight). N3 (review): resolved in memory only -- NOT written to
# CURRENT_STAMP yet, so a refused attempt (preconditions below) does not
# pin that day's stamp for a later, successful start.
if [[ -z "${STAMP:-}" ]]; then
  if [[ -s "$NIGHT/CURRENT_STAMP" ]]; then STAMP=$(cat "$NIGHT/CURRENT_STAMP")
  else STAMP="night2-$(date +%Y%m%d)"; fi
fi
RUN_DIR="$RUNS_ROOT/$STAMP"
LOG_DIR="$NIGHT/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/run.log"
PIDFILE="$NIGHT/runner.pid"
exec > >(tee -a "$LOG") 2>&1

say() { echo "[$(date '+%F %T')] $*"; }
die() { say "REFUSED: $*"; exit "${2:-5}"; }

# -- (a) preconditions: no darktable at all (from night-2 OR a still-live
#    night-1 runner), clean tree, disk, corpus ---------------------------
if pgrep -x darktable >/dev/null || pgrep -f "MacOS/darktable" >/dev/null; then
  pgrep -fl darktable
  die "a darktable is running; quit it first (this script never signals it)"
fi
[[ -z "$(git -C "$REPO" status --porcelain)" ]] || die "repo working tree is not clean"
[[ "$(git -C "$REPO" rev-parse --abbrev-ref HEAD)" == main ]] || die "repo is not on main"
# POSIX `df -Pk` (BSD-only `df -g` is rejected by GNU df): column 4 is available KiB.
# df the nearest existing ancestor: ~/Pictures itself may not exist (non-macOS).
df_target="$HOME/Pictures"
while [[ ! -e "$df_target" && "$df_target" != / ]]; do df_target=$(dirname "$df_target"); done
free_gb=$(df -Pk "$df_target" | awk 'NR==2 {print int($4 / 1048576)}')
min_free_gb="${HTC_MIN_FREE_GB:-15}"  # default unchanged; tests on small CI disks lower it
(( free_gb >= min_free_gb )) || die "only ${free_gb} GB free under ~/Pictures (need >= ${min_free_gb})"
[[ -f "$RUNS_ROOT/corpus.json" ]] || die "corpus.json missing (run the corpus survey)"
if [[ -e "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  die "a night-2 runner is already running (pid $(cat "$PIDFILE"))"
fi
NIGHT1_PIDFILE="$RUNS_ROOT/night1/runner.pid"
if [[ -e "$NIGHT1_PIDFILE" ]] && kill -0 "$(cat "$NIGHT1_PIDFILE")" 2>/dev/null; then
  die "a night-1 runner is still running (pid $(cat "$NIGHT1_PIDFILE")); let it finish or stop it first"
fi
# The two pidfile checks above only catch this script's own launchers -- a
# smoke run or a manual `gauntlet run --stamp ...` invoked outside either
# launcher would still race darktable. Catch it directly: the real
# launcher's argv has "gauntlet" and "run --stamp" NOT adjacent, so the
# pattern must allow anything in between.
if pgrep -f "gauntlet.*run --stamp" >/dev/null 2>&1; then
  pgrep -fl "gauntlet.*run --stamp" || true
  die "a gauntlet runner (smoke or manual) is already running outside this launcher"
fi

# Preconditions hold -- now, and only now, pin today's stamp so a refused
# attempt above never pins it (N3).
echo "$STAMP" > "$NIGHT/CURRENT_STAMP"

# -- (c) EditingTrial originals: read-only listing before/after ---------------
snap() { find "$TRIAL" -type f -exec stat -f '%z %m %N' {} + 2>/dev/null | sort; }
snap > "$LOG_DIR/originals.before"

say "stamp $STAMP  run dir $RUN_DIR  commit $(git -C "$REPO" rev-parse --short HEAD)"
say "progress: $RUN_DIR/progress.json   log: $RUN_DIR/log.md   this log: $LOG"
cd "$REPO" || die "cannot cd $REPO"
export PYTHONPATH="$REPO"

if [[ ! -f "$RUN_DIR/cases.jsonl" ]]; then
  # Night 2 runs a STRATIFIED 40-case subset of scenarios_agent (every
  # problem class and genre represented at least once), not the full 160.
  # The seed is derived from the stamp's own text DETERMINISTICALLY (a
  # sha1 of the stamp string, truncated to 8 hex digits and read as a
  # base-16 integer -- `--seed` is `type=int`), so a resume (cases.jsonl
  # already exists -> this block never re-runs) always sees the exact same
  # subset, and regenerating the same stamp always derives the same seed
  # too -- unlike `date -j -f '%Y%m%d' "${STAMP#night2-}" +%s`, which fills
  # hour/minute/second from the current clock and so disagrees between two
  # calls seconds apart for the same stamp.
  SUBSET_SEED="${HTC_NIGHT2_SUBSET_SEED:-$((16#$(printf '%s' "$STAMP" | shasum | cut -c1-8)))}"
  "$PY" -m gauntlet generate --stamp "$STAMP" \
    --subset "${HTC_NIGHT2_SUBSET_SIZE:-40}" --subset-layer scenarios_agent \
    --subset-keys class,genre --seed "$SUBSET_SEED" || die "generate failed" 2
  # The subset (40 of 160 scenarios_agent cases) shrinks the total well
  # below the full run's own count -- no fixed number to grep for here.
  grep -m1 -i "total\|cases" "$RUN_DIR/generation.md" || true
fi

caffeinate -i -w $$ &   # keep the laptop awake for this script's lifetime

# Checkpoints: refresh progress.html every 5 min (read-only; the controller republishes).
( while sleep 300; do
    "$PY" -m gauntlet progress-html --stamp "$STAMP" --title "Scouter Gauntlet" >/dev/null 2>&1
    "$PY" - "$RUN_DIR/progress.json" <<'EOF' 2>/dev/null
import json, sys
p = json.load(open(sys.argv[1]))
print("[checkpoint]", p.get("status"), {k: v.get("status") for k, v in p.get("layers", {}).items()})
EOF
  done ) &
CKPT=$!

CHILD=""
on_stop() { say "stop requested: forwarding SIGINT to runner $CHILD"; [[ -n "$CHILD" ]] && kill -INT "$CHILD" 2>/dev/null; }
trap on_stop INT TERM

run_pass() {  # $@ = --layer args
  # A background job of a non-interactive bash starts with SIGINT ignored;
  # restore Python's handler so a stop runs the runner's cleanup (session
  # close -> our darktable quit, agent claude killed) before it exits.
  "$PY" -c 'import signal, sys; signal.signal(signal.SIGINT, signal.default_int_handler)
from gauntlet.cli import main; sys.exit(main(sys.argv[1:]))' \
    run --stamp "$STAMP" --resume --wait-on-park "$@" &
  CHILD=$!; echo "$CHILD" > "$PIDFILE"
  wait "$CHILD"; local rc=$?
  while kill -0 "$CHILD" 2>/dev/null; do wait "$CHILD"; rc=$?; done  # wait again after a trap
  rm -f "$PIDFILE"; return "$rc"
}

# (b) pass 1: every tool-mode layer, same order as night-1; pass 2: agent
# mode (row 436: it runs too, meter ignored) -- last, so a usage-limit
# park cannot hold up the tool layers.
say "pass 1: tool-mode layers"
run_pass --layer offline --layer coverage --layer scenarios --layer bulk --layer looks \
         --layer robustness --layer parity --layer install --layer guide
rc1=$?
say "pass 1 exit $rc1"
rc2=skipped
if (( rc1 == 0 )); then
  say "pass 2: scenarios_agent"
  run_pass --layer scenarios_agent; rc2=$?
  say "pass 2 exit $rc2"
fi

kill "$CKPT" 2>/dev/null
"$PY" -m gauntlet progress-html --stamp "$STAMP" --title "Scouter Gauntlet" || true

snap > "$LOG_DIR/originals.after"
if ! cmp -s "$LOG_DIR/originals.before" "$LOG_DIR/originals.after"; then
  say "ORIGINALS CHANGED under $TRIAL -- see $LOG_DIR/originals.{before,after}"; exit 4
fi
if pgrep -x darktable >/dev/null; then say "WARNING: a darktable is still running"; pgrep -fl darktable; fi

final=$rc1; [[ "$rc2" != skipped ]] && final=$rc2
if (( final == 0 )); then
  "$PY" -m gauntlet report --stamp "$STAMP" || final=1
fi
say "done: pass1=$rc1 pass2=$rc2 -> exit $final (0 done, 75 parked, 3 blocked, else crash)"
exit "$final"
