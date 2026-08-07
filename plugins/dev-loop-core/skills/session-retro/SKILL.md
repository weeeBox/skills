---
name: session-retro
description: Produce the daily Claude Code session-retro report (friction analysis of yesterday's sessions across all projects). Use when asked to run the session retro, produce/regenerate a daily session report, or when invoked as /session-retro.
---

# session-retro

Analyzes all Claude Code session transcripts for a day and writes a friction report with
concrete improvement recommendations to
`~/.claude/session-reports/<date>.md`.

The reduce step is also fed a trusted snapshot of the user's global config
(`~/.claude/CLAUDE.md`, `AGENTS.md` if present, `settings.json`) so it can DEDUP
recommendations against rules that already exist and flag stale/ineffective ones today's
sessions implicate (report section "Global rules & settings health"). The config is passed
as DATA TO REVIEW in the trusted-first zone - it never gets tools and its imperatives are
never obeyed (`prompts/reduce.md` enforces this).

It also tracks recommendation recurrence across days (`recs.jsonl`) and reports, per TAKEN
recommendation, whether the friction it targeted actually stopped ("Fix effectiveness &
chronic friction"), so a fix that is not working surfaces instead of silently repeating.

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
  errors, interrupts, retries, top friction, plus `gate_calls` and `max_gate_wait_secs` -
  codex/agy review-gate volume and slowest single wait, and the wall-clock partition below);
  the reduce prompt reads the last 14 for trends. Upserted atomically by
  `scan_sessions.py metrics --date D` after a report completes; safe to backfill manually.
- **Wall-clock partition (`work_secs` / `human_wait_secs` / `blocked_secs`).** Every
  inter-event gap lands in exactly one bucket, so the three sum to the session's span:
  `blocked` = the gap follows a tool/gate dispatch, or the agent ended its turn while a
  background job was still in flight; `human_wait` = the agent ended its turn with NOTHING
  running, so only a human could restart it; `work` = everything else. The two waits are
  indistinguishable without the in-flight set - which is why they used to be one undifferentiated
  "NEUTRAL wall-clock" bucket that no finding could name. Background jobs are correlated exactly:
  a completion `<task-notification>` carries its dispatching tool_use's id (`BG_NOTIFY_RE`), which
  also yields `bg_jobs` / `bg_job_secs` / `bg_blocked_secs` and a **parallelism ratio** (job time
  / wall-clock blocked); ~1.0x with several jobs means they ran strictly one after another.
  Validated against an independent hand-analysis of session `3f02f940` (2026-08-07): that
  session's 1.09x hand-computed parallelism reads 1.07x here, and its ~64/27/8 human/blocked/work
  split reads 54/35/10 over a wider window. Day-level sums in metrics.jsonl are session-hours
  across overlapping sessions, NOT clock-hours - compare the buckets to each other, not to the day.
  Jobs still in flight when the day window closes are uncounted, so `bg_*` is a lower bound.
- **The gap timeline is built from `user`/`assistant` rows only** (`TIMELINE_TYPES`). Transcripts
  also carry `system` / `attachment` / `queue-operation` / `file-history-delta` rows that are not
  agent steps; letting them terminate a gap destroys the attribution the label exists for. Measured
  on `3f02f940`, 83% of gap-seconds - including that session's 7.25h stall - were reported as
  "after system", and a gate gap with a system row after the dispatch was dropped from
  `gate_wait_secs` entirely. Gates dispatched as background jobs are now charged from dispatch to
  completion notification (the same session read 8 min of gate wait against a ~1.6h hand count).
- **`self_retractions` is a LOWER BOUND, not yet a trend axis.** It counts the AGENT
  retracting its own prior claim, on assistant turns (`RETRACTION_RE`) - distinct from
  `corrections`, which scans USER turns and measures the user correcting the agent. It is
  reported in the scan table (`retr`) and the extract header, and is deliberately NOT part
  of `friction_score`.
  **Acceptance gate before anyone trends it or gates on it:** hand-label every assistant
  turn in one mid-sized session (~100 turns) as retraction / not, then compute precision
  and recall of `RETRACTION_RE` against those labels. Require **precision >= 0.95** and
  **recall >= 0.70**. Below 0.70 recall the count moves with phrasing rather than
  behaviour and must stay a diagnostic aid only. Re-run this check before ever widening
  the regex - a bounded-gap variant was already rejected in 2026-08-04 testing for
  false-matching "My test covers the case where the input was wrong on purpose".
  **Status after the 2026-08-06 widening: precision PASSES, recall does NOT yet.**
  Pre-widening it matched 5 of an independently estimated 33-38 true retractions on
  2026-08-05 (~13% recall), and four of that day's analysts each flagged the counter as a
  false floor. Post-widening it matches 24, all hand-labelled: 23-24 true, so
  **precision 0.958-1.00**. Recall is ~0.63-0.73 against an ESTIMATED denominator, which
  straddles the bar and was not produced by the hand-labelled session this gate specifies,
  so the recall half stays OPEN and the count stays a diagnostic aid, not a trend axis.
  Running this gate is what caught the widening's own defect: the first attempt scored 0.92
  because a bare `correction to` matched "every correction to one has to be checked against
  the other" - prose ABOUT corrections. Do not widen from a pattern list alone; probe each
  candidate clause against real assistant text and read its hits before adding it.
- **Slow gate detection**: `scan_sessions.py` tags codex/agy review-gate activity
  (`GATE_RE`) in each session and reports `gate_calls` / `gate_wait_secs` /
  `max_gate_wait_secs` in the extract header. A gate that blocks for many minutes, or many
  re-gate rounds on one session, is the one wall-clock cost the map/reduce prompts treat as
  nameable (not neutral async wait) - so a slow/thrashing codex or agy gate surfaces as a
  finding instead of hiding inside `largest_gaps`.
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
