#!/bin/bash
# Daily session-retro runner. All deterministic work (catch-up, scan, extract, metrics)
# happens here. The model is invoked map-reduce style over stdin prompts with a CLOSED
# empty toolset (--tools ""), so transcript-derived content can neither execute
# anything nor read outside its input.
set -u

BASE="$HOME/.claude/session-reports"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL="${SKILL:-$(cd "$SCRIPT_DIR/.." && pwd)}"
CLAUDE="${CLAUDE:-$(command -v claude || echo "$HOME/.brew/bin/claude")}"
PY="${PY:-/usr/bin/python3}"   # overridable so failure paths can be tested cheaply
LOCK="$BASE/.lock"
LOG="$BASE/logs/run-$(date +%F).log"
# Denylist is defense-in-depth only: --tools "" (closed switch) is the boundary and
# --max-turns 1 kills the agent loop even if a flag ever regresses.
NO_TOOLS="Bash,Read,Glob,Grep,Task,Agent,Write,Edit,MultiEdit,NotebookEdit,WebFetch,WebSearch,TodoWrite,Skill,KillShell,BashOutput,Workflow,Monitor,ToolSearch,CronCreate,CronDelete,CronList,TaskCreate,TaskUpdate,TaskGet,TaskList,TaskOutput,TaskStop,PushNotification,RemoteTrigger,SendMessage,EnterWorktree,ExitWorktree,Artifact,ScheduleWakeup,DesignSync,Ls,MemoryRead,MemoryWrite,Config,ComputerUse,ReportFindings,StructuredOutput"

fail_reasons=""   # non-empty = suspicious/failed run; short tokens, notified verbatim
completed=""

mkdir -p "$BASE/logs" "$BASE/work"

notify() {  # best-effort AND detached: a hung osascript (headless permission prompt)
            # must never block the run while it holds the lock
  ( /usr/bin/osascript -e "display notification \"$1\" with title \"session-retro\"" \
      >>"$LOG" 2>&1 || true ) &
}

finish() {  # exactly one notification per run; every exit path funnels through here
  if [ -n "$fail_reasons" ]; then
    notify "attention:$fail_reasons (see logs/run-$(date +%F).log)"
  elif [ -n "$completed" ]; then
    notify "report ready:$completed"
  fi
  rmdir "$LOCK" 2>/dev/null
}

# lock so kickstart + scheduled runs can't overlap; stale (>2h) locks are removed via
# atomic mv-aside so two racers can't both clear-and-take it. Clearing one is
# suspicious (a run died or hung) and always notifies, even if today succeeds.
if [ -d "$LOCK" ]; then
  age=$(( $(date +%s) - $(stat -f %m "$LOCK") ))
  if [ "$age" -gt 7200 ]; then
    if mv "$LOCK" "$LOCK.stale.$$" 2>/dev/null; then
      rmdir "$LOCK.stale.$$" 2>/dev/null
      fail_reasons="$fail_reasons stale-lock-cleared"
    fi
  fi
fi
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date '+%F %T') another run holds the lock, exiting" >> "$LOG"
  # trap isn't installed yet: if we cleared a stale lock and then lost the race to
  # re-acquire, the suspicious event must still notify before this early exit
  [ -n "$fail_reasons" ] && notify "attention:$fail_reasons (see logs/run-$(date +%F).log)"
  exit 0
fi
trap finish EXIT

exec >> "$LOG" 2>&1
echo "=== session-retro run $(date '+%F %T') ==="

run_model() {  # stdin: prompt; stdout: model text
  # --tools "": the CLOSED switch - empty built-in toolset (the boundary)
  # --strict-mcp-config with no --mcp-config: zero MCP servers
  # denylist + --max-turns 1: defense-in-depth if either flag ever regresses
  "$CLAUDE" -p --tools "" --disallowedTools "$NO_TOOLS" --strict-mcp-config --max-turns 1
}

if ! dates=$("$PY" "$SKILL/scripts/scan_sessions.py" missing-dates); then
  echo "missing-dates FAILED, aborting"
  fail_reasons="$fail_reasons missing-dates"
  exit 1
fi
if [ -z "$dates" ]; then
  echo "no uncovered dates, nothing to do"
  exit 0
fi

