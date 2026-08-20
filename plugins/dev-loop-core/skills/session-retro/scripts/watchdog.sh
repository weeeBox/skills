#!/bin/bash
# Same-day staleness watchdog. Runs every ~15-30 min (launchd StartInterval, not
# StartCalendarInterval - see the sibling .plist), independent of the daily
# session-retro report. All logic lives in staleness_watchdog.py; this wrapper only
# resolves paths, runs it, and turns any newly-flagged session into ONE notification.
set -u

BASE="$HOME/.claude/session-reports"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-/usr/bin/python3}"
LOG="$BASE/logs/watchdog-$(date +%F).log"
LOCK="$BASE/.watchdog-lock"

mkdir -p "$BASE/logs" "$BASE/work"

notify() {  # best-effort AND detached, same as run_retro.sh's notify()
  ( /usr/bin/osascript -e "display notification \"$1\" with title \"session-retro watchdog\"" \
      >>"$LOG" 2>&1 || true ) &
}

# lock so an overlapping fire (machine woke from sleep, prior run still finishing) can't
# double-run; stale (>10 min - this should finish in well under a minute) locks are cleared
if [ -d "$LOCK" ]; then
  age=$(( $(date +%s) - $(stat -f %m "$LOCK") ))
  if [ "$age" -gt 600 ]; then
    mv "$LOCK" "$LOCK.stale.$$" 2>/dev/null && rmdir "$LOCK.stale.$$" 2>/dev/null
  fi
fi
if ! mkdir "$LOCK" 2>/dev/null; then
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

exec >> "$LOG" 2>&1
echo "=== watchdog run $(date '+%F %T') ==="

flagged=$("$PY" "$SCRIPT_DIR/staleness_watchdog.py" scan)
if [ -n "$flagged" ]; then
  echo "$flagged"
  count=$(echo "$flagged" | wc -l | tr -d ' ')
  first=$(echo "$flagged" | head -1)
  notify "$count session(s) may be stuck - $first"
else
  echo "nothing stale"
fi
