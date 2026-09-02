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

The top `RERUN_TOP` (default 2) friction sessions are analyzed **twice**, by two
independent map calls on the identical extract (`findings-<id>.md` and
`findings-<id>-run2.md`; the friction rank comes from `work/<date>/rank.txt`, written by
`extract`). reduce promotes a theme to a recommendation only if it appears in BOTH runs;
themes from one run only land in a `## Provisional (single-run)` section that mints no rec
id and gets no Next-action prompt. Measured 2026-08-26, three independent re-runs of one
real extract shared only ~51% of their themes and three themes appeared in exactly one
document of four - one of which had already become a permanent rule in the always-loaded
global instruction file. The two strongest findings reproduced unanimously; it is the tail
that does not, and the tail is what this filters. Cost is +2 map calls/day (plan quota).
A failed re-run degrades to single-run for that session rather than blocking the day.

reduce is **barred from emitting prose-tier recommendations at all** (`prompts/reduce.md`,
ENFORCEMENT TIER). Prose is not a weak mechanism, it is not a measurable one: a natural
experiment on the "never append `; echo rc=$?`" rule, which landed 2026-08-13, put the
violation rate at 31.11% over the 9 days before and 32.11% over the 14 days after, across
1831 opportunities. The friction is still reported; only a MECHANISM
(`tier: hook|script|test`, corpus-scored) or an explicit `no mechanism - not recommended`
line is allowed as the response.

Two themes are additionally closed outright, not merely down-tiered - putting the
worktree/containment constraints into the agent files, and widening the Stop guard for the
don't-stop-with-queued-work rule. Each consumed 22 days and ~9 rec ids; the first theme's own
latest entry concedes the substitution table is already inlined and subagents still hit it,
and the second's last four entries are successive widenings of one guard. The Stop-guard theme
is now closed on evidence rather than on repetition: labelled by outcome, agent-side premature
stopping is 39h of 1268h of human-wait (3.0%), because ~97% of that wait ends with a
substantive human message carrying new direction. Do not re-open either without refuting that.

reduce is also given the day's **repo & lander artifacts** (`scan_sessions.py
day-artifacts`): per repo the day's sessions worked in, the merge/non-merge commit split,
repeated identical commit subjects, and the `.claude/state/verify.log` verb histogram
(`gateloop-block` / `-pass` / `-capout` / `-tamper`, `land-error`, `land-conflict`). It
reports them under `## Repo & lander friction`. Every other input is a session TRANSCRIPT,
so the loop could only ever see friction that surfaced as an agent-visible event: measured
2026-08-26 against a ground truth built from these same two sources for one day, **merge
churn and every lander `land-error`/`land-conflict` row were named in 0 of 207
recommendations across all 30 reports**, while the classes that did surface in a transcript
were all caught. A merge that succeeds is silent; a `land-error` row is written by the
lander, not narrated.

Repos are resolved from each transcript's own `cwd` field (not by decoding the dashed
project-directory name, which is ambiguous for any path containing a dash), mapped to the
PRIMARY checkout via `git worktree list` because a worktree has its own gitignored
`.claude/state/`. git runs read-only with a FRESH env - no system or global config, no
inherited `GIT_*`, no pager, no credential prompt - and only in a directory that already
has a `.git`; a repo that is missing, locked or slow degrades this block rather than
failing the day. The analysis calls still get no tools.

The reduce step is additionally given the **last 21 days of recommendation titles**
(`scan_sessions.py prior-recs`, wrapper-computed from `recs.jsonl`, trusted-first zone) and
must emit a `Dedup:` line under every recommendation citing the closest prior id. Without
it reduce can only see yesterday's report plus the taken/chronic ids in the digest, so a
finding re-derived from fresh evidence days later collides with nothing and gets a fresh id
with `repeat: false`; measured 2026-08-26, **15.5% of the 207-rec corpus is exactly that**,
which is why the self-report (24.6%) sits well under an independent clustering pass (37.2%).
The corpus is supplied rather than a similarity SCORE deliberately: re-measured on that same
corpus, the nearest-title Jaccard has median 0.10 / p90 0.16 over 162 fresh-id recs while
known restatements sit at 0.18, so no threshold separates them - the report rewrites its own
titles daily, and the model is the only matcher that works at same-theme granularity.

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

