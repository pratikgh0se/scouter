#!/usr/bin/env bash
# ops/install-backup-job.sh -- render, lint and load Scouter's nightly backup
# launchd job (com.pratik.scouter-backup). `--uninstall` reverses it.
#
# Refuses to install when the backup gate (`scouter config show`) is not
# `ok`: a launchd job that fires against an unconfigured backup target would
# just accumulate `backup_not_configured` failures in the log, silently.
#
# No test runs this script end to end -- task 13's tests render the template
# and lint it directly. Installing is Pratik's own action.
set -euo pipefail

LABEL="com.pratik.scouter-backup"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$SCRIPT_DIR/launchd/${LABEL}.plist.template"
PLIST_DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_PATH="$HOME/Library/Logs/scouter-backup.log"
VAULT_PATH="${OBSIDIAN_VAULT_PATH:-$HOME/second-brain}"
PYTHON_BIN="${SCOUTER_PYTHON:-$(command -v python3 || true)}"

usage() {
  echo "usage: $(basename "$0") [--uninstall]" >&2
  exit 2
}

uninstall() {
  echo "Unloading ${LABEL} (if loaded)..."
  launchctl bootout "gui/$(id -u)" "$PLIST_DEST" 2>/dev/null || true
  if [ -f "$PLIST_DEST" ]; then
    rm -f "$PLIST_DEST"
    echo "Removed ${PLIST_DEST}"
  else
    echo "${PLIST_DEST} was not present"
  fi
  exit 0
}

if [ "${1:-}" = "--uninstall" ]; then
  uninstall
elif [ -n "${1:-}" ]; then
  usage
fi

if [ ! -f "$TEMPLATE" ]; then
  echo "error: template not found at $TEMPLATE" >&2
  exit 1
fi

if [ -z "$PYTHON_BIN" ] || ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "error: no python3 on PATH (set SCOUTER_PYTHON)" >&2
  exit 1
fi

echo "Checking the backup gate (scouter config show)..."
GATE_JSON="$(cd "$REPO_ROOT" && "$PYTHON_BIN" -m scouter config show 2>&1)" || {
  echo "error: \`scouter config show\` failed:" >&2
  echo "$GATE_JSON" >&2
  exit 1
}
GATE="$(printf '%s' "$GATE_JSON" | "$PYTHON_BIN" -c \
  'import json, sys
try:
    doc = json.load(sys.stdin)
    print(doc.get("data", {}).get("backup_gate", "unknown"))
except Exception:
    print("unknown")' 2>/dev/null || echo unknown)"
if [ "$GATE" != "ok" ]; then
  echo "error: backup gate is not ok (${GATE}); refusing to install the nightly job." >&2
  echo "run \`scouter config show\` to see the failing check, fix it, and retry." >&2
  exit 1
fi
echo "Backup gate: ok"

mkdir -p "$(dirname "$LOG_PATH")"
mkdir -p "$(dirname "$PLIST_DEST")"

TMP_PLIST="$(mktemp "${TMPDIR:-/tmp}/scouter-backup-plist.XXXXXX")"
trap 'rm -f "$TMP_PLIST"' EXIT

sed \
  -e "s#@PYTHON@#${PYTHON_BIN}#g" \
  -e "s#@REPO@#${REPO_ROOT}#g" \
  -e "s#@VAULT@#${VAULT_PATH}#g" \
  -e "s#@LOG@#${LOG_PATH}#g" \
  "$TEMPLATE" > "$TMP_PLIST"

if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$TMP_PLIST"
else
  echo "warning: plutil not found; skipping lint" >&2
fi

cp "$TMP_PLIST" "$PLIST_DEST"
echo "Wrote ${PLIST_DEST}"

launchctl bootout "gui/$(id -u)" "$PLIST_DEST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
echo "Loaded ${LABEL}."

NEXT_RUN="$("$PYTHON_BIN" - <<'PYEOF'
import datetime

now = datetime.datetime.now()
target = now.replace(hour=3, minute=30, second=0, microsecond=0)
if target <= now:
    target += datetime.timedelta(days=1)
print(target.isoformat(timespec="minutes"))
PYEOF
)"
echo "Next scheduled run: ${NEXT_RUN} (StartCalendarInterval 03:30 daily, RunAtLoad false)"
