---
name: session-retro
description: Produce the daily Claude Code session-retro report (friction analysis of yesterday's sessions across all projects). Use when asked to run the session retro, produce/regenerate a daily session report, or when invoked as /session-retro.
---

# session-retro

Analyzes all Claude Code session transcripts for a day and writes a friction report with
concrete improvement recommendations to
`~/.claude/session-reports/<date>.md`.

Run it on demand (see **Manual invocation** below), or wire it to run every morning via a
scheduler (macOS `launchd` / Linux `cron`) that calls `scripts/run_retro.sh` - see the repo
README for an example LaunchAgent/cron entry. Everything is
deterministic shell/python plus tool-less `claude -p` map-reduce calls (`--tools ""` +
`--strict-mcp-config`; denylist and `--max-turns 1` as defense-in-depth) - by design the
model never gets ANY tools while processing transcript-derived (untrusted) content. Do
not "improve" this by giving the synthesis calls tools; that reopens the
prompt-injection boundary (codex-gated at plan AND diff stage, 3 rounds each,
2026-07-09).

## Manual invocation

Run the whole pipeline for all uncovered dates (same thing launchd does):

    bash ${CLAUDE_PLUGIN_ROOT}/skills/session-retro/scripts/run_retro.sh

One specific date by hand (stage, then inspect `work/<date>/` before synthesis):

    python3 ${CLAUDE_PLUGIN_ROOT}/skills/session-retro/scripts/scan_sessions.py scan --date YYYY-MM-DD
    python3 ${CLAUDE_PLUGIN_ROOT}/skills/session-retro/scripts/scan_sessions.py extract --date YYYY-MM-DD --top 8

Then either run `run_retro.sh` (it picks up any date whose report lacks the
`<!-- retro-complete -->` marker) or analyze the staged extracts yourself following
`prompts/map.md` + `prompts/reduce.md` - those two files are the single source of truth
for the analysis brief and the report contract (evidence + concrete artifact required
for every recommendation).

To force a date to re-run: delete `session-reports/<date>.md` (or its completion
marker) and run the runner again.

## Closing the loop (metrics + actions ledger)

- `session-reports/metrics.jsonl` - one wrapper-written JSON line per day (sessions,
  errors, interrupts, retries, top friction); the reduce prompt reads the last 14 for
  trends. Upserted atomically by `scan_sessions.py metrics --date D` after a report
  completes; safe to backfill manually.
- `session-reports/actions-log.md` - recommendation-outcome ledger, STRICT schema:
  `- [YYYY-MM-DD] taken|rejected|deferred rec:<report-date>#<n> - <summary> (<reason>)`.
  When you act on (or reject) a report recommendation - including manually - append an
  outcome line citing its `rec:` id. Reports suppress only on exact id match to a
  `rejected` line and outcome-check `taken` ones; non-conforming lines are ignored.

## Cost

Measured 2026-07-10 (busy 41-session day, 8 map + 1 reduce calls, all `claude-opus-4-8`):
~680K tokens/run - 553K cache writes (each isolated session rewrites its ~60K prompt
prefix; inherent to the tool-less security design), 73K output, 22K uncached input.
API-equivalent ~$5.40/run (~$160/mo) at Opus 4.8 rates; that day was near the ceiling
(N<=8 maps caps it), quiet days cost ~nothing. **Actual marginal cost is $0**: the CLI
runs on the Max-plan OAuth login (no API key), so runs consume plan quota, not dollars -
and at 8:07am, before interactive usage typically starts.

- Re-measure: sum `message.usage` over the run's headless transcripts (that morning's
  `~/.claude/projects/-Users-user/*.jsonl`, mtime in the run window).
- If quota pressure ever appears: run the map calls on `claude-haiku-4-5` via `--model`
  in `run_model()` (reduce stays default) - roughly 3-4x cheaper, some analyst-quality
  tradeoff. Not worth it at $0 marginal cost.

## Logs / troubleshooting

- Each run posts ONE macOS notification: FAILED (see the dated log), "report ready",
  or "stale lock cleared"; plain no-op runs are silent. Notifications are best-effort
  and detached - a hung osascript never blocks the run.
- Per-run logs: `session-reports/logs/run-YYYY-MM-DD.log`
- **"all N map calls failed -> date uncovered" is known-transient** empty-output flakiness in the
  tool-less `claude -p` map calls. Do NOT grep the run log for the cause (the runner used to swallow it;
  it now writes each failed attempt's stderr to `work/<date>/map-err-<id>.log`) and do NOT manually
  repro a map call. Just re-run: confirm no stale `*.lock`, then `bash run_retro.sh`.
- If wired to a scheduler, its stdout/err (pre-log failures) go wherever you point the
  LaunchAgent/cron job (e.g. `session-reports/logs/launchd.{out,err}`).
- Kick a scheduled run manually with your scheduler (macOS: `launchctl kickstart -k
  gui/$(id -u)/<your-launchd-label>`).