**"How am I trending over time?"** - don't eyeball `metrics.jsonl` by hand or re-derive
this from daily report prose (each report only compares to the prior 1-2 days). Use:

    python3 ${CLAUDE_PLUGIN_ROOT}/skills/session-retro/scripts/scan_sessions.py trends --days 14

Prints a rate-normalized (`errors_per_hour`/`tool_calls_per_hour`, not raw counts - a
busy day and a quiet day are otherwise incomparable), coverage-aware table, a first-half-
vs-second-half trend split (skipped with a note if fewer than `MIN_TREND_SPLIT_DAYS`
usable days are in range), and an effectiveness-digest snapshot (holding /
recurred-after-fix / too-soon / CHRONIC counts) as of the most recent stamped report. A
day with `coverage_hours` below `MIN_TREND_COVERAGE_HOURS` (no sessions, or an all-retro-
stub day) is marked `low-coverage` and excluded from the split - never silently averaged
in as if it were a great quiet day. `tool_failures_per_hour` is the primary axis;
`errors_per_hour` is the pre-2026-08-26 undivided counter, printed for continuity with
old rows and never a friction axis. **An axis missing from part of the window is printed
`NOT COMPARABLE` with its coverage instead of being trended** - it changed definition
inside the window, so the direction would report WHEN the counter landed rather than how
the work went. Measured 2026-09-02 on 36 days of this machine's own history:
`tool_failures_per_hour` existed on 7 of 30 usable days while `trends` printed
`errors/hr (primary) ... (down)` across both the 08-17 wall-clock re-bucketing and the
08-26 error split. `friction_score` is printed too but is noisier (tracks how messy what was ATTEMPTED was,
not whether the process is improving) - a shrinking CHRONIC count over weeks is the
stronger "is this actually getting better" signal. Read-only: never writes
recs.jsonl/actions-log.md/metrics.jsonl.

## Same-day staleness watchdog

The daily report only surfaces the FOLLOWING morning - a session that dispatches a
background job whose completion notification never arrives (defef5a9-3b39-4cb4-ba79-
bda6611372cc, 2026-08-19/20: lost 19,478s, 65% of an 8.5h unattended overnight run, to
exactly this) just sits there until a human happens to check in. `scripts/watchdog.sh`
(-> `staleness_watchdog.py`) is a separate, independent, frequent (~20 min via launchd
`StartInterval`, not the daily `StartCalendarInterval`) sweep that catches this same-day:

- **Cheap first filter:** `stat`s `~/.claude/projects/*/*.jsonl` mtimes only - no content
  read - for files touched between `RETRO_WATCHDOG_STALE_SECS` (default 2700s/45min) and
  `RETRO_WATCHDOG_RECENT_SECS` (default 18h) ago. Most files never pass this filter.
- **Tail-parse only the stale candidates** (reuses `scan_sessions.py`'s
  `iter_lines`/`blocks`/`BG_NOTIFY_RE` - one parser, not two) to check whether the file's
  LAST row leaves the session "owed" a turn: either the last row is an assistant turn that
  dispatched a background job (an `Agent`, or a `run_in_background: true` Bash) whose id
  never got a `<task-notification>` anywhere in the file, or the last row IS a
  notification with no assistant reply after it. A plain trailing human message, or a
  dispatch that already got its notification and reply, is ordinary idle time - not
  flagged.
