#!/bin/zsh
# Row 430/431 live check (fix/commit-noop-hang). Run with darktable CLOSED.
# Installs the plugin from .worktrees/noophang, launches darktable on a copy of
# one ScouterScratch bench frame, runs history_commit on the unedited frame
# (timeout_s=10: must answer within 12s as noop=true), then darkroom_say "brighter by 1 ev" and
# darkroom_enable denoiseprofile over MCP, prints before/after exposure and
# history length, quits darktable, restores the previous plugin
# (KEEP_FIX_PLUGIN=1 to keep the fix installed).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
if pgrep -x darktable >/dev/null; then echo "darktable is running -- quit it first"; exit 1; fi
cd /Users/pratik/CodeBase/scouter
exec /Users/pratik/CodeBase/scouter/.venv/bin/python "$HERE/adjust_check.py" "$@"
