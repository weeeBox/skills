#!/usr/bin/env bash
# review-gate wedge watchdog, owned by the review-gate skill.
#
# NOT the completion signal - completion is decided by the orchestrator polling the job's
# `result` field. This only answers "has output stopped moving long enough to go look?"
# (done OR wedged - read the tail to tell which).
#
# Usage:
#   watch-agent-output.sh <log-file> [silent_secs=120] [poll_secs=15]   # EXACT-FILE (the mode the skill uses)
#   watch-agent-output.sh <dir>      [missing_secs=240] [silent_secs=120] [poll_secs=15]  # dir (legacy)
#   watch-agent-output.sh --selftest
# Exact-file mode is immune to the dir-scan mtime>=start race (a log written before the
# watchdog arms is still tracked). Exit 0 = went silent; exit 2 = (dir mode) nothing appeared.
set -euo pipefail

mtime(){ stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0; }  # BSD || GNU

if [ "${1:-}" = "--selftest" ]; then
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  out=/tmp/watch-agent-output.selftest.out

  # case 1: dir mode, nothing ever appears -> exit 2
  set +e; "$0" "$tmp/nothing" 2 4 1 >"$out" 2>&1; rc=$?; set -e
  [ "$rc" -eq 2 ] || { echo "FAIL: missing dir expected exit 2, got $rc"; cat "$out"; exit 1; }
  echo "PASS: missing log -> exit 2"

  # case 2: dir mode, a file appears then goes silent -> exit 0
  mkdir -p "$tmp/job"; ( sleep 1; echo hi > "$tmp/job/run.log" ) &
  set +e; "$0" "$tmp/job" 5 2 1 >"$out" 2>&1; rc=$?; set -e; wait
  [ "$rc" -eq 0 ] || { echo "FAIL: appears-then-silent expected 0, got $rc"; cat "$out"; exit 1; }
  echo "PASS: file appears then goes silent -> exit 0"

  # case 3: exact-file mode, a pre-existing STAGNANT file -> exit 0 (not false MISSING)
  pre="$tmp/pre.log"; echo "already here" > "$pre"
  set +e; "$0" "$pre" 2 1 >"$out" 2>&1; rc=$?; set -e
  [ "$rc" -eq 0 ] || { echo "FAIL: pre-existing stagnant file expected 0, got $rc"; cat "$out"; exit 1; }
  echo "PASS: pre-existing stagnant file -> exit 0"

  # case 4: exact-file mode, file actively written then stops -> exit 0
  f="$tmp/live.log"; : > "$f"; ( sleep 1; echo l1 >> "$f"; sleep 1; echo l2 >> "$f" ) &
  set +e; "$0" "$f" 3 1 >"$out" 2>&1; rc=$?; set -e; wait
  [ "$rc" -eq 0 ] || { echo "FAIL: exact-file active-then-silent expected 0, got $rc"; cat "$out"; exit 1; }
  echo "PASS: exact-file active then silent -> exit 0"
  exit 0
fi

# --- EXACT-FILE mode: arg 1 is an existing regular file ---
if [ -f "${1:-}" ]; then
  target=$1; silent_secs=${2:-120}; poll_secs=${3:-15}
  last=$(mtime "$target"); last_change=$(date +%s)
  while sleep "$poll_secs"; do
    [ -e "$target" ] || { echo "STUCK-OR-DONE: $target vanished"; exit 0; }
    m=$(mtime "$target"); now=$(date +%s)
    if [ "$m" != "$last" ]; then last=$m; last_change=$now; echo "active: $target"; continue; fi
    silent_for=$((now - last_change))
    if [ "$silent_for" -ge "$silent_secs" ]; then
      echo "STUCK-OR-DONE: $target silent ${silent_for}s (>= ${silent_secs}s) - go read its tail"; exit 0
    fi
    echo "quiet ${silent_for}s: $target"
  done
fi

# --- DIR mode (legacy): watch the newest *.log/*.jsonl created since launch ---
dir=${1:?usage: watch-agent-output.sh <log-file|dir> ...}
missing_secs=${2:-240}; silent_secs=${3:-120}; poll_secs=${4:-15}
start=$(date +%s); last_file=""; last_mtime=0; last_change=$start
while true; do
  now=$(date +%s); f=""; newest=0
  while IFS= read -r c; do
    [ -z "$c" ] && continue
    m=$(mtime "$c")
    if [ "$m" -ge "$start" ] && [ "$m" -gt "$newest" ]; then newest=$m; f=$c; fi
  done < <(find "$dir" -type f \( -name "*.log" -o -name "*.jsonl" \) 2>/dev/null)
  if [ -z "$f" ]; then
    if [ $((now - start)) -ge "$missing_secs" ]; then echo "MISSING: no log under $dir after ${missing_secs}s"; exit 2; fi
    echo "waiting for log under $dir ... ($((now - start))s)"; sleep "$poll_secs"; continue
  fi
  mtime=$(mtime "$f")
  if [ "$f" != "$last_file" ] || [ "$mtime" != "$last_mtime" ]; then
    last_file=$f; last_mtime=$mtime; last_change=$now; echo "active: $f"
  else
    silent_for=$((now - last_change))
    if [ "$silent_for" -ge "$silent_secs" ]; then
      echo "STUCK-OR-DONE: $f silent ${silent_for}s (>= ${silent_secs}s) - go read its tail"; exit 0
    fi
    echo "quiet ${silent_for}s: $f"
  fi
  sleep "$poll_secs"
done