- **Dedup via `work/watchdog-state.json`:** one notification per stuck INSTANCE (keyed on
  the file's exact mtime), not per poll - a session still stuck 20 minutes later with no
  new writes is silently skipped.

**Known ceiling (ponytail, documented in `is_owed_turn`'s docstring):** from a static
transcript file alone, a genuinely-wedged LIVE session and an old session the user simply
stopped returning to look IDENTICAL - both end on an unresolved dispatch forever. Verified
live on 2026-08-20: 2 of 3 first-run flags were sessions from the evening before, already
covered by that day's retro report - false positives by this definition, not live wedges.
Dedup means each such session notifies ONCE ever, not repeatedly, which is the accepted
mitigation for v1. Upgrade path if the false-positive rate ever becomes a real problem:
correlate against live `claude`/`codex-companion` process state instead of file content
alone - not built here, no evidence yet that it's needed over the dedup mitigation.

Manual invocation: `python3 ${CLAUDE_PLUGIN_ROOT}/skills/session-retro/scripts/staleness_watchdog.py scan`
(prints newly-flagged sessions, updates the dedup state) or `bash
${CLAUDE_PLUGIN_ROOT}/skills/session-retro/scripts/watchdog.sh` (same, plus the macOS
notification). Wired to run automatically via
`~/Library/LaunchAgents/com.alementuev.claude-session-retro-watchdog.plist`.

## Closing step: the report IS the queue

After the report is written - and equally after reading an existing one - **emit one `TodoWrite`
item per open recommendation, id-tagged `rec:<date>#<n>`, then begin the first one in the same
turn.** Do not end the turn with an open rec; when one is genuinely blocked, name the user-only
input it needs and mark that todo blocked.

**A PRINTED prompt does not count as begun, and the "Next actions" section is not the deliverable.**
The `## Next actions` prompts exist so the work is already specified - they are for YOU to execute,
never a block for the user to paste. The generating turn must actually run the first one (edit the
file, land the commit), not restate it. This is the exact mechanism by which `rec:2026-08-14#7` has
stayed CHRONIC at `seen_count:6`: on 2026-08-20 the 08-19 report's three prompts had produced zero
ledger lines, and the generating session (`34d4cc42`) shows why - `subagents=0`, `bg_jobs=0`, nothing
dispatched. It repeated the next day: the 08-20 report printed **eleven** prompts and none had run 24
hours later. If a rec turns out to be wrong, that is a finding worth more than the rec - verify each
one's premise against the code before executing it rather than after (2026-08-21: `rec:2026-08-19#1`
asked to inline a table into two agent files that already contained it verbatim, and the real cause
was three command shapes the table never listed).

An unfinished todo list is visible to the harness at turn end in a way a paragraph of CLAUDE.md is
not, which is the whole point: "ending the turn with the next step already named" was a top-3
friction pattern on 2026-08-10, 08-11 and 08-12, and on 08-12 it cost 71 minutes in one session
(`47518583` at `17:08:57` ended with *"Five recs remain open: #2…, #3…"* and nothing running; the
resuming turn opened with *"Not done… Taking #2 and #7 now"*). The global rule forbidding it is the
first bullet of `~/.claude/CLAUDE.md` and was in context in every one of those sessions - prose is
not the lever.

A rec is open unless `session-reports/actions-log.md` carries a
`taken`/`rejected`/`deferred`/`applied` line
citing its exact id. Check before queueing, and append the outcome line when you finish one.

## Closing the loop (metrics + actions ledger)

- `session-reports/metrics.jsonl` - one wrapper-written JSON line per day (sessions,
  errors, interrupts, retries, top friction, plus `gate_calls` and `max_gate_wait_secs` -
  codex/agy review-gate volume and slowest single wait, and the wall-clock partition below);
  the reduce prompt reads the last 14 for trends. Upserted atomically by
  `scan_sessions.py metrics --date D` after a report completes; safe to backfill manually.
- **Analysis coverage (`sessions_analyzed` / `sessions_eligible` /
  `analysis_coverage_pct`).** Written by `extract` into `scan.json`'s `totals`, carried into
  `metrics.jsonl`, and stated by reduce in the Scoreboard. `--top 8` is fixed while the
  session count is not, so the share of a day an analyst ever reads FALLS as the day gets
  busier - and the busiest days are the ones with the most friction to find. Measured
  2026-09-02 over 36 days: **216 of 409 eligible sessions analyzed (52.8%), falling to 25.0%
  (08-04), 28.6% (08-26) and 36.4% (08-17) on the busiest days**, with nothing anywhere
  reporting it - the report said "8 sessions analyzed" and never "of 28". Divide by RAW
  session count instead and you get 38%, which is wrong in the other direction: retro stubs
  are not work. The denominator is the ELIGIBLE set (`eligible_sessions()`: retro stubs
  dropped, quiet sessions kept, since a quiet session could have taken a slot), so it is the
  same filter `pick_sessions` ranks - one definition, not two. This is an instrument, not a
  fix: it makes "is 8 the right cap" a question with data behind it instead of a constant
  nobody revisits.
- **Wall-clock partition (`work_secs` / `human_wait_secs` / `blocked_secs` /
  `model_latency_secs` / `idle_secs`).** Every inter-event gap lands in exactly one bucket, so
  the five sum to the session's span. **A gap is classified by the PAIR of events that bound
  it, never by the one that precedes it** (`rec:2026-08-15#7`, fixed 2026-08-17): `blocked` =
  the gap follows a tool/gate dispatch, or the agent ended its turn while a background job was
  still in flight; `human_wait` = the gap CLOSES on a user message, so only a human could
  restart it; `model_latency` = a pending turn answered within `MAX_GENERATION_SECS` (default
  1800, env `RETRO_MAX_GENERATION_SECS`); `idle` = a pending turn NOT answered within it, i.e.
  the machine sat on it; `work` = everything else (model thinking between a tool result and the
  next call). **Classifying by the preceding event alone was wrong in both directions and both
  halves are measured**: 2026-08-16's `78476725` booked one 38,833.3s pending turn as 100%
  `work` - 98.4% of that entire day's `work_secs`, on a session with zero tool calls - and now
  reads `idle=38833.3`; `67080bab` reported `human_wait=14.1s` for assistant→assistant
  generation and now reads `human_wait=0.0, model_latency=26.6`. Never read `idle` as work.
  Rows in `metrics.jsonl` written before 2026-08-17 use the old three-bucket rule and are not
  comparable across that boundary. The two waits are
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
- **Every scoreboard row and every claim about a prior session carries an evidence class**
  (`rec:2026-08-16#4`, `prompts/reduce.md`, 2026-08-17): `VERIFIED` (read cell-for-cell from
  the supplied `scan.json` / `metrics.jsonl` block), `ACCEPTED` (carried from an analyst or a
  prior report), `UNVERIFIED` (neither - never stated flat; write "unavailable" instead). The
  defect this closes is that a supplied figure and a recalled one were indistinguishable in
  the output: on 2026-08-16 a full scoreboard citing `scan.json` came out of a session with
  `tools: {}`. It is deliberately NOT a licence to add tool calls - the wrapper supplies the
  data on purpose. **Ceiling: this is a prompt rule with no wrapper-side check.** The
  structural validation in `run_retro.sh` gates on size + `## Next actions` only; a report
  that drops the classes still stamps `<!-- retro-complete -->`. Add a check there if it is
  ever observed to drift, rather than assuming the prose holds.
- **The retro's own headless calls never take an analysis slot** (`is_retro_stub`,
  `rec:2026-08-16#2`, 2026-08-17). A `-`-project session with no tools and <=1 user turn IS a
  map call from that morning's run; analysing it is a retro of a retro and can only report "no
  waste, one turn" (2026-08-16: nine of eleven sessions, chain two levels deep). The one
  exception is the daily REPORT-WRITER call, kept because QA-ing the report is what produced
  `rec:2026-08-16#4`; it is told apart by matching the retro's own reduce prompt - a
  first-party string, never transcript-derived text. **Do not re-implement this as a heading
  test**: `92572f74` is a map call over a *real* working session whose output opens
  "# Session retro: eldercare wake_min sentinel bar", and a heading test skips it wrongly.
  Measured on real days - 08-16: 8 extracts -> 3 (the reduce call survives); 08-15: 8 -> 7,
  three stubs dropped and a real session promoted into a freed slot; 08-13, a full working
  day: **unchanged at the same 8**, so the filter is inert when there is real work.