for d in $dates; do
  echo "--- processing $d ---"
  touch "$LOCK"  # keep a legitimately long run from ever looking stale
  work="$BASE/work/$d"
  "$PY" "$SKILL/scripts/scan_sessions.py" scan --date "$d" || { echo "scan FAILED for $d"; fail_reasons="$fail_reasons scan:$d"; continue; }
  if [ "$("$PY" -c "import json,sys;print(len(json.load(open(sys.argv[1]))['sessions']))" "$work/scan.json")" = "0" ]; then
    printf '# Session retro %s\n\nNo sessions recorded on this date.\n\n<!-- retro-complete -->\n' "$d" > "$BASE/$d.md"
    "$PY" "$SKILL/scripts/scan_sessions.py" metrics --date "$d" || { echo "metrics FAILED for $d"; fail_reasons="$fail_reasons metrics:$d"; }
    completed="$completed $d"
    echo "no sessions on $d, stub report written"
    continue
  fi
  "$PY" "$SKILL/scripts/scan_sessions.py" extract --date "$d" --top 8 || { echo "extract FAILED for $d"; fail_reasons="$fail_reasons extract:$d"; continue; }

  # map: one analyst call per extract (sequential; bounded inputs). The claude -p call
  # fails intermittently (empty output), so retry each 3x. Findings use stable
  # content-hashed ids, so REUSE any already produced - a re-run only re-attempts what's
  # missing instead of re-rolling all 8. A single extract that keeps failing (e.g. a
  # mega-session that overruns the context) is dropped+logged so it degrades the report
  # rather than blocking the whole day forever.
  dropped=""
  for e in "$work"/extract-*.md; do
    [ -e "$e" ] || continue
    id=$(basename "$e" .md)
    if [ -s "$work/findings-$id.md" ]; then echo "reusing findings for $id"; continue; fi
    echo "analyzing $id"
    touch "$LOCK"
    ok=""
    for try in 1 2 3; do
      if cat "$SKILL/prompts/map.md" "$e" | run_model > "$work/findings-$id.md" \
           2>>"$work/map-err-$id.log" \
         && [ -s "$work/findings-$id.md" ]; then
        ok=y; break
      fi
      echo "map attempt $try failed for $id (stderr -> $work/map-err-$id.log)"
      rm -f "$work/findings-$id.md"
    done
    [ -n "$ok" ] || { echo "map FAILED for $id (dropped after retries)"; dropped="$dropped $id"; }
  done
  # If extracts existed but EVERY map failed, the run is bad (transient) - leave the date
  # uncovered to retry. Otherwise proceed: 0 extracts => scan-only no-op report; partial
  # => degraded report (dropped sessions logged, not silently omitted).
  n_find=0; for f in "$work"/findings-*.md; do [ -s "$f" ] && n_find=$((n_find+1)); done
  n_extr=0; for e in "$work"/extract-*.md; do [ -e "$e" ] && n_extr=$((n_extr+1)); done
  if [ "$n_extr" -gt 0 ] && [ "$n_find" -eq 0 ]; then
    echo "all $n_extr map calls failed for $d; leaving date uncovered for retry"
    echo "  captured map stderr: $work/map-err-*.log (known-transient empty-output flakiness; just re-run, do NOT manually repro a map call)"
    fail_reasons="$fail_reasons map:$d"
    continue
  fi
  [ -n "$dropped" ] && echo "NOTE: proceeding for $d without dropped extracts:$dropped"

  # reduce: synthesize the report from scan stats + findings + previous report,
  # plus the wrapper-written metrics history and the schema-parsed actions ledger
  tmp="$work/report.tmp"
  {
    cat "$SKILL/prompts/reduce.md"
    echo "REPORT DATE: $d"
    # trusted wrapper-written sections FIRST, before any transcript-derived content
    # can forge a look-alike section (reduce.md: first-occurrence-wins)
    if [ -s "$BASE/metrics.jsonl" ]; then
      echo "--- metrics history (last 14 days, wrapper-written) ---"
      tail -14 "$BASE/metrics.jsonl"
    fi
    ledger=$("$PY" "$SKILL/scripts/scan_sessions.py" ledger --date "$d")
    if [ -n "$ledger" ]; then
      echo "--- actions log (wrapper-filtered: valid, temporally-plausible lines only) ---"
      echo "$ledger"
    fi
    echo "--- scan.json ---"
    cat "$work/scan.json"
    for f in "$work"/findings-*.md; do
      [ -e "$f" ] || continue
      echo "--- $(basename "$f") ---"
      cat "$f"
    done
    if [ -e "$work/previous-report.md" ]; then
      echo "--- previous report ---"
      cat "$work/previous-report.md"
    fi
  } > "$work/reduce-input.txt"
  # reduce also fails intermittently (empty output) - retry 3x before giving up.
  rok=""
  for try in 1 2 3; do
    if run_model < "$work/reduce-input.txt" > "$tmp" && [ -s "$tmp" ]; then rok=y; break; fi
    echo "reduce attempt $try failed for $d"; rm -f "$tmp"
  done
  if [ -z "$rok" ]; then
    echo "reduce FAILED for $d; leaving date uncovered for retry"
    fail_reasons="$fail_reasons reduce:$d"
    continue
  fi

  # completion marker stamped by THIS wrapper only after structural validation:
  # size + the mandatory final section (catches truncated model output)
  if [ -s "$tmp" ] && [ "$(stat -f %z "$tmp")" -gt 500 ] && grep -q "## Next actions" "$tmp"; then
    mv "$tmp" "$BASE/$d.md"
    printf '\n<!-- retro-complete -->\n' >> "$BASE/$d.md"
    echo "report complete: $BASE/$d.md"
    # metrics AFTER stamping (deliberate: today's stats reach reduce via scan.json;
    # metrics.jsonl exists for prior-day trends). Failure notifies but never unstamps.
    "$PY" "$SKILL/scripts/scan_sessions.py" metrics --date "$d" || { echo "metrics FAILED for $d"; fail_reasons="$fail_reasons metrics:$d"; }
    completed="$completed $d"
  else
    echo "report FAILED validation for $d (missing/too small/truncated); left uncovered for retry"
    rm -f "$tmp"
    fail_reasons="$fail_reasons validate:$d"
  fi
done
echo "=== done $(date '+%F %T') ==="
