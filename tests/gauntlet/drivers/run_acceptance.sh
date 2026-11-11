#!/bin/bash
# Row 429 part B: Scouter acceptance run. Run with darktable CLOSED.
#
#   1. scouter setup (real: persistent plugin, luarc, doctor, MCP line) + setup --check
#   2. Sony WB calibration, folder mode: --camera a7, then --camera nex5n
#      (darktable launched on each scratch copy first, per the README recipe)
#   3. acceptance per body (olympus, sony) into a fresh library root under
#      ~/Pictures/ScouterScratch/acceptance/runs/<stamp>/: import, cull,
#      darkroom open, three phrases, undo, preset save, job run, history read
#   4. MCP parity over stdio (darkroom_say, preset_list, job_run --dry-run)
#   5. editor benchmark run 2 (plan v2, label phase3-v2) per body from
#      tests/gauntlet/benchmark, results committed on main
#   6. quit darktable; final table + crash reports before/after
#
# Every wait is bounded. Log: ~/Pictures/ScouterScratch/acceptance/log.md
# (appended as it goes); per-step JSON under the run dir's steps/.
# Expect roughly 25-40 minutes, most of it the two benchmark runs.
set -u
SP="$HOME/CodeBase/scouter/tests/gauntlet/drivers"
REPO="$HOME/CodeBase/scouter"
PY=$REPO/.venv/bin/python
export PYTHONPATH=$REPO
unset SCOUTER_CONFIG SCOUTER_CACHE SCOUTER_JOB
cd "$REPO" || exit 1
if pgrep -x darktable >/dev/null; then echo "darktable is running -- quit it first"; exit 1; fi
[ -x "$PY" ] || { echo "no venv python at $PY"; exit 1; }
[ -f "$REPO/tests/gauntlet/benchmark/run_editor_benchmark.py" ] || echo "note: benchmark harness missing -- step 5 will be skipped"
echo "== main $(git rev-parse --short HEAD)"
mkdir -p ~/Pictures/ScouterScratch/acceptance
exec "$PY" "$SP/acceptance_driver.py"