- **A `retro-analyst` subagent type is NOT the lever, and `--system-prompt` is not either.**
  Measured 2026-08-17: the per-session analysis is a `claude -p --tools ""` headless call, not
  an `Agent` dispatch, so a `subagent_type` has nothing to attach to. `--system-prompt` does
  not suppress `~/.claude/CLAUDE.md` (probed directly - both the default and the overridden
  call answer YES to "do your instructions contain 'Never use the em dash'") and saves only
  3,113 of 19,296 prompt tokens. Sequential map calls already cache-READ the prefix rather
  than rewriting it (measured with a no-flag control), so the "each isolated session rewrites
  its ~60K prefix" line below is bytes, not tokens, and does not reproduce as a per-call cost.
  Skipping whole calls is the lever that works.
- **Long messages in an extract are clipped from the MIDDLE, and the header says how much went**
  (`rec:2026-08-16#3`, fixed 2026-08-17). Every extract carries a
  `truncated= elided_chars= trajectory_capped= trajectory_elided_chars=` line. End-truncation
  silently ate the tail of
  every message over its cap, and a deliverable's conclusion lives in its tail: on 2026-08-16 six
  of nine analysts reported input cut mid-word and correctly refused to assess what they could
  not see. Regenerated against the fix, all six now end on a complete sentence (`a2854163` 3,816
  chars elided, `78476725` 8,185, `88d5288a` 8,267, `92572f74` 7,418, `a06cd63c` 5,276,
  `5a9c5769` 1,618). `elided_chars` covers only the per-message clipping - `trajectory_capped`
  is the separate whole-event case, counted by `trajectory_elided_chars`.
