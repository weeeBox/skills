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

## Closing step: the report IS the queue

After the report is written - and equally after reading an existing one - **emit one `TodoWrite`
item per open recommendation, id-tagged `rec:<date>#<n>`, then begin the first one in the same
turn.** Do not end the turn with an open rec; when one is genuinely blocked, name the user-only
input it needs and mark that todo blocked.

An unfinished todo list is visible to the harness at turn end in a way a paragraph of CLAUDE.md is
not, which is the whole point: "ending the turn with the next step already named" was a top-3
friction pattern on 2026-08-10, 08-11 and 08-12, and on 08-12 it cost 71 minutes in one session
(`47518583` at `17:08:57` ended with *"Five recs remain open: #2…, #3…"* and nothing running; the
resuming turn opened with *"Not done… Taking #2 and #7 now"*). The global rule forbidding it is the
first bullet of `~/.claude/CLAUDE.md` and was in context in every one of those sessions - prose is
not the lever.

A rec is open unless `session-reports/actions-log.md` carries a `taken`/`rejected`/`deferred` line
citing its exact id. Check before queueing, and append the outcome line when you finish one.

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
- **Calibrate a counter against ONE hand-measured session before any report leans on it.**
  This tool's whole job is measurement, so an uncalibrated counter is not a rough number - it
  is an unfalsified claim that a report will state as fact. The precedent that works is
  `self_retractions` below: it has a written acceptance gate, and running that gate is what
  caught a defect in its own regex widening. The counter-precedent is everything else. Gap
  attribution and `gate_wait_secs` shipped uncalibrated for weeks and were wrong by 83% and
  ~12x respectively, discovered only on 2026-08-07 when an outside analysis measured the same
  session (`3f02f940`) by hand and disagreed with the report. Neither error needed new data to
  find - only someone counting the same thing twice.
  **When you add or widen a counter:** pick one real session, measure the same quantity by
  hand, record BOTH numbers and the session id in this file (as the `bg_*` and `RETRACTION_RE`
  bullets do). If the two disagree, the counter is wrong until shown otherwise - a plausible
  implementation is not evidence. A counter with no recorded hand-check is a diagnostic aid,
  never a trend axis and never the basis of a recommendation.
- **`self_retractions` PASSES its acceptance gate as of 2026-08-10 and is now a trend
  axis.** It counts the AGENT
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
  **Status 2026-08-10: BOTH halves PASS, on the first properly-executed run of this gate.**
  Session `a59acf3f` (2026-08-09), all **128** assistant text blocks hand-labelled in full:
  **precision 0.952** (conservative reading; 1.000 if the one hand-labelled borderline
  counts as correct), **recall 0.952** (20 of 21). Held-out session `8d7a8a76` (297 blocks,
  never label-fitted): 8 hits -> 16, all 16 read and all genuine. Before this change the
  same regex measured **recall 0.091 / precision 0.500** on that labelled set - the
  2026-08-06 status line above was written against 2026-08-05 text and an ESTIMATED
  denominator, and it overstated both halves.
  Two defects only the labelled run could find, both now pinned by asserts:
  (1) the bare `(is|was) now stale` clause matched *"the verdict record ... is now stale"* -
  a fact about an artifact, and the single false positive that put precision at 0.500;
  (2) the new quote-stripper's global `re.S` made its `>` branch swallow every line after
  the first blockquote, silently deleting a real retraction until it was measured.
  **The labelling method is part of the gate.** A first pass that labelled from 300-char
  previews found 11 retractions; re-reading all 128 blocks IN FULL found 21. Ten sat past
  the truncation point (`[052]`'s retraction is its final paragraph). Never label from a
  preview - the resulting recall denominator is wrong in the flattering direction.
  Running this gate is also what caught the 2026-08-06 widening's own defect: that attempt
  scored 0.92 because a bare `correction to` matched "every correction to one has to be
  checked against the other" - prose ABOUT corrections. Do not widen from a pattern list
  alone; probe each candidate clause against real assistant text and read its hits before
  adding it. Both narrowings above were found this way, not by inspection.
- **`corrections=0` is usually CORRECT, and reports keep misreading it as a broken
  counter.** `CORRECTION_RE` scans USER turns for the user correcting the agent.
  `self_retractions` scans ASSISTANT turns for the agent retracting itself. They are
  different measurements, and a report that cites first-person agent quotes
  (*"my claim was an unverified assertion"*) as evidence that `corrections` is
  under-counting has made a category error. Verified 2026-08-10 on 2026-08-09's
  `a59acf3f`: of its 36 counted user turns, every one is a genuine non-corrective request
  (*"what is the current state"*, *"take the next recommended step"*, *"clear all"*) or a
  harness-injected `<task-notification>` / skill preamble. **There was no user correction
  to find.** `rec:2026-08-03#13` carried this conflation for six days. Before calling
  `corrections` broken, dump the session's real user turns and point at one the regex
  missed. (Related, still open: `user_turns` counts those harness-injected blocks as user
  turns, so it overstates human involvement - `a59acf3f` reads 36 against 6 real ones.)
- **`still_active` names only sessions that HAVE a record in `sessions[]`** (invariant
  asserted in the selftest since 2026-08-10). Still-active files have always been scanned;
  a record is omitted only when the file has ZERO events inside the day window - i.e. the
  session belongs to a later day. Those are now counted in `active_no_in_day_events`
  rather than named, because naming them implied their figures were missing from the day's
  totals, and a reader acted on exactly that: the 2026-08-09 report opened with a caveat
  about a session "absent from every figure above" which in fact had 889 events, all dated
  2026-08-10. Do **not** "fix" this by emitting a partial all-nulls record for such a
  session (`rec:2026-08-09#7`'s proposed artifact, rejected 2026-08-10 after checking the
  transcript) - that fabricates a row for a session with no data on the date, and the
  report then has to explain a row of nulls.
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