- **The extract body budget is `RETRO_MAX_EXTRACT_BYTES` (default 100,000) and it is
  what decides how much of a big session is ever read.** Measured 2026-09-02 over 129
  preserved extracts: **63% exceed it**, median uncapped 149KB, p90 327KB, max 538KB - so
  the middle-clipping below is not an edge case, it is the common path. It is NOT a context
  limit (538KB is ~134K tokens and fits one map call); it is a cost limit. Raising it buys
  whole sessions nearly linearly and the ceiling is cheap - **removing it entirely costs 2x
  extract bytes** (100K->1.00x/37% whole, 200K->1.62x/70%, 300K->1.88x/88%, 600K->1.99x/100%).
  **Trimming the per-message caps instead is measured DEAD**: 80% of the bytes are tool_use
  args and tool_result bodies, but most of those lines already sit under their 300-char cap,
  so cutting them to 80/150 buys 19-32% and the worst session still loses 73% of its
  trajectory - while TOOL_ERROR, the friction evidence, is 0.5% of the bytes. Do not rebuild
  that. The default stays at 100,000 because raising it spends plan quota every day, which is
  a decision for the reader; override it for one investigation with
  `RETRO_MAX_EXTRACT_BYTES=400000 ... extract --date D`. **Two things the override does not
  do on its own.** (1) `run_retro.sh` reuses any existing `findings-<id>.md` (its line
  `reusing findings for $id`), so re-extracting at a bigger cap and re-running the day
  analyses NOTHING new - delete the matching `findings-*.md` too, or the larger extract is
  never read. (2) It is one budget for all 8 extracts, so raising it to see one mega-session
  also inflates the ones already under the cap; the extract header now carries
  `extract_cap=` / `body_budget=` so an artifact says which budget produced it, since
  otherwise two extracts of the same session at different caps are indistinguishable.
  Set it too high and the map call overruns the model context, gets retried 3x and then
  DROPPED - so the session you raised the cap to see is the one missing from the report;
  the cause is in `work/<date>/map-err-<id>.log`.
- **The whole TRAJECTORY is clipped from the middle too, for the same reason** (fixed 2026-08-17,
  `trajectory_clip`). The extract loop used to `break` at the byte cap, which kept the session's
  HEAD - but a session's lands, cleanups and destructive commands live in its TAIL. Measured on
  one 2026-08-13 session (954 in-day events, 282,409 raw bytes against a 100,000 cap): under the
  old break-at-cap the extract stopped at event 391 and the `git worktree remove --force` that
  destroyed ~2h of uncommitted work at 20:51:51Z produced **0** matches, so no analyst could ever
  have reported it; under middle-clipping the same cap yields **2** matches, including the
  agent's own admission. That extract was guard #5 of the five that failed that day.
- **The gap timeline is built from `user`/`assistant` rows only** (`TIMELINE_TYPES`). Transcripts
  also carry `system` / `attachment` / `queue-operation` / `file-history-delta` rows that are not
  agent steps; letting them terminate a gap destroys the attribution the label exists for. Measured
  on `3f02f940`, 83% of gap-seconds - including that session's 7.25h stall - were reported as
  "after system", and a gate gap with a system row after the dispatch was dropped from
  `gate_wait_secs` entirely. Gates dispatched as background jobs are now charged from dispatch to
  completion notification (the same session read 8 min of gate wait against a ~1.6h hand count).
- **`subagents` counts the subagents that ran ON THE DAY, not the transcript files in the
  session's `subagents/` dir** (`rec:2026-08-16#1`, fixed 2026-08-17). It is sliced by the
  in-day event window exactly like `tools` / `errors` / `total_tokens`. File-counting carried a
  session's whole history into a day it never touched: 2026-08-16's `129c1cb6` reported
  `subagents=10` on a record with 2 events, zero turns and no tools. An emit-time assert now
  rejects any record with `subagents > 0` and no turns and no tools. Hand-check on the same
  change: `5b861411` read 11 on 2026-08-15 and now reads **3** - the report that proposed this
  fix predicted "unchanged" and was wrong; reading all 11 transcripts' timestamps shows only 3
  carry an 08-15 event, the other 8 are dated 08-13/08-14. Swept 2026-08-08..08-17: the assert
  fires on no real day.
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
- **`errors` is SPLIT as of 2026-08-26, and `tool_failures` is the honest axis.** The
  undivided counter summed four incompatible things: on 2026-08-25, of 47 in-day `is_error`
  blocks, **32 were genuine tool failures, 12 were policy blocks** (worktree containment
  refusals, stale-read protocol guards, hook guard refusals, classifier denials) **and 3 were
  harness outages** (model timeout, the 2-minute Bash ceiling's exit 143). All 15 non-failures
  were read individually and confirmed. A fall in `errors_per_hour` is therefore equally
  consistent with "the code broke less", "the guardrails were relaxed" and "the API had a
  better day" - it is retained for continuity with pre-2026-08-26 rows but **must not be read
  as friction**. `tool_failures_per_hour` is the axis. `classify_error`'s fallback is
  `tool_failure` on purpose: a guard shape nobody has listed yet shows up as over-counted
  friction (visible) instead of being absorbed into `policy_block` (invisible).
  `friction()` now weights `tool_failures` and no longer adds `denials` on top of `errors`,
  of which it was a strict subset - a permission denial used to score 4 against a real test
  failure's 3.
- **`user_turns` HAND-CHECK, and it now passes.** Session `3ece6495` (2026-08-25): the counter
  read **164**; an independent hand count of genuine human messages found **19**; after the
  fix it reads **19**. Day-wide the same day goes 421 -> 148 user turns with 438 reclassified
  as `injected_turns`. The discriminator is the transcript's own **`origin.kind`** field
  ("human" vs "task-notification", plus `isMeta` for slash-command echoes) - not a text
  heuristic. Two things made the earlier regex attempt score 1 of 133: injected rows carry
  `message.content` as a plain **str**, so `blocks()` returns `[]` and any block-list-only
  test sees nothing; and the tag vocabulary is open-ended while `origin.kind` is closed.
  **This matters beyond the counter**: `user_turns` is `friction_score`'s DENOMINATOR, so
  inflating it deflated friction on exactly the heavily-delegating sessions, and
  `pick_sessions` ranks by that score. Re-ranking 2026-08-25 with the corrected score changes
  **3 of the 8 sessions** that would get an analyst call.
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
  `- [YYYY-MM-DD] taken|rejected|deferred|applied rec:<report-date>#<n> - <summary> (<reason>)`.
  When you act on (or reject) a report recommendation - including manually - append an
  outcome line citing its `rec:` id. Reports suppress only on exact id match to a
  `rejected` or `applied` line and outcome-check `taken` ones; non-conforming lines are
  ignored. `applied` = written but effect unmeasured: done, so it stops resurfacing, but it
  is not `taken` and never enters fix-effectiveness.

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
