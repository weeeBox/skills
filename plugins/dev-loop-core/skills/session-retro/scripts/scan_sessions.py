#!/usr/bin/env python3
"""Deterministic pre-scan for the session-retro skill.

Commands:
  scan --date YYYY-MM-DD      stats for all sessions with events on that day
                              (writes work/<date>/scan.json + table to stdout)
  extract --date D --top N    pruned evidence extracts for top-N friction sessions
                              (writes work/<date>/<session-id>.md + previous-report.md)
  day-artifacts --date D      the day's repo/lander artifacts (commit split, repeated
                              subjects, gate-log histogram) for the repos the day's
                              sessions worked in - friction no transcript records
  prior-recs --date D         the last 21 days of recommendation titles (dedup corpus
                              supplied to the reduce prompt; TRUSTED, wrapper-computed)
  memory-health               memory files at/near the loader's silent-truncation limits
                              (25000 chars / 200 lines / ~200 chars per index entry);
                              cross-project, silent when healthy, CURRENT state not --date
  missing-dates               dates since last complete report, up to yesterday
  trends [--days N]           rate-normalized quality trend over the last N days of
                              metrics.jsonl (default 14) + an effectiveness-digest snapshot
  selftest                    run built-in assertions on a synthetic transcript

Env: RETRO_MAX_ANALYSIS_SESSIONS (default 8) - how many sessions get an analyst call.
     53.3% of eligible sessions at 8; 87.0% at 16 for 1.50x the map calls. `--top N` only
     lowers it. Each extra session is a whole extra model call, i.e. real daily quota.
     RETRO_MAX_EXTRACT_BYTES (default 100000) - per-session extract body budget. 63% of
     extracts exceed the default and lose the middle of the trajectory; raising it to
     300000 shows 88% of sessions whole for 1.88x extract bytes. Re-running `extract`
     alone changes NOTHING about a report - run_retro.sh reuses any existing
     findings-<id>.md; delete those too, or the bigger extract is never analysed.
     RETRO_MAX_GENERATION_SECS (default 1800) - pending-turn gap above which a gap is
     `idle` rather than `model_latency`.

Stdlib only. Transcript content is untrusted data; this script only counts and
truncates it, never executes it.
"""
import collections
import json
import os
import re
import sys
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
REPORTS = Path.home() / ".claude" / "session-reports"
COMPLETE_MARKER = "<!-- retro-complete -->"
ACTIVE_GRACE_SECS = 15 * 60  # sessions written to in the last 15 min are deferred
# Body budget for one session extract. NOT a context limit - the largest extract
# measured is 537KB (~134K tokens) and fits in one map call - it is an undocumented COST
# limit, and it is what makes trajectory_clip drop the middle of a session.
#
# Measured 2026-09-02 over 129 preserved extracts: 63% exceed this, median uncapped size
# 149KB, p90 327KB, max 538KB. Raising it buys whole sessions nearly linearly, and the
# ceiling is cheap - removing it entirely costs 2x extract bytes total:
#
#       cap        total input   extracts seen WHOLE
#   100,000 (now)    1.00x         48 of 129 (37%)
#   200,000          1.62x         90 of 129 (70%)
#   300,000          1.88x        113 of 129 (88%)
#   600,000          1.99x        129 of 129 (100%)
#
# Trimming the per-message caps instead does NOT work and was measured before this was
# written: 80% of an extract's bytes are tool_use args + tool_result bodies, but most of
# those lines are already under their 300-char cap, so cutting them to 80/150 buys only
# 19-32% and the worst session still loses 73% of its trajectory. TOOL_ERROR - the
# friction evidence itself - is 0.5% of the bytes. Raising the budget is the only lever
# that moves this.
#
# The default is left at 100_000: raising it spends plan quota daily, which is the
# reader's call, not this file's. Override per-run for one investigation:
#   RETRO_MAX_EXTRACT_BYTES=400000 python3 scan_sessions.py extract --date D --top 8
# A single extract that overruns the model context is already dropped and logged by
# run_retro.sh after its retries, so a too-large override degrades that session rather
# than failing the day.
MAX_EXTRACT_BYTES = int(os.environ.get("RETRO_MAX_EXTRACT_BYTES", 100_000))
# Longest a single model generation is allowed to be before the gap is called `idle` instead.
# A user->assistant gap of 30s is generation latency; the same gap at 10h47m is the machine
# sitting on a pending turn (2026-08-16 session 78476725, which alone was 98.4% of that day's
# reported work_secs).
# How many sessions get an analyst (map) call. The BREADTH budget, as
# MAX_EXTRACT_BYTES is the depth one - and the binding constraint of the two: the cap is
# fixed while the session count is not, so coverage FALLS as a day gets busier.
#
# Measured 2026-09-02 over 30 scanned days, 409 eligible sessions (map calls include the
# RERUN_TOP=2 second pass, so the multiplier is of the whole daily map spend):
#
#     N        coverage   map calls   vs N=8
#     8 (now)     53.3%        278     1.00x
#    12           73.3%        360     1.29x
#    16           87.0%        416     1.50x
#    24           97.1%        457     1.64x
#    every        100.0%       469     1.69x
#
# Each extra session is a whole additional `claude -p` call, but NOT a whole extra prompt
# prefix - sequential map calls cache-READ it (SKILL.md, measured with a no-flag control),
# so the marginal cost is the extract body plus that call's output, ~35K tokens at the
# default byte budget. Pricing an added session at the run average (680K/8) double-counts
# cache writes the marginal call does not repeat. Still real daily quota, so the default
# stays at 8; override for a run:
#   RETRO_MAX_ANALYSIS_SESSIONS=16 bash run_retro.sh
# `--top N` still LOWERS it per-invocation and is clamped to this value, so a stray
# `--top 50` cannot blow the budget open by accident.
MAX_ANALYSIS_SESSIONS = int(os.environ.get("RETRO_MAX_ANALYSIS_SESSIONS", 8))
MAX_GENERATION_SECS = float(os.environ.get("RETRO_MAX_GENERATION_SECS", 1800))
TRUNC_SLOT = "\x00truncation-header-slot"  # placeholder, filled once the body is built
DENIAL_RE = re.compile(r"doesn't want to proceed|denied by|permission denied", re.I)

# `errors` (every is_error tool_result) sums four incompatible things, so a fall in it is
# equally consistent with "the code broke less" and "the guardrails were relaxed" and "the
# API had a better day". Categorising one real day's 151 errors: ~76 genuine tool failures,
# 22 permission/classifier denials, 17 worktree containment refusals, 20 stale-read protocol
# nudges, 7 harness outages, 6 hook-guard refusals (instrument audit, 2026-08-26). Only the
# first is friction the agent can reduce by working better. Patterns below are taken from
# real result bodies, not invented.
#
# A guard said no. Terminal-by-policy or self-correcting in one turn; the agent adapts and
# moves on. More of these can mean the guards got STRICTER, which is not a regression.
POLICY_BLOCK_RE = re.compile(
    r"this session is isolated in the worktree"        # worktree containment refusal
    r"|file has been modified since read"              # stale-read protocol guard
    r"|has not been read yet\b"                        # read-before-edit protocol guard
    r"|DESTRUCTIVE COMMAND IN A FALLBACK POSITION"     # destructive-git-guard
    r"|DESTROYING THE RECOVERY LAYER"                  # destructive-git-guard
    r"|ON THE DEFAULT BRANCH"                          # commit --no-verify guard
    r"|EnterWorktree and retry"                        # worktree-guard
    r"|requested permissions?\b|permission to use",
    re.I)

# Not the agent's behaviour at all: the model endpoint or the harness failed.
HARNESS_OUTAGE_RE = re.compile(
    r"is temporarily unavailable"                      # classifier/model timeout
    r"|classifier temporarily unavailable"
    r"|Command timed out after"                        # harness Bash ceiling (exit 143)
    r"|\b(?:503|overloaded_error|RESOURCE_EXHAUSTED|UNAVAILABLE)\b"
    r"|API Error: 5\d\d",
    re.I)


def classify_error(txt):
    """One is_error body -> 'harness_outage' | 'policy_block' | 'tool_failure'.

    Order matters: an outage is checked first because a timed-out command also carries an
    exit code, and a denial is checked before the fallback because DENIAL_RE bodies are
    otherwise shapeless. Everything unmatched is a genuine tool failure - the fallback is
    the honest branch, so a new guard shape shows up as tool_failure (visible, over-counted)
    rather than being silently absorbed into policy_block (invisible, under-counted).
    """
    if HARNESS_OUTAGE_RE.search(txt):
        return "harness_outage"
    if POLICY_BLOCK_RE.search(txt) or DENIAL_RE.search(txt):
        return "policy_block"
    return "tool_failure"


# A user-role row is not necessarily a human. Harness-injected blocks (background-task
# notifications, skill preambles, slash-command echoes, Stop-hook feedback) and, in a
# subagent transcript, the parent-authored brief all arrive as user turns. Counting them
# overstated human involvement 8.6x on one measured session and, because user_turns is
# friction_score's DENOMINATOR, made heavily-delegating sessions rank artificially clean -
# which decides which sessions get an analyst call at all (instrument audit, 2026-08-26).
def is_injected_turn(d):
    """True when a `user`-role row was written by the harness, not typed by a human.

    The transcript carries this STRUCTURALLY - `origin.kind` is "human" for a real turn and
    names the injector otherwise ("task-notification", ...), and `isMeta` flags the
    slash-command/caveat echoes. Prefer both over any text heuristic: measured on one real
    session-day, origin.kind splits 132 task-notifications from 18 human turns, and the 18
    matches an independent hand count of 19 genuine messages. The regex below is a fallback
    for rows that predate `origin`, and it must run against the raw string content because
    `blocks()` returns [] when `message.content` is a plain str - which is exactly the shape
    every injected row uses, and why a text-only test saw 1 of 133 of them.
    """
    if d.get("isMeta"):
        return True
    origin = d.get("origin")
    if isinstance(origin, dict) and origin.get("kind"):
        return origin["kind"] != "human"
    return bool(INJECTED_TURN_RE.search(raw_text_of(d)))


def raw_text_of(d):
    """All user-visible text on a row, whether content is a str or a block list."""
    content = (d.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    return " ".join(text_of(b) for b in blocks(d) if isinstance(b, dict))


INJECTED_TURN_RE = re.compile(
    r"<task-notification>"
    r"|<local-command-(?:caveat|stdout)>"
    r"|<command-(?:name|message|args)>"
    r"|Stop hook feedback:"
    r"|^Base directory for this skill:"
    r"|^\s*<system-reminder>",
    re.M)
# Verbal friction the per-turn density score would otherwise miss (no tool error thrown):
# a user CORRECTING the agent, or repeatedly NUDGING it to continue. Both are precise by
# construction - a correction matches a correction-specific phrase (never a bare verb that
# shows up in ordinary commands like "stop the server" / "revert the API"), and a nudge is
# the WHOLE stripped turn being a bare continuation word (so short answers like "2pm"/"ok"
# never count).
CORRECTION_RE = re.compile(
    r"(?i)"
    r"\bno,|\bnope\b|"                                  # leading \b only: 'no,' has no
    r"\byou forgot\b|\bforgot to\b|"                    #   trailing word boundary
    r"that'?s (?:wrong|not right|incorrect|not what)|"
    r"that is (?:wrong|incorrect|not what)|"
    r"not what i (?:wanted|asked|meant|said)|"
    r"isn'?t (?:right|correct)|"
    r"\bdon'?t do that\b|\bwhy did you\b|"
    r"you shouldn'?t have|\bthat'?s not what\b")
# Agent SELF-retraction (assistant voice) - distinct from CORRECTION_RE, which is the USER
# correcting the agent. Still precision-first: a bounded-gap variant ("my assessment THAT IT WAS
# CLEAN was wrong") was rejected in 2026-08-04's probe after it false-matched "My test covers the
# case where the input was wrong on purpose". That string is now a standing negative assert below.
#
# WIDENED 2026-08-06. The strict form measured ~13% recall: 5 matches on 2026-08-05 against an
# independently sampled true rate of ~33-38 (precision ~0.42 on a broad recall regex, hand-judged
# over 30 of 78 candidates). Four separate analysts flagged the counter as a false floor, and the
# day's top friction pattern was invisible to the scoreboard for four consecutive days as a result.
# Every clause added below was probed against the day's 1,960 real assistant text blocks BEFORE
# being added, and each one's sampled hits were read: recall 5 -> 26, no measured precision loss.
# Two candidates were deliberately NOT taken - bare `\bi assumed\b` (a statement of method at least
# as often as a retraction; narrowed to `than i assumed` / `wrongly assumed`) and a bare `on me`
# (anchored to `that's ... on me` instead).
RETRACTION_RE = re.compile(
    r"(?i)"
    r"(?:^|\n)\s*correction\b|"
    r"\bcorrection to what i (?:just )?said\b|"
    r"\bi (?:was wrong|misread|got that wrong|misstated|mis-stated)\b|"
    r"\bmy (?:test|tests|assessment|claim|statement|conclusion) (?:was|were) wrong\b|"
    r"\bthat (?:was|is) wrong[.,—-]|"
    r"\bi need to correct\b|"
    # "One correction to my earlier summary" - the line-start clause above misses mid-sentence use.
    # The object MUST be self-referential: a bare `correction to` false-matched "every correction
    # to one has to be checked against the other" twice on 2026-08-05 (general prose about
    # cross-document drift), which alone put precision at 24/26 = 0.92, under the 0.95 gate.
    r"\b(?:one |a )?correction (?:to (?:my|what i|something i|the record)|i owe|i must)\b|"
    # "my published number was wrong", "my ad-hoc-entry test was wrong" (<=4 tokens, so the
    # rejected unbounded-gap variant stays rejected and the negative assert still holds).
    r"\bmy [a-z0-9_./-]{1,30}(?: [a-z0-9_./-]{1,30}){0,3} (?:was|were|is|are) wrong\b|"
    r"\bthat(?:'s| is) (?:the [^.]{0,40} )?(?:mistake|error|one)?,? ?on me\b|"
    r"\bi (?:overstated|understated|conflated|misattributed|mislabel(?:l?ed)|"
    r"wrongly assumed|incorrectly assumed)\b|"
    r"\bthan i (?:assumed|thought)\b|"
    r"\b(?:is|was|are|were) confounded\b|"
    r"\b(?:names?|lists?|cites?) (?:it|them|that) wrongly\b|"
    # NARROWED 2026-08-10. The bare form matched "the verdict record ... is now stale" -
    # a fact about an artifact, not a self-retraction. It was the ONLY false positive in
    # the 128-block hand-labelled set, so it alone put precision at 0.500.
    r"\b(?:my|our) [^.\n]{0,45}?(?:is|was) now stale\b|"
    # WIDENED 2026-08-10 (rec:2026-08-03#13, chronic 6 days). The 2026-08-06 widening was
    # probed on 2026-08-05 text and still measured recall 0.091 / precision 0.500 against
    # the first FULL hand-label of a session (a59acf3f, 2026-08-09, all 128 assistant text
    # blocks). Every clause below was probed individually against those 128 blocks AND the
    # 297 blocks of held-out session 8d7a8a76, and each one's hits were read before it was
    # added - the discipline the 2026-08-06 note prescribes. Post-change on the labelled
    # set: precision 0.952 (strict) / 1.000 (counting the one hand-labelled borderline as
    # correct), recall 0.952. Held-out session: 8 -> 16 hits, all 16 read as genuine.
    r"\bhollow\b|"
    r"\bmutation (?:still )?passes\b|"
    r"\bpass(?:es|ed) pre-fix\b|"
    r"\bmy [^.\n]{0,40}? claim (?:may be|might be|was|is) wrong\b|"
    r"\bmy arithmetic\b|"
    r"\bi missed\b|\b(?:i'?d|i had) missed\b|"
    r"\b(?:i'?d|i would) have shipped\b|"
    r"\bnever asserted (?:anything|it|that)\b|\basserted nothing\b|"
    r"\b(?:was|were) vacuous\b|\bvacuous assertion\b|"
    r"\bunverified assertion\b|"
    r"\brefutes (?:my|a) claim\b|"
    r"\bovercla(?:im|ims|imed|iming)\w*\b|"
    r"\bmy (?:own )?mutation evidence\b|"
    r"\bi never (?:measured|ran|checked|verified|tested|proved)\b|"
    # First-person + emphasis-tolerant: [095] writes "I asserted *absence after the
    # fence*". A BARE `asserted absence` also matched two running-tally restatements
    # ("Six tests this session asserted absence...") which are reports, not retractions.
    r"\bi asserted \**absence\b|"
    r"\bdid(?:n'?t| not) prove what i claimed\b")
# Quoted/citation spans are EVIDENCE the agent is reporting, not a live retraction: a
# reviewer verdict in a blockquote, a commit log in a fence, a timestamped citation.
# Strip them before matching. NB the DOTALL scoping - a global re.S makes the `>` branch
# swallow every line after the first blockquote, which silently deleted a real retraction
# from the labelled set until it was measured.
QUOTE_STRIP_RE = re.compile(
    r"(?s:```.*?```)"
    r"|^[ \t]*>.*$"
    r"|^.*\b\d{2}:\d{2}:\d{2}\b.*$",
    re.M)


def strip_quoted(t):
    return QUOTE_STRIP_RE.sub(" ", t)
NUDGE_RE = re.compile(
    r"(?i)^\W*(?:continue|proceed|keep going|go on|carry on|go ahead|"
    r"next|resume|do it)\W*$")
# codex/agy review-GATE activity. A slow gate - a codex/agy review that blocks for many minutes,
# or many re-gate rounds - is the one wall-clock cost the neutral-gap rule would otherwise hide.
# Detection is TWO-part: the tool must be a job DISPATCHER (GATE_TOOLS) AND its input must carry a
# gate signature (GATE_RE). The tool-name gate is what stops an AskUserQuestion / Read / Edit that
# merely MENTIONS a gate (a question about the review-gate no-ship, reading a codex job file) from
# counting as gate time - that would mislabel human-answer + read latency as gate wait.
GATE_TOOLS = {"Bash", "Agent", "Skill"}   # the only tools that dispatch/poll a codex/agy gate job
# Tools whose gap MUST be human_wait, not blocked_secs, because only a human can close them -
# distinct from GATE_TOOLS's gate-mention filter (session-retro dimension-1 audit, 2026-08-20).
HUMAN_INTERACTIVE_TOOL_LABELS = {"tool_use:AskUserQuestion"}
GATE_RE = re.compile(
    r"(?i)"
    r"adversarial-review|codex-companion|agy-companion|"                    # reviewer runtimes
    r"codex\s+exec|/codex:|codex:(?:adversarial|rescue|task)|codex-rescue|" # codex CLI/skill/subagent
    r"/agy:|agy:rescue|agy-rescue|\bagy\s|"                                 # agy CLI/skill/subagent
    r"lander\.sh|review-gate|gate-loop|"                                    # gate/land scripts
    r'"skill":\s*"[^"]*(?:review-gate|gate-loop|:land|:ship)')              # gate skills via Skill


# A background job (a `run_in_background` Bash, or an async Agent) reports completion as a
# <task-notification> user turn carrying the DISPATCHING tool_use's id - so dispatch->completion
# correlates exactly, no heuristics. This is what makes "blocked on a background job" measurable
# and separable from "waiting on a human": both look like an assistant turn followed by a long
# gap, and only the in-flight set tells them apart.
BG_NOTIFY_RE = re.compile(r"<task-notification>.*?<tool-use-id>([^<]+)</tool-use-id>", re.S)
# Row types that ARE agent steps. Everything else in a transcript is meta bookkeeping.
TIMELINE_TYPES = {"user", "assistant"}


def merge_intervals(iv):
    """Union length of [start, end) intervals, in seconds. Overlapping background jobs are
    concurrent wall-clock, so their waits must not be summed twice."""
    total, cur_s, cur_e = 0.0, None, None
    for s0, e0 in sorted(iv):
        if cur_e is None or s0 > cur_e:
            if cur_e is not None:
                total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s0, e0
        elif e0 > cur_e:
            cur_e = e0
    if cur_e is not None:
        total += (cur_e - cur_s).total_seconds()
    return total


# A Bash command that is PURELY a read (starts with a read-only verb) merely mentioning a
# gate keyword in a file path or grep pattern - e.g. `sed -n '48,62p' scripts/lander.sh`,
# `grep -n review-gate SKILL.md` - is not a dispatch. The old "ponytail: acceptable" call on
# this (see git history) was made when the effect was small; re-measured against
# session-retro's own calibration session (3f02f940, 2026-08-07) it now inflates
# gate_calls to 51 / gate_wait_secs to ~3.06h against the documented ~1.6h hand-count -
# ~1.9x, undetected since the last fix (session-retro dimension-1 audit, 2026-08-20).
READ_ONLY_CMD_RE = re.compile(
    r"(?i)^\s*(?:grep|sed|cat|head|tail|less|nl|find|ls|wc|awk|diff|rg)\b")

# The same "merely mentions a gate" exclusion, applied to the Agent arm. An SDD implementer or
# an Explore/audit subagent routinely carries gate vocabulary in its BRIEF ("when green, run
# gate-loop", "analyze the gating regime") without dispatching a gate, and the whole lane -
# dispatch to completion notification - was then charged to gate_wait_secs. Measured over every
# transcript in ~/.claude/projects on 2026-08-26: 150 Agent calls counted as gate calls, of
# which 97 are real (codex:codex-rescue 96, agy:agy-rescue 1) and 53 are not (sdd-implementer
# 30, explore-readonly 18, general-purpose 5) - a 35% false-positive rate on this arm. It
# produced the single largest artifact of the error: 3ece6495 on 2026-08-25 reported
# max_gate_wait_secs=3803.7 ("a 63-minute gate round"), which was in fact one sdd-implementer
# lane (W0-J) running for 63 minutes - productive parallel work, not gate cost.
AGENT_GATE_TYPE_RE = re.compile(r"(?i)codex|agy")


def is_gate_call(name, input_json):
    """True iff a tool call is a codex/agy gate dispatch/poll: a job-dispatcher tool (GATE_TOOLS)
    whose input carries a gate signature. The name gate stops gate-MENTIONING reads/questions
    (AskUserQuestion, Read, Edit) from counting as gate time; for Bash specifically (which IS a
    dispatcher tool) a read-only command shape gets the same exclusion, since Bash is also how a
    gate file gets grepped/catted rather than run."""
    if name not in GATE_TOOLS or not GATE_RE.search(input_json):
        return False
    if name == "Bash":
        try:
            cmd = json.loads(input_json).get("command", "")
        except (json.JSONDecodeError, AttributeError):
            cmd = ""
        if READ_ONLY_CMD_RE.match(cmd):
            return False
    if name == "Agent":
        try:
            sub = json.loads(input_json).get("subagent_type", "") or ""
        except (json.JSONDecodeError, AttributeError):
            sub = ""
        if not AGENT_GATE_TYPE_RE.search(sub):
            return False
    return True


def parse_ts(s):
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt.astimezone() if dt.tzinfo is None else dt


def iter_lines(path):
    """Yield parsed JSONL lines, tolerating a partial final line (active writers)."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in raw.splitlines():
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue  # partial/corrupt line


def day_bounds(day):
    # DST-safe: build both midnights from naive dates, then localize
    start = datetime.combine(day, datetime.min.time()).astimezone()
    end = datetime.combine(day + timedelta(days=1), datetime.min.time()).astimezone()
    return start, end


def hhmmss(ts):
    """Wall-clock LOCAL time for an emitted timestamp.

    Transcripts stamp UTC ("...Z"), so parse_ts returns a UTC-aware datetime and a bare
    .strftime() on it prints UTC. day_bounds windows on LOCAL midnights, so before 2026-08-06
    every timestamp quoted in an extract was offset from the day it had been selected into -
    7h on PDT. A report citing "the sleep spam at 15:40:19" meant 08:40:19 local, and nothing
    said so, which makes a cited time impossible to reconcile against any other local record
    (verify.log rows, `ps` output, the user's own memory of the day).
    Emit local everywhere a human or an analyst LLM will read it; keep first_ts/last_ts as
    full ISO-8601 with offset, which is unambiguous either way.
    """
    return ts.astimezone().strftime("%H:%M:%S")


def blocks(msg):
    content = (msg.get("message") or {}).get("content")
    if isinstance(content, list):
        return content
    return []


def text_of(block):
    if isinstance(block, str):
        return block
    if isinstance(block.get("content"), str):
        return block["content"]
    if isinstance(block.get("content"), list):
        return " ".join(b.get("text", "") for b in block["content"] if isinstance(b, dict))
    return block.get("text", "") or ""


def scan_file(path, start, end):
    """Stats for one transcript file, counting only events inside [start, end)."""
    s = {
        "user_turns": 0, "injected_turns": 0, "assistant_turns": 0, "tools": {}, "errors": 0,
        "tool_failures": 0, "policy_blocks": 0, "harness_outages": 0,
        "error_samples": [], "interrupts": 0, "denials": 0, "perm_switches": 0,
        "retries": 0, "self_retractions": 0, "first_ts": None, "last_ts": None, "events": 0,
        "in_tokens": 0, "out_tokens": 0, "cache_read_tokens": 0,
        "cache_write_tokens": 0, "tool_secs": {}, "gaps": [], "repeated_error_runs": [],
        "corrections": 0, "nudges": 0,
        "gate_calls": 0, "gate_wait_secs": 0.0, "max_gate_wait_secs": 0.0,
        "work_secs": 0.0, "human_wait_secs": 0.0, "blocked_secs": 0.0,
        "model_latency_secs": 0.0, "idle_secs": 0.0,
        "bg_jobs": 0, "bg_job_secs": 0.0, "bg_blocked_secs": 0.0,
    }
    prev_call = None
    prev_ts = None          # datetime of the previous in-day event (transcript order)
    prev_label = None       # what the previous event was, for gap attribution
    pending = {}            # tool_use id -> (name, ts) awaiting its tool_result
    dispatched = {}         # tool_use id -> ts, kept for the whole scan (a background Bash
                            # gets its tool_result immediately; completion arrives much later)
    bg_iv = []              # (dispatch_ts, completion_ts) per correlated background job
    gate_ids = set()        # tool_use ids that dispatched a codex/agy gate
    gate_bg = []            # durations of gate jobs that ran in the BACKGROUND
    all_gaps = []           # [secs, at_hms, after_label, gap_start_ts, closing_role]
                            # closing_role is patched in at the END of the iteration that
                            # appended the gap - a gap is classified by the pair of events
                            # that BOUND it, never by the one that precedes it alone.
    cur_err = None          # first line of the current consecutive-error run
    cur_err_count = 0
    runs = []               # completed runs of >=2 identical consecutive errors

    def flush_run():
        if cur_err_count >= 2:
            runs.append({"snippet": cur_err, "count": cur_err_count})

    for d in iter_lines(path):
        if d.get("type") == "permission-mode":
            # untimestamped meta line: counted file-wide (only files with in-day
            # events reach the output at all)
            s["perm_switches"] += 1
            continue
        ts = parse_ts(d.get("timestamp", ""))
        if ts is None or not (start <= ts < end):
            continue
        s["events"] += 1
        s["first_ts"] = s["first_ts"] or ts.isoformat()
        s["last_ts"] = ts.isoformat()
        t = d.get("type")
        # The gap timeline is built from user/assistant rows ONLY. A transcript also carries
        # meta rows (system, attachment, queue-operation, file-history-delta) that are not agent
        # steps, and letting them terminate a gap destroys the attribution the label exists for:
        # measured on 3f02f940, 48,882 of 59,204 gap-seconds (83%) - including the session's
        # 7.25h stall - were attributed to "after system", and any gate gap with a system row
        # after the dispatch was silently dropped from gate_wait_secs.
        if t not in TIMELINE_TYPES:
            continue
        # inter-event gap in TRANSCRIPT order (not sorted), attributed to what the
        # previous event was. NEUTRAL wall-clock: may be human think-time, model
        # latency, async wait, or tool latency - not waste on its own.
        gap_idx = None
        if prev_ts is not None:
            # max(0): rows are not strictly monotonic (a file-history-delta can back-date)
            all_gaps.append([max(0.0, (ts - prev_ts).total_seconds()),
                             hhmmss(prev_ts), prev_label, prev_ts, None])
            gap_idx = len(all_gaps) - 1
        label = t
        has_tool_result = False
        if t == "assistant":
            s["assistant_turns"] += 1
            u = (d.get("message") or {}).get("usage") or {}
            s["in_tokens"] += u.get("input_tokens", 0) or 0
            s["out_tokens"] += u.get("output_tokens", 0) or 0
            s["cache_read_tokens"] += u.get("cache_read_input_tokens", 0) or 0
            s["cache_write_tokens"] += u.get("cache_creation_input_tokens", 0) or 0
            # NB: join with "\n", NOT " ". RETRACTION_RE anchors on (?:^|\n) for a leading
            # "Correction:", and space-joining multiple text blocks erases the block boundary,
            # so a block that STARTS with "Correction:" silently stops matching. Verified
            # 2026-08-04: " ".join -> MISS, "\n".join -> MATCH on the same input.
            atext = "\n".join(text_of(b) for b in blocks(d)
                              if isinstance(b, dict) and b.get("type") == "text")
            if RETRACTION_RE.search(strip_quoted(atext)):
                s["self_retractions"] += 1
            for b in blocks(d):
                if b.get("type") == "tool_use":
                    name = b.get("name", "?")
                    s["tools"][name] = s["tools"].get(name, 0) + 1
                    label = "tool_use:" + name
                    bid = b.get("id")
                    if bid:
                        pending[bid] = (name, ts)  # local to this file: never crosses scans
                        dispatched[bid] = ts
                    call = (name, json.dumps(b.get("input", {}), sort_keys=True))
                    if call == prev_call:
                        s["retries"] += 1
                    prev_call = call
                    if is_gate_call(name, call[1]):
                        s["gate_calls"] += 1
                        label = "gate:" + name   # so the FOLLOWING gap is attributed to the gate
                        if bid:
                            gate_ids.add(bid)
        elif t == "user":
            # background-job completion: correlates to its dispatching tool_use by id.
            # content is a bare string on these turns, so read it raw, not via blocks().
            raw = (d.get("message") or {}).get("content")
            raw_text = raw if isinstance(raw, str) else " ".join(
                text_of(b) for b in blocks(d) if isinstance(b, dict))
            for nm_ in BG_NOTIFY_RE.finditer(raw_text):
                t0 = dispatched.get(nm_.group(1))
                if t0 is not None and ts > t0:
                    bg_iv.append((t0, ts))
                    if nm_.group(1) in gate_ids:
                        gate_bg.append((ts - t0).total_seconds())
            for b in blocks(d):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    has_tool_result = True
                    label = "tool_result"
                    tuid = b.get("tool_use_id")
                    if tuid in pending:
                        nm, t0 = pending.pop(tuid)  # pop: a replayed result can't double-count
                        s["tool_secs"][nm] = s["tool_secs"].get(nm, 0) + (ts - t0).total_seconds()
                    txt = text_of(b)
                    if b.get("is_error"):
                        s["errors"] += 1  # retained: total, for continuity with older rows
                        s[classify_error(txt) + "s"] += 1
                        snip = txt.strip().split("\n")[0][:200]
                        if len(s["error_samples"]) < 10:
                            s["error_samples"].append(snip)
                        if DENIAL_RE.search(txt):
                            s["denials"] += 1
                        if snip == cur_err:
                            cur_err_count += 1
                        else:
                            flush_run()
                            cur_err, cur_err_count = snip, 1
                    else:
                        flush_run()  # a successful result breaks the error run
                        cur_err, cur_err_count = None, 0
                txt = text_of(b) if isinstance(b, dict) else str(b)
                if "[Request interrupted" in txt:
                    s["interrupts"] += 1
            if not has_tool_result:
                # NB: if/else, never `continue` - the gap-timeline append below runs for
                # every user/assistant row, and skipping it for injected turns would drop
                # them from the wall-clock partition and silently re-break the very
                # attribution that human_wait/idle depends on.
                if is_injected_turn(d):
                    s["injected_turns"] += 1  # harness-injected, not a human turn
                else:
                    s["user_turns"] += 1
                    # verbal friction: only genuine user turns. The interrupt marker is
                    # itself a user text turn - skip it (already counted as an interrupt).
                    utext = " ".join(text_of(b) for b in blocks(d)
                                     if isinstance(b, dict) and b.get("type") == "text")
                    if "[Request interrupted" not in utext:
                        if CORRECTION_RE.search(utext):
                            s["corrections"] += 1
                        if NUDGE_RE.match(utext.strip()):
                            s["nudges"] += 1
        if gap_idx is not None:
            all_gaps[gap_idx][4] = ("assistant" if t == "assistant"
                                    else "tool_result" if has_tool_result else "user")
        prev_ts, prev_label = ts, label
    flush_run()
    s["gaps"] = [{"secs": round(g, 1), "at": at, "after": lab}
                 for g, at, lab, _t0, _cl in sorted(all_gaps, key=lambda x: -x[0])[:5]]
    # wall-clock spent waiting on a codex/agy gate = the gaps that FOLLOW a gate call. Reuses the
    # gap machinery (a gate that blocks 25 min shows as a big gap labeled "gate:*") instead of a
    # stateful dispatch->verdict correlator.
    # ...plus the gates dispatched as BACKGROUND jobs, whose following gap is ~0s (the tool_result
    # returns at once) and whose real wait only ends at the completion notification. Measured on
    # 3f02f940, the gap-only rule saw 8 min of gate wait against a hand-count of ~1.6h.
    gate_gaps = [g for g, _at, lab, _t0, _cl in all_gaps
                 if lab and lab.startswith("gate:")] + gate_bg
    s["gate_wait_secs"] = round(sum(gate_gaps), 1)
    s["max_gate_wait_secs"] = round(max(gate_gaps), 1) if gate_gaps else 0.0
    # ponytail: a job still in flight when the window closed is simply uncounted - a dispatch id
    # with no notification is indistinguishable from an ordinary synchronous tool call. That makes
    # bg_* a LOWER bound; charge it to the window edge only if the undercount is ever shown to bite.
    s["bg_jobs"] = len(bg_iv)
    s["bg_job_secs"] = round(sum((e - b).total_seconds() for b, e in bg_iv), 1)
    s["bg_blocked_secs"] = round(merge_intervals(bg_iv), 1)
    # WALL-CLOCK PARTITION of the session's span, one bucket per inter-event gap:
    #   blocked        - the gap follows a tool_use/gate dispatch (synchronous tool latency), OR
    #                    the agent ended its turn while a background job was still in flight
    #   human_wait     - the gap CLOSES on a user message: only a human could restart it
    #   model_latency  - a pending turn answered inside MAX_GENERATION_SECS
    #   idle           - a pending turn NOT answered inside MAX_GENERATION_SECS: the machine
    #                    sat on it. Never `work`.
    #   work           - everything else: model thinking between a tool result and the next call
    # Every gap is classified by the PAIR of events bounding it. Classifying by the preceding
    # event alone was wrong in both directions: a user-opened gap was `work` however long it ran
    # (2026-08-16 `78476725`: one 38,833.3s span = 98.4% of the day's work_secs, on a session
    # with zero tool calls), and an assistant->assistant generation gap was `human_wait`
    # (`67080bab`, where the real human_wait is 0s).
    for g, _at, lab, t0, close in all_gaps:
        if lab in HUMAN_INTERACTIVE_TOOL_LABELS:
            # a dispatch that itself REQUIRES the human to answer (e.g. AskUserQuestion) - its
            # gap closes on a "user" turn structurally like any tool_result, but that closing
            # turn IS the human, not a mechanical result. Without this the wait launders into
            # `blocked_secs` (session-retro dimension-1 audit, 2026-08-20), backwards for a
            # tool whose purpose is to make human-wait nameable.
            s["human_wait_secs"] += g
        elif lab and (lab.startswith("tool_use:") or lab.startswith("gate:")):
            s["blocked_secs"] += g
        elif lab == "assistant" and any(b <= t0 < e for b, e in bg_iv):
            s["blocked_secs"] += g
        elif close == "user":
            s["human_wait_secs"] += g
        elif lab == "user":
            s["model_latency_secs" if g < MAX_GENERATION_SECS else "idle_secs"] += g
        elif lab == "assistant":
            s["model_latency_secs"] += g
        else:
            s["work_secs"] += g
    for k in ("work_secs", "human_wait_secs", "blocked_secs",
              "model_latency_secs", "idle_secs"):
        s[k] = round(s[k], 1)
    s["repeated_error_runs"] = runs
    # A retro STUB - the pipeline analysing its own output - is one user turn, one or two
    # assistant turns, and zero tool calls, under the "-" project. Its assistant text is
    # QUOTING transcript evidence, so any retraction phrasing in it belongs to the session
    # being analysed, not to this agent. Hard-zero both verbal counters there: 2026-08-09
    # scored self_retractions=1 on such a stub (session 17d87756).
    if is_retro_stub(s):
        s["self_retractions"] = 0
        s["corrections"] = 0
    return s


def merge_sub(parent, sub):
    for k in ("errors", "tool_failures", "policy_blocks", "harness_outages",
              "injected_turns", "interrupts", "denials", "retries", "assistant_turns",
              "in_tokens", "out_tokens", "cache_read_tokens", "cache_write_tokens",
              "corrections", "nudges", "gate_calls", "self_retractions"):
        parent[k] += sub[k]
    # gate_wait_secs stays parent-only (gap-derived; a subagent's gaps aren't the parent's
    # wall-clock - same rule as gaps/duration below)
    for name, n in sub["tools"].items():
        parent["tools"][name] = parent["tools"].get(name, 0) + n
    for name, sec in sub.get("tool_secs", {}).items():
        parent["tool_secs"][name] = parent["tool_secs"].get(name, 0) + sec
    # subagent errors are already in the parent's score/evidence path; fold their
    # repeated-error runs too (re-ranked/truncated in scan_day). gaps/duration stay
    # parent-only (a subagent's gaps are not the parent's wall-clock).
    parent["repeated_error_runs"] = parent["repeated_error_runs"] + sub.get("repeated_error_runs", [])
    parent["error_samples"] = (parent["error_samples"] + sub["error_samples"])[:10]


def friction(s):
    # A single-turn negative/sandbox test is not friction: no thrash, and the
    # error IS the asserted outcome. Suppress so probes don't outrank real friction.
    if s["retries"] == 0 and s["interrupts"] == 0 and s["errors"] > 0 \
       and s.get("corrections", 0) == 0 and s.get("nudges", 0) == 0 \
       and s["error_samples"] \
       and all("operation not permitted" in e for e in s["error_samples"]):
        return 0.0
    # ponytail: naive weighted density; tune weights when reports misrank.
    # corrections/nudges are verbal friction (no tool error) - .get keeps pre-migration
    # scan.json and the probe dict below KeyError-free.
    # `denials` used to be added ON TOP of `errors`, of which it is a strict subset - so a
    # permission denial scored 4 and a real test failure scored 3. Now the weighted term is
    # tool_failures only; policy blocks and harness outages are reported but not scored.
    # .get keeps pre-migration scan.json readable (falls back to the old undivided total).
    fails = s.get("tool_failures", s["errors"])
    weighted = (3 * fails + 5 * s["interrupts"] + 2 * s["retries"]
                + 4 * s.get("corrections", 0) + 2 * s.get("nudges", 0))
    turns = max(s["user_turns"] + s["assistant_turns"], 1)
    return round(100.0 * weighted / turns, 1)


def scan_day(day):
    start, end = day_bounds(day)
    now = datetime.now(timezone.utc)
    sessions, active = [], []
    active_no_events = 0
    day_start_epoch = start.timestamp()
    for proj in sorted(PROJECTS.iterdir()):
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            st = f.stat()
            if st.st_mtime < day_start_epoch:
                continue  # no appends since day start -> no in-day events
            # still-active files ARE scanned: target-day events are already
            # written (new appends are a later day) and iter_lines tolerates a
            # partial final line; 'active' is informational only
            is_active = now.timestamp() - st.st_mtime < ACTIVE_GRACE_SECS
            s = scan_file(f, start, end)
            if s["events"] == 0:
                # Recent mtime but NO in-day events: the session belongs to a LATER
                # day and legitimately has nothing here. Naming it in `still_active`
                # implied its figures were missing from the day's totals, and a
                # reader acted on exactly that - the 2026-08-09 report opened with a
                # caveat about a session "absent from every figure above" which in
                # fact had 889 events, all dated 2026-08-10. Count it instead of
                # naming it: nothing hidden, nothing implied. Do NOT emit a partial
                # all-nulls record for it (rec:2026-08-09#7's proposed artifact) -
                # that fabricates a row for a session with no data on this date.
                if is_active:
                    active_no_events += 1
                continue
            if is_active:
                active.append(f"{proj.name}/{f.stem}")
            subdir = f.parent / f.stem / "subagents"
            subs = list(subdir.glob("*.jsonl")) if subdir.is_dir() else []
            # `subagents` counts the subagents that ran ON THIS DAY, sliced identically to
            # tools/errors/total_tokens. Counting FILES instead carried the whole transcript's
            # history into a day it did not touch: 2026-08-16's `129c1cb6` reported
            # subagents=10 on a record with 2 events, zero turns and no tools - those 10 ran
            # on 08-15.
            in_day_subs = 0
            for sub in subs:
                sub_s = scan_file(sub, start, end)
                if sub_s["events"]:
                    in_day_subs += 1
                merge_sub(s, sub_s)
            dur = 0.0
            if s["first_ts"] and s["last_ts"]:
                dur = (parse_ts(s["last_ts"]) - parse_ts(s["first_ts"])).total_seconds()
            s["duration_secs"] = round(dur, 1)
            s["total_tokens"] = (s["in_tokens"] + s["out_tokens"]
                                 + s["cache_read_tokens"] + s["cache_write_tokens"])
            s["tool_secs"] = {k: round(v, 1) for k, v in s["tool_secs"].items()}
            s["repeated_error_runs"] = sorted(
                s["repeated_error_runs"], key=lambda r: -r["count"])[:3]
            s.update(project=proj.name, session=f.stem, path=str(f),
                     subagents=in_day_subs, friction_score=friction(s))
            # A record with delegated work but no turns and no tools is a slicing bug, not a
            # session. Fail loudly rather than emitting it - it inflates the day's session
            # count and corrupts any subagent-derived metric.
            assert not (s["subagents"] and not s["user_turns"]
                        and not s["assistant_turns"] and not s["tools"]), s
            sessions.append(s)
    sessions.sort(key=lambda x: x["friction_score"], reverse=True)
    return {"date": day.isoformat(), "generated": now.isoformat(),
            "sessions": sessions, "still_active": active,
            # every name in `still_active` HAS a record in `sessions`; this is the
            # count of live files that contributed no in-day events at all.
            "active_no_in_day_events": active_no_events}


MIN_PROBE_LEN = 8     # a shorter literal matches incidentally; "git" is not a signature
MAX_PROBE_LEN = 120   # also the cap _sanitize_summary imposes on every other stored field


def valid_probe(pattern):
    """Normalize a rec probe, or None if it is not usable.

    A probe is a LITERAL substring, matched case-insensitively. It is deliberately not a
    regex: the pattern comes from the REPORT, which is model output over untrusted transcript
    content, and `re` offers no per-match timeout, so a model-authored pattern run against
    every event of every transcript is an unbounded-backtracking surface that a length cap
    does not bound. `str.find` cannot blow up. Metacharacters are therefore DATA - a probe
    for `rc=$?` or `([` is stored and matched verbatim.

    Validation is printable-ASCII plus a length band. It does NOT route through
    `_sanitize_summary`, which strips `<`, `>` and `"` and truncates at 120: for a summary
    that is cosmetic, but it would silently rewrite a probe into a DIFFERENT literal that
    still matches things, which is worse than rejecting it. MAX_PROBE_LEN is set to that same
    120 so the two can never disagree.

    A pattern failing any check returns None and is DROPPED, never degraded to something that
    matches less: a probe scoring 0 because it is broken is indistinguishable from a fix that
    worked, which is the exact defect this whole axis exists to remove.
    """
    if not isinstance(pattern, str):
        return None
    p = pattern.strip()
    if not (MIN_PROBE_LEN <= len(p) <= MAX_PROBE_LEN):
        return None
    if not re.fullmatch(r"[\x20-\x7e]+", p):
        return None
    return p


def probe_text_of(d):
    """Every string anywhere in a transcript row, for probe matching only.

    A row carries friction text in at least five different top-level fields, measured
    2026-09-02 on this machine's own corpus: searching for one real error token found it
    under `error` (41 rows), `message` (21), a TOP-LEVEL `content` (12), `toolUseResult` (6)
    and `attachment` (3). `raw_text_of` reads `message.content` only, so a probe built on it
    was blind to 62 of 83 of those - and blind precisely where tool errors and refusals land,
    which is most of what a probe is for. The first version of this function added `tool_use`
    inputs and still missed the other four fields; enumerating fields was the wrong shape.

    So: recurse and collect every string leaf. No field list to keep in sync, and no
    json.dumps either - a serialized row escapes quotes and newlines, so a probe containing
    either would silently stop matching.

    Known and accepted: this also sees the assistant's own prose, so a session that DISCUSSES
    a friction signature counts as an occurrence of it. That was already true of raw_text_of.
    It inflates a probe whose text gets quoted in retro discussion; `matches_before` is the
    hook for spotting one.
    """
    parts = []

    def walk(v):
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(d)
    return " ".join(parts)


def probe_counts(day, patterns):
    """{rec_id: pattern} -> {rec_id: matching events on `day`}.

    ONE walk of the corpus for all patterns. A per-pattern walk re-reads every transcript
    once per probe, and the digest carries tens of probes over a 14-day window.
    Rejected patterns are absent from the result rather than present as 0.
    """
    compiled = {}
    for rid, pat in (patterns or {}).items():
        v = valid_probe(pat)
        if v is not None:
            compiled[rid] = v.lower()
    if not compiled:
        return {}
    counts = {rid: 0 for rid in compiled}
    start, end = day_bounds(day)
    day_start_epoch = start.timestamp()
    if not PROJECTS.exists():
        return counts
    for proj in sorted(PROJECTS.iterdir()):
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            try:
                if f.stat().st_mtime < day_start_epoch:
                    continue   # no appends since day start -> no in-day events
            except OSError:
                continue
            for d in iter_lines(f):
                ts = parse_ts(d.get("timestamp"))
                if ts is None or not (start <= ts < end):
                    continue
                txt = probe_text_of(d)
                if not txt:
                    continue
                low = txt.lower()
                for rid, needle in compiled.items():
                    if needle in low:
                        counts[rid] += 1
    return counts


def _scan_totals(sessions):
    """Day totals, written into scan.json so the reduce model can COPY them.

    reduce.md orders "every scoreboard number is the one in scan.json / metrics.jsonl; copy
    it, never re-derive or re-total it" - but scan.json carried no day total and the
    metrics.jsonl row for the report date is written AFTER stamping, so the model was
    structurally forced to sum a `tools` dict across ~25 sessions in its head, which is
    exactly what the rule forbids. Measured 2026-08-27 over the 24 preserved reduce inputs:
    the narrated tool-call total disagreed with its own scan.json on 18 of 24 days, median
    6.3%, max 20.1%. This is not a new computation - it is the same sum cmd_metrics already
    performs, moved earlier so the number exists when the model needs it."""
    return {
        "tool_calls": sum(sum(s["tools"].values()) for s in sessions),
        "errors": sum(s["errors"] for s in sessions),
        "interrupts": sum(s["interrupts"] for s in sessions),
        "retries": sum(s["retries"] for s in sessions),
        "denials": sum(s["denials"] for s in sessions),
        "sessions": len(sessions),
    }


def cmd_scan(day):
    result = scan_day(day)
    result["totals"] = _scan_totals(result["sessions"])
    workdir = REPORTS / "work" / day.isoformat()
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "scan.json").write_text(json.dumps(result, indent=1))
    print(f"{'score':>6} {'err':>4} {'int':>4} {'rty':>4} {'retr':>5} {'turns':>6} {'tools':>6} "
          f"{'dur_s':>7} {'ktok':>6}  session")
    for s in result["sessions"]:
        print(f"{s['friction_score']:>6} {s['errors']:>4} {s['interrupts']:>4} "
              f"{s['retries']:>4} {s.get('self_retractions', 0):>5} "
              f"{s['user_turns']+s['assistant_turns']:>6} "
              f"{sum(s['tools'].values()):>6} {s.get('duration_secs', 0):>7.0f} "
              f"{s.get('total_tokens', 0) / 1000:>6.0f}  {s['project']}/{s['session'][:8]}")
    if result["still_active"]:
        print(f"still-active files scanned: {len(result['still_active'])} "
              f"(all have records above)")
    if result.get("active_no_in_day_events"):
        print(f"live files with no {day} events (belong to a later day, "
              f"correctly excluded): {result['active_no_in_day_events']}")
    print(f"scan.json: {workdir / 'scan.json'}")
    return result


def clip(txt, cap, tally=None):
    """Cap a message from the MIDDLE, keeping both ends, with an explicit marker.

    End-truncation loses the tail of every long message, and a deliverable's conclusion
    lives in its tail: on 2026-08-16 six of nine analysts reported their input cut
    mid-word ("But the same tu", "so no verifi") and correctly refused to assess what
    they could not see. `tally` is a one-element list accumulating elided chars so the
    extract header can state the bound as a number instead of an analyst inferring it
    from a broken word.
    """
    if len(txt) <= cap:
        return txt
    keep = cap // 2
    n = len(txt) - 2 * keep
    if tally is not None:
        tally[0] += n
    return f"{txt[:keep]}\n[... {n} chars elided ...]\n{txt[-keep:]}"


TRAJ_MARKER_BUDGET = 200  # the elision marker is itself part of the body


def trajectory_clip(lines, cap):
    """Drop whole events from the MIDDLE of a trajectory until it fits `cap`.

    Same rule as clip(), one level up. Breaking out of the event loop at the cap kept the
    HEAD of the session, and a session's lands, cleanups and destructive commands live in
    its TAIL: on 2026-08-13 one session's extract stopped at [08:38:29] against a
    20:51 event, so the retro never saw the `git worktree remove --force` that destroyed
    two hours of work. Returns chars dropped (0 if it already fit).
    """
    sizes = [len(x) + 1 for x in lines]
    total = sum(sizes)
    if total <= cap:
        return 0
    budget = max(cap - TRAJ_MARKER_BUDGET, 0)
    i = front = 0
    while i < len(lines) and front + sizes[i] <= budget // 2:
        front += sizes[i]
        i += 1
    j = len(lines)
    back = 0
    while j > i and back + sizes[j - 1] <= budget - front:
        j -= 1
        back += sizes[j]
    dropped = total - front - back
    lines[i:j] = [f"[... {j - i} whole events ({dropped} chars) elided from the MIDDLE of "
                  f"the trajectory - both the start and the TAIL of the session survive ...]"]
    return dropped


def prune_event(d, start, end, out, tally=None):
    ts = parse_ts(d.get("timestamp", ""))
    if ts is None or not (start <= ts < end):
        return
    t = d.get("type")
    stamp = hhmmss(ts)
    if t == "user":
        for b in blocks(d):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                txt = text_of(b).strip()
                cap = 500 if b.get("is_error") else 300
                tag = "TOOL_ERROR" if b.get("is_error") else "tool_result"
                out.append(f"[{stamp}] {tag}: {clip(txt, cap, tally)}")
            else:
                txt = (text_of(b) if isinstance(b, dict) else str(b)).strip()
                if txt:
                    out.append(f"[{stamp}] USER: {clip(txt, 1500, tally)}")
    elif t == "assistant":
        for b in blocks(d):
            if b.get("type") == "tool_use":
                args = clip(json.dumps(b.get("input", {})), 300, tally)
                out.append(f"[{stamp}] tool_use {b.get('name', '?')}: {args}")
            elif b.get("type") == "text" and b.get("text", "").strip():
                out.append(f"[{stamp}] ASSISTANT: {clip(b['text'].strip(), 800, tally)}")


def sub_errors(s, start, end, out, size, tally=None):
    """Append subagent TOOL_ERROR evidence (their stats are folded into the
    parent's score, so the report needs their evidence too)."""
    subdir = Path(s["path"]).parent / s["session"] / "subagents"
    if not subdir.is_dir():
        return size
    out.append("\n## Subagent errors")
    for sub in sorted(subdir.glob("*.jsonl")):
        for d in iter_lines(sub):
            if size > MAX_EXTRACT_BYTES:
                out.append(f"[extract truncated at the "
                           f"{MAX_EXTRACT_BYTES:,}-byte cap]")
                return size
            if d.get("type") != "user":
                continue
            ts = parse_ts(d.get("timestamp", ""))
            if ts is None or not (start <= ts < end):
                continue
            for b in blocks(d):
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                    line = (f"[{sub.stem} {hhmmss(ts)}] "
                            f"TOOL_ERROR: {clip(text_of(b).strip(), 300, tally)}")
                    out.append(line)
                    size += len(line) + 1
    return size


SLOW_RESERVE_MIN_SECS = 1800


def _pct(part, whole):
    return f"{100.0 * part / whole:.0f}%" if whole else "n/a"


def wallclock_line(s):
    """Where the session's span actually went. The five buckets partition duration_secs."""
    dur = s.get("duration_secs", 0) or 0
    w, h, b = (s.get("work_secs", 0), s.get("human_wait_secs", 0), s.get("blocked_secs", 0))
    m, i = s.get("model_latency_secs", 0), s.get("idle_secs", 0)
    return (f"wall_clock: work={w}s ({_pct(w, dur)}) human_wait={h}s ({_pct(h, dur)}) "
            f"blocked={b}s ({_pct(b, dur)}) model_latency={m}s ({_pct(m, dur)}) "
            f"idle={i}s ({_pct(i, dur)}) of duration_secs={dur}  "
            "(human_wait = the gap closed on a user message, so only a human could restart it "
            "- a NAMEABLE cost when the next step was already known; blocked = waiting on a "
            f"tool or a background job; model_latency = a pending turn answered within "
            f"{MAX_GENERATION_SECS:.0f}s; idle = a pending turn NOT answered within it, i.e. the "
            "machine sat on it - never count idle as work)")


def bg_line(s):
    """Background-job serialization. par ~1.0x with many jobs = they ran one after another."""
    n, jobsec, blocked = (s.get("bg_jobs", 0), s.get("bg_job_secs", 0),
                          s.get("bg_blocked_secs", 0))
    par = f"{jobsec / blocked:.2f}x" if blocked else "n/a"
    return (f"bg_jobs={n} bg_job_secs={jobsec} bg_blocked_secs={blocked} parallelism={par}  "
            "(parallelism = total job time / wall-clock actually blocked on jobs. ~1.0x with "
            "several long jobs means they ran strictly one after another with nothing queued "
            "alongside - serialization is a NAMEABLE cost, not neutral async wait)")


REDUCE_ANCHOR = "You are writing the daily Claude Code session-retro report"


def _first_user_text(path, limit=400):
    """First user-turn text of a transcript, for telling the retro's own calls apart."""
    try:
        for d in iter_lines(Path(path)):
            if d.get("type") != "user":
                continue
            c = (d.get("message") or {}).get("content")
            txt = c if isinstance(c, str) else " ".join(
                text_of(b) for b in blocks(d) if isinstance(b, dict))
            if txt.strip():
                return txt.strip()[:limit]
    except (OSError, ValueError):
        pass
    return ""


def is_retro_stub(s):
    """This session IS the retro pipeline analysing something else - one user turn, no
    tools, under the `-` project - and so is not worth an analysis slot of its own.

    Analysing one is a retro OF a retro, which can only ever report "no waste, one turn":
    on 2026-08-16 nine of eleven sessions were these and the chain ran two levels deep.

    The shape test is on the scan RECORD, not on the session's output text: a heading test
    (what rec:2026-08-16#2 proposed) would have wrongly skipped `92572f74`, a map call over
    a real working session whose output opens "# Session retro: eldercare wake_min sentinel
    bar". The one text test here is against the retro's OWN reduce prompt - a first-party
    string, not transcript-derived content - to keep the daily report-writer call, whose
    analysis is the report's QA path and which produced rec:2026-08-16#4.

    NB: inside scan_file() neither `project` nor `path` is set yet, so both of those tests
    are inert there and the predicate reduces to the turn/tool test. Preserved on purpose -
    changing it would silently alter which sessions get their verbal counters zeroed, a
    different question from this one.
    """
    if not (s.get("project") in ("-", None, "")
            and not s["tools"] and s["user_turns"] <= 1):
        return False
    return not _first_user_text(s["path"]).startswith(REDUCE_ANCHOR) if s.get("path") else True


def analysis_cap(top):
    """Sessions that may take an analyst call: `--top` clamped to the configured ceiling.

    `--top` can only ever LOWER the count. Raising the ceiling is deliberately the env
    var and not a flag, because a flag is a per-invocation typo away from spending 50
    map calls, while the env var is a decision someone makes once for a run.
    """
    return min(top, MAX_ANALYSIS_SESSIONS)


def eligible_sessions(sessions):
    """Sessions that CAN take an analysis slot - everything but the retro's own calls.

    The denominator of the coverage ratio, and the input pick_sessions ranks. One
    definition, used by both: a denominator computed separately from the filter it
    describes drifts the first time either moves.
    """
    return [s for s in sessions if not is_retro_stub(s)]


def coverage_pct(analyzed, eligible):
    """Share of eligible sessions that got an analyst call. None when nothing was
    eligible - a day with no analysable sessions has no coverage, which is not 0%."""
    return round(100.0 * analyzed / eligible, 1) if eligible else None


def pick_sessions(sessions, cap):
    """Choose which `cap` sessions get an evidence extract -> (picked, slow_reserved).

    Retro stubs are dropped before any ranking: see is_retro_stub(). A day of nothing but
    stubs correctly yields zero extracts, which run_retro.sh already handles as a
    scan-only no-op report.

    Reserves up to 2 slots for the LONGEST sessions the friction ranking would not
    surface on its own, so "slow but quiet" sessions stop being invisible.

    The reserve used to key on `friction_score == 0`, which is only a proxy for "friction
    misses it" and is wrong on any day with more than `cap` friction-positive sessions. On
    2026-08-06 (22 sessions, 13 friction-positive, cap 8) every friction==0 session was a
    sub-5-minute stub, so the reserve spent 2 of the 8 analysis slots on a 56s and a 257s
    session while a 9.1h and a 7.4h session - friction-positive, but ranked below the cut -
    were dropped entirely. Key on the actual cut instead, with a duration floor so a quiet
    day cannot reserve trivia.
    """
    sessions = eligible_sessions(sessions)
    fric = [s for s in sessions if s["friction_score"] > 0]
    above_cut = {(s["project"], s["session"]) for s in fric[:cap]}
    slow = sorted((s for s in sessions if s.get("duration_secs", 0) > 0),
                  key=lambda x: -x.get("duration_secs", 0))
    reserve = [s for s in slow
               if (s["project"], s["session"]) not in above_cut
               and s.get("duration_secs", 0) >= SLOW_RESERVE_MIN_SECS]
    picked, seen, slow_added = [], set(), []

    def _add(s):
        k = (s["project"], s["session"])
        if k in seen:
            return False
        seen.add(k)
        picked.append(s)
        return True

    for s in reserve[:2]:
        if len(picked) < cap and _add(s):
            slow_added.append(s)
    for pool in (fric, slow):
        for s in pool:
            if len(picked) >= cap:
                break
            _add(s)
    picked.sort(key=lambda x: (x["friction_score"], x.get("duration_secs", 0)), reverse=True)
    return picked, slow_added


def cmd_extract(day, top):
    import hashlib
    workdir = REPORTS / "work" / day.isoformat()
    scan_path = workdir / "scan.json"
    result = json.loads(scan_path.read_text()) if scan_path.exists() else cmd_scan(day)
    start, end = day_bounds(day)
    cap = analysis_cap(top)
    picked, slow_added = pick_sessions(result["sessions"], cap)
    if slow_added:
        print("slow-reserved extracts (long wall-clock, below the friction cut): "
              + ", ".join(f"{s['session'][:8]}({s.get('duration_secs', 0):.0f}s)"
                          for s in slow_added))
    for s in picked:
        top_tools = sorted(s.get("tool_secs", {}).items(), key=lambda kv: -kv[1])[:3]
        gaps = s.get("gaps", [])[:3]
        rer = s.get("repeated_error_runs", [])
        out = [
            f"# Extract {s['project']}/{s['session']} ({day})",
            f"friction={s['friction_score']} errors={s['errors']} "
            f"interrupts={s['interrupts']} retries={s['retries']} "
            f"self_retractions={s.get('self_retractions', 0)} subagents={s['subagents']}",
            f"duration_secs={s.get('duration_secs', 0)} total_tokens={s.get('total_tokens', 0)} "
            f"cache_write_tokens={s.get('cache_write_tokens', 0)}",
            "slowest_tools_secs=" + (", ".join(f"{n}:{v}" for n, v in top_tools) or "none"),
            "largest_gaps=" + (", ".join(f"{g['secs']}s after {g['after']} @{g['at']}"
                                         for g in gaps) or "none")
            + "  (NEUTRAL wall-clock: human think-time / model latency / async wait / "
            "tool latency - NOT waste on its own)",
            wallclock_line(s),
            bg_line(s),
            "repeated_error_runs=" + (", ".join(f"{r['count']}x {r['snippet'][:60]!r}"
                                                for r in rer) or "none"),
            f"gate_calls={s.get('gate_calls', 0)} gate_wait_secs={s.get('gate_wait_secs', 0)} "
            f"max_gate_wait_secs={s.get('max_gate_wait_secs', 0)}  (codex/agy review-gate "
            "latency: many calls + high wait = slow/repeated gate rounds - a NAMEABLE cost)",
            TRUNC_SLOT,   # replaced once the body is built and the elided total is known
            "",
        ]
        trunc_at = out.index(TRUNC_SLOT)
        tally = [0]
        head_size = sum(len(x) + 1 for x in out)
        # sessions with subagents keep 15KB of the cap reserved for their error
        # evidence (their stats are folded into the score, so the report needs it)
        main_cap = MAX_EXTRACT_BYTES - (15_000 if s["subagents"] else 0)
        body = []
        for d in iter_lines(Path(s["path"])):
            prune_event(d, start, end, body, tally)
        # clip the WHOLE trajectory from the middle, never by stopping at the cap - the
        # tail is where the lands and the destructive commands are (see trajectory_clip)
        capped = trajectory_clip(body, main_cap - head_size)
        out.extend(body)
        size = head_size + sum(len(x) + 1 for x in body)
        if s["subagents"]:
            size = sub_errors(s, start, end, out, size, tally)
        # State the bound as a NUMBER. An analyst that can see how much was removed reports
        # a bounded conclusion; one that sees only a broken word has to guess.
        out[trunc_at] = (
            f"extract_cap={MAX_EXTRACT_BYTES} body_budget={main_cap} "
            f"truncated={'true' if (tally[0] or capped) else 'false'} "
            f"elided_chars={tally[0]} trajectory_capped={'true' if capped else 'false'} "
            f"trajectory_elided_chars={capped}  "
            "(long messages are clipped from the MIDDLE - both ends survive, and the elided "
            "run is marked inline. trajectory_capped=true means whole events were dropped from "
            f"the MIDDLE to fit the {main_cap}-byte cap - the start AND the tail of the session "
            "are present, the marked gap is not; elided_chars does NOT cover those)")
        # project hash in the name: same session basename in two projects must not collide
        h8 = hashlib.md5(s["project"].encode()).hexdigest()[:8]
        (workdir / f"extract-{s['session']}-{h8}.md").write_text("\n".join(out))
    # Friction rank, for the runner's double-map: a single map call's TAIL of findings
    # does not reproduce (mean pairwise theme overlap ~0.51 across three re-runs of one
    # real extract, 2026-08-26), so the top sessions are analyzed twice and only themes
    # appearing in both are promoted. `picked` is already in rank order; the filenames
    # are not, hence this file.
    (workdir / "rank.txt").write_text(
        "".join(f"extract-{s['session']}-"
                f"{hashlib.md5(s['project'].encode()).hexdigest()[:8]}\n" for s in picked))
    # stage previous complete report for the repeat-findings comparison
    prev = [p for p in sorted(REPORTS.glob("????-??-??.md"))
            if p.stem < day.isoformat() and COMPLETE_MARKER in p.read_text()]
    if prev:
        (workdir / "previous-report.md").write_text(prev[-1].read_text())
    # COVERAGE. The `--top 8` cap is fixed while the session count is not, so the
    # share of the day an analyst ever sees FALLS as the day gets busier - the days with
    # the most friction to find are the ones seen least. Measured 2026-09-02 over 36 days:
    # 216 of 409 eligible sessions analysed (52.8%), but 25.0% on 08-04, 28.6% on 08-26 and
    # 36.4% on 08-17 - the three busiest. (A first pass put this at 38% by dividing by RAW
    # session count; retro stubs are not work and do not belong in the denominator. The
    # ratio is only honest against `eligible_sessions()`, which is why it is computed here
    # rather than by a reader dividing two numbers that look adjacent.)
    # Nothing reported this: the report says "8 sessions analysed", never "of 28". Written
    # into scan.json's `totals` (not computed here for print only) so reduce copies it
    # under the same copy-never-re-derive rule as every other day total, and so
    # cmd_metrics can carry it into metrics.jsonl for a trend.
    eligible = eligible_sessions(result["sessions"])
    result.setdefault("totals", {}).update({
        "sessions_eligible": len(eligible),
        "sessions_analyzed": len(picked),
        "analysis_coverage_pct": coverage_pct(len(picked), len(eligible)),
    })
    scan_path.write_text(json.dumps(result, indent=1))
    print(f"extracted {len(picked)} sessions to {workdir}")
    print(f"analysis coverage: {len(picked)} of {len(eligible)} eligible "
          f"({result['totals']['analysis_coverage_pct']}%) of "
          f"{len(result['sessions'])} sessions on the day")


def cmd_metrics(day):
    """Upsert one per-day summary line into metrics.jsonl (atomic replace-by-date)."""
    scan = json.loads((REPORTS / "work" / day.isoformat() / "scan.json").read_text())
    ss = scan["sessions"]
    line = {
        "date": day.isoformat(),
        "sessions": len(ss),
        "tool_calls": sum(sum(s["tools"].values()) for s in ss),
        "errors": sum(s["errors"] for s in ss),
        # The split: only tool_failures is friction the agent can reduce by working better.
        "tool_failures": sum(s.get("tool_failures", 0) for s in ss),
        "policy_blocks": sum(s.get("policy_blocks", 0) for s in ss),
        "harness_outages": sum(s.get("harness_outages", 0) for s in ss),
        "injected_turns": sum(s.get("injected_turns", 0) for s in ss),
        "interrupts": sum(s["interrupts"] for s in ss),
        "retries": sum(s["retries"] for s in ss),
        "denials": sum(s["denials"] for s in ss),
        "top_friction": max((s["friction_score"] for s in ss), default=0),
        # .get defaults: a scan.json written before this field existed must not KeyError
        "tokens": sum(s.get("total_tokens", 0) for s in ss),
        "cache_write_tokens": sum(s.get("cache_write_tokens", 0) for s in ss),
        "max_duration_secs": max((s.get("duration_secs", 0) for s in ss), default=0),
        "gate_calls": sum(s.get("gate_calls", 0) for s in ss),
        # The TOTAL, not only the day's worst round: max answers "how long was the longest wait"
        # and nothing answered "how much of the day went into gating", which is the number a
        # gate-hours-per-land ratio needs. Session-hours like the wall-clock partition beside it -
        # concurrent sessions overlap, so it is compared against those, never against 24h.
        "gate_wait_secs": round(sum(s.get("gate_wait_secs", 0) for s in ss), 1),
        "max_gate_wait_secs": max((s.get("max_gate_wait_secs", 0) for s in ss), default=0),
        # wall-clock partition, summed across sessions (they overlap, so these are
        # session-hours, not clock-hours - compare the three against each other, not the day)
        "work_secs": round(sum(s.get("work_secs", 0) for s in ss), 1),
        "human_wait_secs": round(sum(s.get("human_wait_secs", 0) for s in ss), 1),
        "blocked_secs": round(sum(s.get("blocked_secs", 0) for s in ss), 1),
        "model_latency_secs": round(sum(s.get("model_latency_secs", 0) for s in ss), 1),
        "idle_secs": round(sum(s.get("idle_secs", 0) for s in ss), 1),
        "bg_jobs": sum(s.get("bg_jobs", 0) for s in ss),
        "bg_job_secs": round(sum(s.get("bg_job_secs", 0) for s in ss), 1),
        "bg_blocked_secs": round(sum(s.get("bg_blocked_secs", 0) for s in ss), 1),
    }
    # COVERAGE: first-to-last activity span, so a partial day cannot be read as a full one.
    # 2026-08-03 covered 20:21-23:59 local (3.6h, the machine was provisioned that day) and was
    # then used as a full-day baseline in every "three days moving the same way" claim in the
    # 2026-08-05 report. Normalized per hour of actual coverage, ONE of those five axes survived;
    # max_duration was pure censoring (no session that day COULD exceed 3.64h). Raw daily counts
    # are only comparable between days of comparable coverage - emit the span so a reader can
    # divide, and a low `coverage_hours` is a signal to normalize or to exclude the day entirely.
    firsts = [s.get("first_ts") for s in ss if s.get("first_ts")]
    lasts = [s.get("last_ts") for s in ss if s.get("last_ts")]
    if firsts and lasts:
        f_dt, l_dt = parse_ts(min(firsts)), parse_ts(max(lasts))
        line["coverage_hours"] = (
            round((l_dt - f_dt).total_seconds() / 3600.0, 2) if f_dt and l_dt else None
        )
    else:
        line["coverage_hours"] = None
    # Per-hour rates for the volume axes, so a trend claim never rests on a raw count alone.
    ch = line["coverage_hours"]
    line["tool_calls_per_hour"] = round(line["tool_calls"] / ch, 1) if ch else None
    line["errors_per_hour"] = round(line["errors"] / ch, 2) if ch else None
    # The honest primary axis. `errors_per_hour` is retained for continuity with rows
    # written before 2026-08-26 but must not be read as "friction" - see classify_error.
    line["tool_failures_per_hour"] = round(line["tool_failures"] / ch, 2) if ch else None
    # The effectiveness digest, persisted so it has a history (see effectiveness_counts).
    line.update(effectiveness_counts(day))
    # How many actions-log lines the reader could not parse on this date. A silent drop in
    # the instrument that measures adoption is exactly the defect this pair of fields exists
    # to make visible; `ledger_non_disposition` is the KNOWN-benign half (FINDING/NOTE/rule:).
    # Analysis coverage, written by cmd_extract into scan.json's totals. Absent on a
    # stub day (extract never ran) and on any scan.json predating the field - None, not 0.
    _tot = scan.get("totals", {})
    line["sessions_eligible"] = _tot.get("sessions_eligible")
    line["sessions_analyzed"] = _tot.get("sessions_analyzed")
    line["analysis_coverage_pct"] = _tot.get("analysis_coverage_pct")
    malformed, non_disp = ledger_drops()
    line["ledger_malformed"] = malformed
    line["ledger_non_disposition"] = non_disp
    _upsert_jsonl(REPORTS / "metrics.jsonl", line, key="date")
    print(f"metrics upserted for {line['date']} (coverage_hours={line['coverage_hours']})")


# Prefix-anchored: date/verb/rec-id/dash and a non-empty summary are mandatory. The reason
# clause is NOT required to end in "(...)" - real actions-log.md entries settled on
# multi-sentence narrative endings (no trailing parens) starting ~2026-08-12, and the old
# `\(.+\)$` end-anchor silently dropped 72 of 103 real lines as "malformed" - including the
# line marking rec:2026-08-14#7 itself as taken, which is why that rec stayed CHRONIC even
# after landing (session-retro dimension-2 audit, 2026-08-20). A genuinely empty summary
# (nothing after "- ") still fails to match.
# A parenthetical QUALIFIER is allowed in both real positions - after the verb
# ("deferred (never started) rec:...") and after the rec id ("taken rec:X (part 2 only) - ...").
# Both shapes occur in the real ledger and the un-qualified regex dropped 31 of 41 otherwise-valid
# lines, 11 of them "taken" (session-retro instrument audit, 2026-08-26). Rec ids may carry an
# alpha suffix (#4a). Groups are NAMED because the drop-classification below needs the verb.
# The dated `rec:<date>#<n>` form is tried FIRST so it keeps its own groups; the slug arm
# catches `rec:<slug>` and `rule:<slug>` adoptions, which are real dispositions that the old
# regex dropped as malformed (3) or that NON_DISPOSITION_RE's `\w+ rule:` arm absorbed as
# prose (4). Measured 2026-08-27: 7 real dispositions of 176 never reached the digest.
# `superseded` is a verb because the log already carries 3 such lines with real id->id links.
# `applied` is the third disposition for WRITTEN, EFFECT UNMEASURED. `taken` is defined as
# APPLIED AND OBSERVED TAKING EFFECT and `deferred` as not-started; 16 of the ledger's 37
# deferred items were neither, so the deferred count overstated open work by ~43% and each of
# the 16 resurfaced daily as an open rec (actions-log reconciliation, 2026-09-01). Mechanically
# `applied` behaves like `rejected`/`deferred` here - it is not `taken`, so it never enters the
# effectiveness index - and reduce.md gives it the suppression half.
LEDGER_RE = re.compile(
    r"^- \[(?P<line_date>\d{4}-\d{2}-\d{2})\] (?P<verb>taken|rejected|deferred|superseded|applied)"
    r"(?: \([^)]*\))? "
    r"(?:rec:(?P<rec_date>\d{4}-\d{2}-\d{2})#(?P<rec_n>\d+[a-z]?)"
    r"|(?P<kind>rec|rule):(?P<slug>[A-Za-z][A-Za-z0-9._-]*))"
    r"(?: \([^)]*\))? - \S.*$")


def _ledger_rid(m):
    """The rec id a matched ledger line disposes of. Dated form -> `<date>#<n>`; slug form ->
    `<kind>:<slug>`, which has no report date and therefore no temporal-plausibility check."""
    if m.group("rec_date"):
        return f'{m.group("rec_date")}#{m.group("rec_n")}'
    return f'{m.group("kind")}:{m.group("slug")}'


# Lines that are deliberately NOT rec dispositions. They are recognised so they can be
# counted as "known non-disposition" rather than silently swelling the malformed count -
# a silent drop in the instrument that measures adoption is the defect this pass exists to fix.
# Only FINDING/NOTE are deliberately-not-dispositions. The old second arm was `\w+ rule:`,
# which matched `taken rule:` and `rejected rule:` and filed 4 real adoptions as prose.
NON_DISPOSITION_RE = re.compile(
    r"^- \[\d{4}-\d{2}-\d{2}\] (?:FINDING|NOTE)\b")


def ledger_drops():
    """(malformed, non_disposition) counts over every '- [' line in the actions-log.

    Nothing in the pipeline may drop a ledger line without counting it here.
    """
    path = REPORTS / "actions-log.md"
    if not path.exists():
        return 0, 0
    malformed = non_disp = 0
    for line in path.read_text().splitlines():
        if not line.startswith("- [") or LEDGER_RE.match(line):
            continue
        if NON_DISPOSITION_RE.match(line):
            non_disp += 1
        else:
            malformed += 1
    return malformed, non_disp


def cmd_ledger(day):
    """Print only valid, temporally-plausible actions-log lines for a report date.

    Deterministic pre-filter (the reduce prompt's parse-only rules are defense in
    depth, not the boundary): drops malformed lines, lines referencing rec ids from
    reports that don't exist yet (blocks pre-seeded suppression of predictable future
    ids), and lines dated before the report that minted their rec id.
    """
    path = REPORTS / "actions-log.md"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        m = LEDGER_RE.match(line)
        if not m:
            continue
        line_date, rec_date = m.group("line_date"), m.group("rec_date")
        if rec_date:   # slug ids carry no report date, so neither check applies to them
            if rec_date >= day.isoformat():
                continue  # rec id from this or a future report: cannot be settled yet
            if line_date < rec_date:
                continue  # acted on a recommendation before its report existed
        print(line)


def cmd_missing_dates(today=None):
    today = today or date.today()
    yesterday = today - timedelta(days=1)
    complete = {p.stem for p in REPORTS.glob("????-??-??.md")
                if COMPLETE_MARKER in p.read_text()}
    if not complete:  # first run ever: seed with yesterday only, no backfill
        print(yesterday.isoformat())
        return
    # every date in the trailing 7-day window that lacks a complete report. Anchored on a
    # FIXED window, not the latest report: an out-of-order completion (e.g. 07-13 finishing
    # before a flaky 07-12) must not strand the earlier gap by moving the start past it.
    # The 7-day cap still enforces "never re-backfill old history".
    d = today - timedelta(days=7)
    while d <= yesterday:
        if d.isoformat() not in complete:
            print(d.isoformat())
        d += timedelta(days=1)


def _upsert_jsonl(path, line, key):
    """Atomic replace-by-`key` upsert into a JSONL file (mirrors the metrics writer):
    drop any existing row with the same key, append `line`, sort by key, replace via a
    pid-suffixed tmp so a manual backfill racing a scheduled run cannot interleave.
    ponytail: read-modify-write with no lock, exactly like the pre-existing cmd_metrics
    upsert. The retro runs once/day (launchd, single writer); a manual backfill racing the
    scheduled run is rare and human-initiated. Accepted pre-existing race - last-writer-wins
    could drop a concurrently-appended date; add a lock only if concurrent writers become
    real."""
    rows = []
    if path.exists():
        for l in path.read_text().splitlines():
            try:
                r = json.loads(l)
            except json.JSONDecodeError:
                continue
            if r.get(key) != line[key]:
                rows.append(r)
    rows.append(line)
    rows.sort(key=lambda r: r.get(key, ""))
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    os.replace(tmp, path)


REC_TAG_RE = re.compile(r"\[rec:\s*(\d{4}-\d{2}-\d{2})#(\d+)\]")


def _sanitize_summary(s):
    """Report text is model output from UNTRUSTED transcripts. A summary stored in recs.jsonl
    must not carry markup/control chars into a later reduce prompt: collapse whitespace, keep
    printable ASCII, drop angle brackets and quotes (the digest quotes summaries as `"{s}"` in
    a future day's trusted-zone prompt - an embedded `"` could cosmetically break out of that
    quoting; session-retro dimension-3 audit, 2026-08-20), cap length."""
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[^\x20-\x7e]", "", s)
    s = s.replace("<", "").replace(">", "").replace('"', "")
    return s[:120]


MECH_TIER_RE = re.compile(r"^\s*(?:\*\*)?tier:\s*(hook|script|test)\b", re.I)
PROBE_LINE_RE = re.compile(r"^\s*(?:\*\*)?Probe:\s*(.+)$")


def report_probe_gaps(text):
    """Rec ids in `text` that declare a mechanism tier but carry no USABLE `Probe:` line.

    Block-scoped on purpose: a whole-file count of `tier:` against a count of `Probe:` is
    satisfied by one rec carrying two probes while another carries none, which is the exact
    report this gate exists to reject.

    A block closes on the next rec heading OR the next `## ` section - not only at EOF. A
    `Probe:` line sitting under `## Next actions` is not part of the last recommendation, and
    letting it attach there re-opens the same masking bug one level down.

    Presence is not enough: the value must pass `valid_probe` or be the declared-absence form
    `none - <reason>`. This is the ONLY check that runs before the report is stamped, so a
    `Probe: x` that will be rejected later has to be rejected here, while rejection still
    means something.
    """
    gaps, mech_total = [], []
    state = {"cur": None, "mech": False, "probe": False}

    def close():
        if state["cur"] and state["mech"]:
            mech_total.append(state["cur"])
            if not state["probe"]:
                gaps.append(state["cur"])

    for line in text.splitlines():
        m = REC_TAG_RE.search(line)
        if line.lstrip().startswith("**[rec:") and m:
            close()
            state.update(cur="%s#%s" % (m.group(1), m.group(2)), mech=False, probe=False)
            continue
        if line.startswith("## "):
            close()
            state.update(cur=None, mech=False, probe=False)
            continue
        if not state["cur"]:
            continue
        if MECH_TIER_RE.match(line):
            state["mech"] = True
        p = PROBE_LINE_RE.match(line)
        if p:
            v = p.group(1).strip()
            if valid_probe(v) is not None or re.match(r"(?i)none\s*-\s*\S", v):
                state["probe"] = True
    close()
    return gaps, mech_total


def cmd_validate_report(path):
    """Exit 1 only when NO mechanism rec carries a usable probe; otherwise warn and pass.

    Proportionality is the whole point, and it is measured rather than assumed. Run against
    the five most recent real reports on 2026-09-02, this check found gaps in four of them
    (5, 5, 5 and 3 recs) - reports written before the contract existed. Satisfying the
    contract depends on reduce following a PROSE instruction, and this repo's own natural
    experiment puts prose-rule compliance at 31.11% before a rule versus 32.11% after, across
    1831 opportunities. So a strict gate would, on the evidence, discard whole days of retro
    output whenever one rec of five missed its line.

    Losing a day's report to enforce an instrument is the wrong trade: the report is the
    deliverable, the probe is a measurement of it. A wholesale ignore (no rec complied at all)
    still fails closed and is retried, because that means the contract did not land. A partial
    miss ships the report and is counted - `probe_drop` names each one and
    `PROBE-UNMEASURED n:<k>` totals them, with reduce instructed to treat a rising k as a
    finding about this loop. The instrument reports its own coverage instead of holding the
    deliverable hostage to it.
    """
    gaps, mech = report_probe_gaps(Path(path).read_text(encoding="utf-8", errors="replace"))
    if not gaps:
        return 0
    msg = "mechanism recs with no Probe: line: %s (%d of %d)" % (
        " ".join(gaps), len(gaps), len(mech))
    if len(gaps) == len(mech):
        print(msg + " - NO rec complied; the contract did not land", file=sys.stderr)
        return 1
    print("warning: " + msg + " - report still stamped; counted in PROBE-UNMEASURED",
          file=sys.stderr)
    return 0


def cmd_recs(day):
    """Upsert the rec-ids in <day>'s stamped report into recs.jsonl - the cross-day recurrence
    signal (a rec-id on multiple report dates = a pattern that recurred). Deterministic: only
    well-formed `[rec: <origin-date>#<n>]` tags whose origin is a real calendar date <= the
    report date; summaries charset-neutralized. Feeds only future retro bookkeeping.

    Two passes so a canonical `**[rec: ...] ...**` heading (in `## Recommendations`) always
    wins over an earlier casual cross-reference elsewhere in the report (e.g. "see [rec: ...]
    below") - first-match-wins per rid, but canonical headings are scanned first regardless of
    line position. Without this, a same-day earlier mention supplies the summary and the real
    one is discarded (confirmed live: the 2026-08-19 entry for 2026-08-17#3 read as a stray
    fragment, "` below for the sharpening.`"; session-retro dimension-2 audit, 2026-08-20)."""
    report = REPORTS / f"{day.isoformat()}.md"
    if not report.exists():
        return
    lines = report.read_text(encoding="utf-8", errors="replace").splitlines()
    # The `Dedup:` line reduce is required to emit under each canonical heading (see
    # prompts/reduce.md). Stored so the fresh-id-restatement rate is measurable in-band
    # rather than only by re-clustering the corpus by hand: a rec with repeat False and
    # no Dedup line is the writer skipping the gate.
    dedup, probe, cur = {}, {}, None
    for line in lines:
        m = REC_TAG_RE.search(line)
        if line.lstrip().startswith("**[rec:") and m:
            cur = f"{m.group(1)}#{m.group(2)}"
            continue
        d = re.match(r"\s*(?:\*\*)?Dedup:\*{0,2}\s*(.+)$", line)
        if d and cur and cur not in dedup:
            dedup[cur] = _sanitize_summary(d.group(1))
        p = re.match(r"\s*(?:\*\*)?Probe:\*{0,2}\s*(.+)$", line)
        if p and cur and cur not in probe:
            # NOT _sanitize_summary: it strips <, > and " and truncates at 120, which would
            # silently rewrite a probe into a DIFFERENT literal that still matches things.
            # valid_probe is this field's sanitizer, and it rejects rather than rewrites.
            probe[cur] = p.group(1).strip()
    recs = {}
    for canonical_only in (True, False):
        for line in lines:
            if canonical_only and not line.lstrip().startswith("**[rec:"):
                continue
            for m in REC_TAG_RE.finditer(line):
                origin, n = m.group(1), m.group(2)
                try:
                    if date.fromisoformat(origin) > day:
                        continue
                except ValueError:
                    continue  # `\d{4}-\d{2}-\d{2}` can still be a non-calendar date (2026-00-99)
                rid = f"{origin}#{n}"
                if rid not in recs:
                    after = line.split(m.group(0), 1)[1]
                    recs[rid] = {"id": rid, "repeat": "REPEAT" in line.upper(),
                                 "summary": _sanitize_summary(after),
                                 "dedup": dedup.get(rid, "")}
    # A probe is only an outcome signal if it FIRED, often enough to be worth a ratio, before
    # the fix landed. One that never matched cannot tell "the fix worked" from "the probe is
    # wrong", and that failure is silent and self-flattering.
    #
    # This runs AFTER run_retro.sh has already stamped the report and cannot reject it - so
    # nothing here is a gate. What it does instead is make the failure legible: `probe_drop`
    # records WHY a probe was not kept, and probe_lines reports the count of mechanism recs
    # landing unmeasured. The pre-stamp half of this check is cmd_validate_report.
    #
    # `Probe: none - <why>` is the contract's explicit "this friction leaves no textual
    # trace". It satisfies the pre-stamp gate (a line IS present) and must NOT then be
    # matched as the literal "none - ...", which would find nothing and be reported as
    # `no-baseline` - i.e. as a broken probe rather than a declared absence. Three failure
    # modes, three names.
    declared_none = {rid for rid, p in probe.items() if p.lower().startswith("none")}
    valid = {rid: v for rid, v in ((r, valid_probe(p)) for r, p in probe.items()
                                   if r not in declared_none)
             if v is not None}
    baseline = {rid: 0 for rid in valid}
    if valid:
        for back in range(1, PROBE_BASELINE_DAYS + 1):
            for rid, n in probe_counts(day - timedelta(days=back), valid).items():
                baseline[rid] += n
    for rid, rec in recs.items():
        if rid not in probe:
            rec["probe"], rec["probe_baseline"], rec["probe_drop"] = "", None, ""
        elif rid in declared_none:
            rec["probe"], rec["probe_baseline"], rec["probe_drop"] = "", None, "declared-none"
        elif rid not in valid:
            # Rejected by valid_probe. Distinct from a probe that ran and found nothing:
            # baseline 0 here means NOT MEASURED, and conflating the two would report a
            # broken probe as evidence the friction was already absent.
            rec["probe"], rec["probe_baseline"], rec["probe_drop"] = "", 0, "invalid"
        else:
            n = baseline.get(rid, 0)
            rec["probe_baseline"] = n
            if n == 0:
                rec["probe"], rec["probe_drop"] = "", "no-baseline"
            elif n < PROBE_MIN_BASELINE:
                # KEPT, not dropped: too thin to score, but the count is the evidence the
                # threshold itself will be revisited against. Reported `unmeasurable`.
                rec["probe"], rec["probe_drop"] = valid[rid], "thin-baseline"
            else:
                rec["probe"], rec["probe_drop"] = valid[rid], ""
    _upsert_jsonl(REPORTS / "recs.jsonl",
                  {"report_date": day.isoformat(), "recs": list(recs.values())},
                  key="report_date")
    print(f"recs upserted for {day.isoformat()}: {len(recs)} rec-ids")


PROBE_WINDOW_DAYS = 7      # days each side of the take date that a probe rate averages over
PROBE_MIN_WINDOW_DAYS = 5  # covered days required on EACH side before a delta is scored
PROBE_BASELINE_DAYS = 14  # days before its report over which a probe's baseline is counted
PROBE_MIN_BASELINE = 5    # baseline matches required before a probe is scored, not just kept
PRIOR_REC_WINDOW = 21   # calendar days of prior rec titles shown to reduce for dedup
CHRONIC_WINDOW = 14     # calendar days (window span; see cmd_effectiveness cutoff)
CHRONIC_MIN_DAYS = 3    # appearances within the window to count as chronic
TOO_SOON_DAYS = 2       # a fix taken < this many days ago is not yet judged


def _load_recs():
    path = REPORTS / "recs.jsonl"
    out = []
    if path.exists():
        for l in path.read_text().splitlines():
            try:
                r = json.loads(l)
            except json.JSONDecodeError:
                continue
            if isinstance(r.get("report_date"), str) and isinstance(r.get("recs"), list):
                out.append(r)
    return out


def _rec_dispositions(day=None):
    """rec-id -> sorted [(date, verb)] for every DATED rec id in the actions-log.

    The shared ledger walk. `_taken_recs` reads it for "did this fix hold" and therefore
    keeps only ids whose latest verb is `taken`; `effectiveness_lines` reads it for
    "what happened to everything I proposed", which needs the other verbs too. Measured
    2026-09-01: the digest could see 110 of the 210 ids the loop has ever proposed, because
    the 12 `rejected`, 11 `deferred` and 16 `applied` ids were dropped here. Those are the
    record of what did NOT work, and recommending against a success-only view of your own
    history is survivorship bias by construction.

    Non-calendar dates (regex-shaped but invalid) are skipped so cmd_effectiveness's
    date.fromisoformat can never crash on model-written garbage. `day` bounds the ledger to
    lines written on or before that date, which is what makes a BACKFILL of a past date
    reconstruct what was true THEN rather than what is true now.
    """
    path = REPORTS / "actions-log.md"
    per_rid = {}
    if not path.exists():
        return per_rid
    for line in path.read_text().splitlines():
        m = LEDGER_RE.match(line)
        if not m:
            continue
        line_date, rec_date = m.group("line_date"), m.group("rec_date")
        if day is not None and line_date > day.isoformat():
            continue  # written after the date being reconstructed
        try:
            date.fromisoformat(line_date)
            if rec_date:
                date.fromisoformat(rec_date)
        except ValueError:
            continue  # non-calendar date in the (model-written) actions-log -> skip, never crash
        if rec_date and line_date < rec_date:
            continue  # acted before the report existed (see cmd_ledger)
        if not rec_date:
            # A slug id (`rec:<slug>`, `rule:<slug>`) has no report and therefore no entry in
            # recs.jsonl, so it can never recur and would sit in the digest as permanently
            # un-judgeable `holding`, inflating the denominator with rows that cannot move.
            # It is still PARSED - cmd_ledger shows it to the model and ledger_drops stops
            # counting it as malformed - which is the visibility the widening was for.
            continue
        per_rid.setdefault(_ledger_rid(m), []).append((line_date, m.group("verb")))
    for entries in per_rid.values():
        entries.sort()
    return per_rid


def _taken_recs(day=None):
    """rec-id -> earliest date it was marked taken, from the schema-validated actions-log.
    Non-calendar dates (regex-shaped but invalid) are skipped so cmd_effectiveness's
    date.fromisoformat can never crash on model-written garbage.

    `day` bounds the ledger to lines written on or before that date. Live this is a no-op
    (the file only holds past lines when the runner executes), but it is what makes a
    BACKFILL of a past date reconstruct what was true THEN rather than what is true now.
    """
    # Latest verb wins. A single pass keeping min(taken) and ignoring every other verb let a
    # LATER `rejected`/`deferred` fail to retract an earlier `taken`, and the rec kept counting
    # toward eff_holding. Measured 2026-08-27: 21 ids carry conflicting verbs and 2 are genuinely
    # taken-then-reversed (2026-08-20#7, 2026-08-17#3).
    taken = {}
    for rid, entries in _rec_dispositions(day).items():
        if entries[-1][1] != "taken":
            continue  # latest disposition retracts: not taken as of `day`
        taken[rid] = min(d for d, v in entries if v == "taken")
    return taken


RID_REF_RE = re.compile(r"rec:(\d{4}-\d{2}-\d{2}#\d+[a-z]?)")

# A backref is an edge only if it ASSERTS restatement. The dedup line reduce.md mandates is
# equally used to declare NON-identity, and scoring the extractor against the real corpus
# (2026-08-27, AFTER shipping - which was the wrong order) found 5 of 20 edges were exactly
# that: every rec on 2026-08-26 used "distinct from rec:<id>" to say it is NOT a repeat, and
# the first version of this clustering read all five as restatements. 75% precision.
# Checked against the characters BEFORE the reference, not the whole text, so
# "REPEAT of X, distinct from Y" keeps the X edge and drops the Y edge.
EDGE_NEGATION_RE = re.compile(
    r"distinct from|not a repeat|unlike|different from|separate from|as opposed to"
    r"|rather than|supersed|replaces", re.I)
EDGE_WINDOW = 90


def _cluster_recs(hist):
    """rec-id -> canonical (oldest) id of its restatement cluster.

    Recurrence used to require the writer to voluntarily re-use an OLD id, which the reduce
    template makes nearly impossible: it mints `<today>#<n>`, so a re-derived finding always
    got a fresh id and the digest scored it `holding`. Measured 2026-08-27: only 12 of 181 ids
    ever appear on more than one report date, `recurred-after-fix` had fired 4 times in the
    corpus's whole history, and 19 recs (10.5%, LOWER BOUND - the maintainers' own figure is
    15.5%) name an older rec inside their own text while carrying a new id.

    Those backrefs are the edges. They are the WRITER's explicit assertions, already stored in
    `summary` and `dedup` and - until now - never read. No threshold, no similarity score:
    content clustering was measured dead on this corpus (exact hash CAUGHT 0 of 106; Jaccard
    peaked at 12 of 106 with 43 false merges, and two members of the same 22-day theme score
    J=0.000 because summaries are capped at 120 chars)."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)   # oldest id is the canonical root

    for r in hist:
        for rec in r["recs"]:
            rid = rec.get("id")
            if not isinstance(rid, str):
                continue
            find(rid)
            text = f'{rec.get("summary") or ""} {rec.get("dedup") or ""}'
            for m in RID_REF_RE.finditer(text):
                ref = m.group(1)
                if ref == rid:
                    continue
                before = text[max(0, m.start() - EDGE_WINDOW):m.start()]
                if EDGE_NEGATION_RE.search(before):
                    continue  # the writer said this is NOT the same finding
                union(rid, ref)
    return {x: find(x) for x in parent}


def effectiveness_lines(day):
    """Deterministic fix-effectiveness + chronic-friction digest (TRUSTED, wrapper-computed
    from recs.jsonl + the schema-validated actions-log; reduce only narrates it). For each
    TAKEN rec, did its id reappear on a report dated AFTER it was taken (recurred despite the
    fix)? And which ids are chronic (present on >= CHRONIC_MIN_DAYS report dates within the
    last CHRONIC_WINDOW calendar days)? Each line carries the rec's stored summary so reduce
    can NAME it."""
    hist = sorted((r for r in _load_recs() if r["report_date"] < day.isoformat()),
                  key=lambda r: r["report_date"])  # ascending: last summary write = latest
    seen, summ = {}, {}
    for r in hist:
        for rec in r["recs"]:
            rid = rec.get("id")
            if isinstance(rid, str):
                seen.setdefault(rid, set()).add(r["report_date"])
                sm = rec.get("summary")
                if isinstance(sm, str) and sm:
                    summ[rid] = _sanitize_summary(sm)
    seen = {rid: sorted(dates) for rid, dates in seen.items()}
    # Restatement clusters: a fix's recurrence is any CLUSTER member reappearing, not just the
    # same id string. Without this the digest measures the writer's citation discipline.
    cluster = _cluster_recs(hist)
    cseen = {}
    for rid, dates in seen.items():
        cseen.setdefault(cluster.get(rid, rid), set()).update(dates)

    def label(rid):
        return f' "{summ[rid]}"' if rid in summ else ""

    out = []
    for rid, tdate in sorted(_taken_recs(day).items()):
        root = cluster.get(rid, rid)
        dates = sorted(cseen.get(root, set())) or seen.get(rid, [])
        via = "" if len(dates) == len(seen.get(rid, [])) else " via:cluster"
        after = [dd for dd in dates if dd > tdate]
        if after:
            status, last = "recurred-after-fix", after[-1]
        else:
            days_since = (day - date.fromisoformat(tdate)).days
            status = "too-soon" if days_since < TOO_SOON_DAYS else "holding"
            last = dates[-1] if dates else "none"
        out.append(f"EFFECTIVENESS rec:{rid} taken:{tdate} status:{status} "
                   f"last_seen:{last} seen_count:{len(dates)}{via}{label(rid)}")
    cutoff = (day - timedelta(days=CHRONIC_WINDOW)).isoformat()
    for rid in sorted(seen):
        in_window = [dd for dd in seen[rid] if dd >= cutoff]
        if len(in_window) >= CHRONIC_MIN_DAYS:
            out.append(f"CHRONIC rec:{rid} seen_count:{len(in_window)} "
                       f"dates:{in_window[0]}..{in_window[-1]}{label(rid)}")

    # The non-taken half of the history. Everything above narrates ids whose latest verb is
    # `taken`; measured 2026-09-01 that was 110 of the 210 ids ever proposed, so reduce ranked
    # tomorrow's recommendations against a success-only view of its own output. The two blocks
    # below carry the other 100: what was judged and NOT taken, and what was never judged at
    # all. Deliberately NOT one line per id - the digest is already 112 lines / 24 KB, and
    # dumping 100 more would cost more prompt than it informs. One aggregate line, plus the
    # UNDISPOSED ids inside the dedup window, which are the ones reduce is about to re-propose.
    disp = {rid: e[-1][1] for rid, e in _rec_dispositions(day).items()}
    proposed = set(seen)
    verb_counts = collections.Counter(v for rid, v in disp.items() if rid in proposed)
    undisposed = sorted(rid for rid in proposed if rid not in disp)
    if proposed:
        taken_n = verb_counts.get("taken", 0)
        out.append(
            "LEDGER-COVERAGE proposed:%d taken:%d applied:%d deferred:%d rejected:%d "
            "undisposed:%d take_rate:%.1f%%" % (
                len(proposed), taken_n, verb_counts.get("applied", 0),
                verb_counts.get("deferred", 0), verb_counts.get("rejected", 0),
                len(undisposed), 100.0 * taken_n / len(proposed)))
    # Per-TAG take rate. The aggregate above says adoption is the constraint; this asks whether
    # it is concentrated anywhere. On the live corpus (2026-09-01) it is NOT, and this line
    # exists mainly to keep that answer honest. Every UNDISPOSED id inside the window that day
    # was `[claude-md]`, which reads as "prose recs do not get taken" - but measured, claude-md
    # is 63/125 = 50% against an overall 51.9%. It dominates UNDISPOSED because it dominates
    # VOLUME (125 of 210 proposed, 60%), not because its rate is worse. tooling 60% and skill
    # 59% sit above; the lowest is code-org at 4/13 = 31%, n=13 and not separable from noise.
    # So tag is a WEAK discriminator here and must not be used to justify the prose-tier ban -
    # that ban rests on its own 1831-opportunity violation-rate measurement, not on this line.
    # Read a tag's rate only when its n is large and the gap is wide; otherwise report neither.
    tag_tot, tag_taken = collections.Counter(), collections.Counter()
    for rid in proposed:
        m = re.match(r"\s*\[([a-z-]+)\]", summ.get(rid, ""))
        tag = m.group(1) if m else "untagged"
        tag_tot[tag] += 1
        if disp.get(rid) == "taken":
            tag_taken[tag] += 1
    if tag_tot:
        parts = ["%s:%d/%d=%.0f%%" % (tg, tag_taken[tg], n, 100.0 * tag_taken[tg] / n)
                 for tg, n in tag_tot.most_common()]
        out.append("TAG-OUTCOMES taken/proposed " + " ".join(parts))

    win = (day - timedelta(days=PRIOR_REC_WINDOW)).isoformat()
    for rid in undisposed:
        dates = seen.get(rid, [])
        if not dates or dates[-1] < win:
            continue          # older than the dedup corpus reduce is given: out of scope
        out.append(f"UNDISPOSED rec:{rid} proposed:{dates[0]} last_seen:{dates[-1]} "
                   f"seen_count:{len(dates)}{label(rid)}")
    # The outcome half. Appended HERE, not exposed as its own subcommand, because
    # reduce-input.txt is assembled from exactly five subcommands and `probes` is not
    # one of them - a separate CLI would be dead code on the digest path.
    out.extend(probe_lines(day))

    return out


def probe_lines(day):
    """Deterministic outcome digest: for each TAKEN rec carrying a probe, the probe's match
    rate per coverage-hour before and after the day it was taken.

    This is the only line in the report not derived from what a previous report said.
    `status:holding` in the EFFECTIVENESS block means nobody restated the rec - measured on
    this corpus, raw-id recurrence had fired 0 times in 115 taken recs, so `holding` is very
    nearly the default verdict rather than a finding. A probe rate is counted from transcripts
    by a literal the writer chose but whose VALUE it does not supply.

    Rate, not raw count, because a busy day and a quiet day are otherwise incomparable - the
    same reason `errors_per_hour` replaced `errors`. The denominator is `coverage_hours` from
    metrics.jsonl; a day with no metrics row has no denominator and is SKIPPED rather than
    counted as a zero, which would read as an improvement.

    Two guards keep a number off a line that cannot support one. A window needs
    PROBE_MIN_WINDOW_DAYS covered days on EACH side - "nonempty" would let a verdict be
    computed from one day per side and flip daily. A probe needs PROBE_MIN_BASELINE matches
    IN THE PRE-TAKE WINDOW - not the stored `probe_baseline`, which counts the days before the
    REPORT and so need not overlap the window being compared at all. Neither is dropped: both
    report `status:unmeasurable`, because the count is the evidence these thresholds get
    revisited against.

    `control:` is the same before/after ratio over the day's overall tool-failure rate. A probe
    rate can fall because the fix worked or because the week got quieter, and without the
    control those are indistinguishable. It is coverage-WEIGHTED from raw fields, the same
    shape as the probe rate: an unweighted mean of daily rates would give a 0.2-hour day and a
    24-hour day equal weight, so disagreement could be a weighting artifact. No ratio-of-ratios
    is computed - at these n's it would be a precise-looking number built from two noisy ones.
    """
    cov, ctrl = {}, {}
    for r in _load_metrics():
        d, h = r.get("date"), r.get("coverage_hours")
        if isinstance(d, str) and isinstance(h, (int, float)) and h > 0:
            cov[d] = float(h)
            # RAW tool_failures, not tool_failures_per_hour: the control is re-normalized
            # over the window's own summed hours so it is the same shape as the probe rate.
            tf = r.get("tool_failures")
            if isinstance(tf, (int, float)):
                ctrl[d] = float(tf)
    probes, dropped = {}, set()
    for row in _load_recs():
        for rec in row.get("recs", []):
            rid, pat = rec.get("id"), rec.get("probe")
            if not isinstance(rid, str):
                continue
            if isinstance(pat, str) and pat:
                # Re-validate on READ. cmd_recs is not the only writer of recs.jsonl -
                # _upsert_jsonl applies no schema, so a hand edit, a backfill or a future
                # writer can put anything here. A consumer that trusts its store because
                # some producer validated once is the fail-open this axis cannot afford.
                v = valid_probe(pat)
                if v is not None:
                    probes[rid] = v
                else:
                    # Invalid but non-empty. It must land in `dropped`, not vanish: falling
                    # through would drop the rec from BOTH the scored set and the unmeasured
                    # count, which is the one outcome this instrument may never produce - a
                    # mechanism rec absent from its own tally.
                    dropped.add(rid)
            elif rec.get("probe_drop") in ("invalid", "no-baseline", "declared-none"):
                # All three are the same thing to a reader of the digest: a mechanism rec that
                # landed with no outcome measurement. Named separately in recs.jsonl so the
                # REASON is recoverable, counted together here so the gap cannot hide behind
                # its own taxonomy.
                dropped.add(rid)
    all_taken = _taken_recs(day)
    taken = {rid: t for rid, t in all_taken.items() if rid in probes}
    unmeasured = sum(1 for rid in dropped if rid in all_taken)
    if not taken:
        return ["PROBE-UNMEASURED n:%d" % unmeasured] if unmeasured else []

    # One corpus walk per DAY covering every probe that needs that day, rather than one walk
    # per probe: the windows of different recs overlap heavily.
    need = collections.defaultdict(dict)
    sides = {}
    for rid, tdate in taken.items():
        td = date.fromisoformat(tdate)
        before = [td - timedelta(days=k) for k in range(1, PROBE_WINDOW_DAYS + 1)]
        after = [td + timedelta(days=k) for k in range(1, PROBE_WINDOW_DAYS + 1)]
        before = [d for d in before if d.isoformat() in cov]
        after = [d for d in after if d.isoformat() in cov and d < day]
        sides[rid] = (before, after)
        for d in before + after:
            need[d][rid] = probes[rid]
    counted = {d: probe_counts(d, pats) for d, pats in sorted(need.items())}

    def matches(rid, days):
        return sum(counted[d].get(rid, 0) for d in days)

    def rate(rid, days):
        h = sum(cov[d.isoformat()] for d in days)
        return matches(rid, days) / h if h else None

    def control_rate(days):
        d_ok = [d for d in days if d.isoformat() in ctrl]
        h = sum(cov[d.isoformat()] for d in d_ok)
        return (sum(ctrl[d.isoformat()] for d in d_ok) / h if h else None), len(d_ok)

    def pct(before_v, after_v):
        if before_v is None or after_v is None or before_v == 0:
            return "n/a"
        return "%+.1f%%" % (100.0 * (after_v - before_v) / before_v)

    out = []
    for rid in sorted(taken):
        before, after = sides[rid]
        if not before or not after:
            continue   # nothing measured on one side at all: no line, not a zero
        rb, ra = rate(rid, before), rate(rid, after)
        if rb is None or ra is None:
            continue
        mb, ma = matches(rid, before), matches(rid, after)
        cb, nb_ctrl = control_rate(before)
        ca, na_ctrl = control_rate(after)
        ctrl_ok = (cb is not None and ca is not None
                   and nb_ctrl >= PROBE_MIN_WINDOW_DAYS and na_ctrl >= PROBE_MIN_WINDOW_DAYS)
        # One name per failing guard, in a fixed precedence. A bare `unmeasurable` cannot
        # tell a short window from a thin baseline from a missing control, and a reader who
        # cannot tell those apart cannot act on any of them.
        if len(before) < PROBE_MIN_WINDOW_DAYS or len(after) < PROBE_MIN_WINDOW_DAYS:
            reason = "short-window"
        elif mb < PROBE_MIN_BASELINE:
            reason = "thin-baseline"
        elif not ctrl_ok:
            reason = "no-control"
        else:
            reason = ""
        out.append("PROBE rec:%s taken:%s status:%s reason:%s before:%.2f after:%.2f "
                   "delta:%s control:%s control_status:%s matches_before:%d "
                   "matches_after:%d n_before:%d n_after:%d" %
                   (rid, taken[rid], "measured" if not reason else "unmeasurable",
                    reason or "-", rb, ra,
                    pct(rb, ra), pct(cb, ca), "ok" if ctrl_ok else "missing",
                    mb, ma, len(before), len(after)))
    out.append("PROBE-UNMEASURED n:%d" % unmeasured)
    return out


def cmd_probes(day):
    """Manual inspection only. The digest path is effectiveness_lines - see its tail."""
    for l in probe_lines(day):
        print(l)


def effectiveness_counts(day):
    """The four digest integers for `day`. Persisted onto the metrics row by cmd_metrics
    so the digest has a HISTORY - it was previously computed on demand and never written
    down, which made 'did an adopted fix actually work' unanswerable over time."""
    lines = effectiveness_lines(day)
    return {
        "eff_holding": sum(1 for x in lines if "status:holding" in x),
        "eff_recurred_after_fix": sum(1 for x in lines if "status:recurred-after-fix" in x),
        "eff_too_soon": sum(1 for x in lines if "status:too-soon" in x),
        "eff_chronic": sum(1 for x in lines if x.startswith("CHRONIC")),
        # No cache and no second walk: `lines` already holds the PROBE rows.
        "probes_measured": sum(1 for x in lines if x.startswith("PROBE rec:")
                               and "status:measured" in x),
        "probes_improved": sum(1 for x in lines if x.startswith("PROBE rec:")
                               and "status:measured" in x and " delta:-" in x),
        "probes_unmeasured": next((int(x.split("n:")[1]) for x in lines
                                   if x.startswith("PROBE-UNMEASURED")), 0),
    }


REC_ID_RE = re.compile(r"\d{4}-\d{2}-\d{2}#\d+[a-z]?")


def prior_rec_lines(day, window=PRIOR_REC_WINDOW):
    """The last `window` days of recommendation titles, one line per distinct rec id,
    most recent first. TRUSTED like the digest: read from recs.jsonl, whose summaries
    were already charset-neutralized by _sanitize_summary when they were stored (the
    report .md headings are NOT - putting raw model prose in the trusted-first zone is
    the injection surface the dimension-3 audit closed, so this reads the store).

    Why it exists: reduce could previously see only YESTERDAY's report plus the
    taken/chronic ids in the effectiveness digest, so a finding re-derived from fresh
    evidence three days later had nothing to collide with and got a fresh id with
    `repeat: false`. Measured 2026-08-26 over the 207-rec corpus: 15.5% of it is exactly
    that, and the loop's self-report tracks citation, not recurrence.

    NOT a similarity score, deliberately. Item D as planned called for a stdlib Jaccard
    ranking of the neighbours; re-measured on this corpus before writing it, the
    neighbour score has median 0.10 / p90 0.16 over 162 fresh-id recs, and pairs that
    ARE known restatements (2026-08-20#4 -> 2026-08-10#7) sit at 0.18 - inside the noise.
    The retro rewrites its titles daily, so a title-level Jaccard ranks nothing here.
    The corpus is supplied and the model is the matcher (prompts/reduce.md).
    """
    cutoff = (day - timedelta(days=window)).isoformat()
    latest = {}
    for r in sorted(_load_recs(), key=lambda r: r["report_date"]):
        d = r["report_date"]
        if not (cutoff <= d < day.isoformat()):
            continue
        for rec in r["recs"]:
            rid = rec.get("id")
            if isinstance(rid, str) and REC_ID_RE.fullmatch(rid):
                latest[rid] = (d, _sanitize_summary(str(rec.get("summary") or "")))
    return [f'PRIOR rec:{rid} last_seen:{d} "{s}"' for rid, (d, s)
            in sorted(latest.items(), key=lambda kv: (kv[1][0], kv[0]), reverse=True)]


def cmd_prior_recs(day):
    """Print the dedup corpus for `day`."""
    for line in prior_rec_lines(day):
        print(line)


# --- the day's repo/lander artifacts -----------------------------------------------
# Everything else this tool reads is a session TRANSCRIPT, so the loop can only see
# friction that surfaced as an agent-visible event - an error, a stall, a retraction the
# agent typed. Measured 2026-08-26 against a ground truth built from `git log` and
# `.claude/state/verify.log` for one day: of six friction classes present that day, the
# two recorded ONLY in repo/lander artifacts were named in 0 of 207 recommendations
# across all 30 reports. A merge that succeeds is silent and a `land-error` row is
# written by the lander, not narrated. This block is the missing channel.
MAX_ARTIFACT_REPOS = 5
MAX_REPEATED_SUBJECTS = 3
MIN_REPEATED_SUBJECT = 2
GIT_TIMEOUT_SECS = 20

# git in a checkout this tool did not choose is configuration-sensitive, so it gets a
# FRESH env rather than an edited copy of ours: no system/global config, no credential or
# terminal prompt, no inherited GIT_* (an inherited GIT_DIR from a hook overrides `-C` and
# has corrupted a caller's repo before). --no-pager for the same reason.
GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "HOME": "/nonexistent",
           "PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def _git(root, *args):
    """git stdout, or "" on any failure. Never raises: a repo that is missing, locked,
    hostile or slow degrades this block, it does not fail the day's report."""
    import subprocess
    try:
        r = subprocess.run(["git", "--no-pager", "-C", str(root), *args],
                           capture_output=True, text=True, env=GIT_ENV,
                           timeout=GIT_TIMEOUT_SECS)
    except Exception:
        return ""
    return r.stdout if r.returncode == 0 else ""


def _session_cwd(path, limit=50):
    """The session's working directory, from the transcript's own `cwd` field. Prefer a
    field the transcript already carries over decoding the dashed project-directory name,
    which is ambiguous for any path containing a dash."""
    for i, d in enumerate(iter_lines(path)):
        if i >= limit:
            break
        cwd = d.get("cwd")
        if isinstance(cwd, str) and cwd.startswith("/") and Path(cwd).is_dir():
            return cwd
    return None


def _primary_checkout(cwd):
    """The repo's PRIMARY checkout, or None if `cwd` is not in a git repo. A worktree
    session's cwd has the same history but its own gitignored `.claude/state/`, so the
    gate log lives in the primary; `git worktree list` names it first."""
    if not (Path(cwd) / ".git").exists():
        return None            # the .git-exists gate: never run git in a non-repo path
    for line in _git(cwd, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            root = line[len("worktree "):].strip()
            return root if Path(root).is_dir() else None
    return None


def _artifact_lines_from(name, log_text, verify_text, day):
    """Pure formatter: (repo name, `git log` output, verify.log text) -> digest lines.
    Split out from the gathering so the counting is testable without a real repo."""
    import collections
    out, subjects, merges, total = [], [], 0, 0
    for line in log_text.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        parents, subject = parts
        total += 1
        if len(parents.split()) > 1:
            merges += 1
        # Repeated subjects are scanned over ALL commits, merges included. The thrash
        # this is here to catch - five identical "regenerate metrics on the merged tree"
        # inside one minute on 2026-08-25 - lands on MERGE commits, because that is what
        # a lander produces. Restricting the scan to non-merges hid all five.
        subjects.append(subject)
    if total:
        pct = round(100 * merges / total)
        out.append(f"ARTIFACTS repo:{name} commits:{total} merges:{merges} "
                   f"non_merges:{total - merges} merge_pct:{pct}")
        for subj, n in collections.Counter(subjects).most_common(MAX_REPEATED_SUBJECTS):
            if n >= MIN_REPEATED_SUBJECT:
                out.append(f'ARTIFACTS repo:{name} repeated_subject:{n}x '
                           f'"{_sanitize_summary(subj)}"')
    verbs = collections.Counter()
    for line in verify_text.splitlines():
        f = line.split("\t")
        if len(f) > 1 and f[0].startswith(day.isoformat()):
            verbs[_sanitize_summary(f[1])[:40]] += 1
    if verbs:
        out.append(f"ARTIFACTS repo:{name} gatelog " +
                   " ".join(f"{v}:{n}" for v, n in sorted(verbs.items())))
    return out


def day_artifact_lines(day):
    """Deterministic per-repo artifact digest for `day` (TRUSTED, wrapper-computed).
    Repos are the PRIMARY checkouts of the directories the day's own sessions worked in -
    read from each transcript's `cwd`, gated on `.git` existing, capped at
    MAX_ARTIFACT_REPOS."""
    def none(reason):
        return [f"ARTIFACTS none: {reason}"]

    scan = REPORTS / "work" / day.isoformat() / "scan.json"
    if not scan.exists():
        return none("no scan.json for this date")
    try:
        sessions = json.loads(scan.read_text()).get("sessions", [])
    except json.JSONDecodeError:
        return none("scan.json is unreadable")
    roots = []
    for s in sessions:
        path = s.get("path")
        if not isinstance(path, str) or not Path(path).exists():
            continue
        cwd = _session_cwd(Path(path))
        root = _primary_checkout(cwd) if cwd else None
        if root and root not in roots:
            roots.append(root)
        if len(roots) >= MAX_ARTIFACT_REPOS:
            break
    if not roots:
        # Live this cannot happen (a session's cwd exists while its transcript does), but a
        # REPLAY over a day whose transcripts have since been pruned resolves no repo -
        # measured on 2026-08-08, which emitted an empty block indistinguishable from a
        # quiet day.
        return none("no repo resolved from the day's sessions "
                    "(transcripts pruned, or no session ran inside a git checkout)")
    out = []
    for root in sorted(roots):
        log = _git(root, "log", f"--since={day.isoformat()}T00:00:00",
                   f"--until={(day + timedelta(days=1)).isoformat()}T00:00:00",
                   "--format=%p%x09%s")
        verify = Path(root) / ".claude" / "state" / "verify.log"
        vtext = ""
        if verify.is_file():
            try:
                vtext = verify.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        out += _artifact_lines_from(_sanitize_summary(Path(root).name), log, vtext, day)
    return out


def cmd_day_artifacts(day):
    """Print the day's repo/lander artifact digest."""
    for line in day_artifact_lines(day):
        print(line)


# Memory-file limits, READ OUT OF THE CLAUDE CODE BUNDLE (2.1.258), not guessed:
#   var Wl="MEMORY.md", PD=200, B$=25000, AGt=4*B$, pCe=200, bJ=4096;
#   function E1e(e){let n=e.trim();return{trimmed:n,lineCount:_n(n,"\n")+1,byteCount:n.length}}
# so a memory file is measured on its TRIMMED content, and BOTH a char limit and a LINE limit
# apply. `AGt` is NOT a budget - it is the `She(e,0,AGt)` read cap - and treating it as one gives
# a meaningless 827%-of-total reading. `pCe` is the per-index-entry guidance the loader names as
# "index entries are too long".
#
# Why the retro carries this at all: the failure is SILENT BY DESIGN. On overflow the loader
# emits "Only part of it was loaded" and never says WHICH entries went dark, and it cuts the
# TAIL at a line boundary (`w.slice(0, I>0?I:B$)`), so in a newest-first index the oldest
# entries disappear first and nothing in a session reveals it. No transcript event exists for
# reduce to notice, which is exactly the class the day-artifacts block was added for.
MEM_CHAR_LIMIT = 25000
MEM_LINE_LIMIT = 200
MEM_ENTRY_CHARS = 200
MEM_WARN_AT = 0.85      # report a file this close to either limit, before it silently truncates


def memory_health_lines():
    """One line per memory file at risk of silent truncation, across ALL projects.

    Cross-project on purpose: per-project memory only loads inside its own repo, so a project
    the user has not opened lately is exactly where an overflow goes unnoticed longest. Healthy
    files emit nothing - this block is silent until something is actually near a limit.

    Takes NO `day`, unlike every other wrapper-computed block here, and that is a real
    limitation rather than an oversight: memory files are untracked and unversioned, with no
    per-day record to reconstruct from, so a BACKFILLED report carries TODAY's memory state and
    not the state on its own report date. Says so on the line itself via `as_of`, so a reader of
    an old report cannot mistake it for a measurement of that day.
    """
    out = []
    if not PROJECTS.is_dir():
        return out
    for mem in sorted(PROJECTS.glob("*/memory")):
        for path in sorted(mem.glob("*.md")):
            try:
                body = path.read_text(errors="replace").strip()
            except OSError:
                continue        # unreadable memory file must never fail the retro
            chars, lines = len(body), body.count("\n") + 1
            is_index = path.name == "MEMORY.md"
            long_entries = sum(1 for ln in body.split("\n") if len(ln) > MEM_ENTRY_CHARS)
            over = chars > MEM_CHAR_LIMIT or lines > MEM_LINE_LIMIT
            near = (chars >= MEM_CHAR_LIMIT * MEM_WARN_AT
                    or lines >= MEM_LINE_LIMIT * MEM_WARN_AT)
            if not (over or near or (is_index and long_entries)):
                continue
            out.append(
                "MEMORY-HEALTH %s project:%s file:%s chars:%d/%d(%.0f%%) lines:%d/%d "
                "over_%dch_lines:%d as_of:%s" % (
                    "OVER" if over else "NEAR", mem.parent.name, path.name,
                    chars, MEM_CHAR_LIMIT, 100.0 * chars / MEM_CHAR_LIMIT,
                    lines, MEM_LINE_LIMIT, MEM_ENTRY_CHARS, long_entries,
                    date.today().isoformat()))
    return out


def cmd_memory_health():
    for line in memory_health_lines():
        print(line)


def cmd_effectiveness(day):
    """Print the deterministic fix-effectiveness digest for `day`."""
    for line in effectiveness_lines(day):
        print(line)


MIN_TREND_COVERAGE_HOURS = 1.0  # below this, a day's rate is too little signal to trust
MIN_TREND_SPLIT_DAYS = 4        # fewer usable days than this: report the table, skip the split


def _load_metrics():
    path = REPORTS / "metrics.jsonl"
    out = []
    if path.exists():
        for l in path.read_text().splitlines():
            try:
                r = json.loads(l)
            except json.JSONDecodeError:
                continue
            if isinstance(r.get("date"), str):
                out.append(r)
    return sorted(out, key=lambda r: r["date"])


def cmd_trends(days=14):
    """Rate-normalized, coverage-aware quality trend over the last `days` metrics.jsonl
    rows, plus an effectiveness-digest snapshot as of the most recent stamped report.
    Read-only - never writes recs.jsonl/actions-log.md/metrics.jsonl.

    `tool_failures_per_hour` / `tool_calls_per_hour` are the primary axes (already
    rate-normalized, so a busy day and a quiet day are comparable - a raw count conflates
    volume with quality). `errors_per_hour` is the pre-2026-08-26 UNDIVIDED counter - it
    also sums policy blocks and harness outages, so a fall in it is equally consistent
    with "the guardrails were relaxed" and "the API had a better day"; it is printed for
    continuity with old rows and is never a friction axis. A day with < MIN_TREND_COVERAGE_HOURS of coverage (no sessions,
    or an all-retro-stub day) is marked `low-coverage` in the table and EXCLUDED from the
    trend split, never silently averaged in as if it were a great quiet day.
    `top_friction`/friction_score is printed too but is the noisier axis - it tracks how
    messy what was ATTEMPTED was, not whether the underlying process is improving; the
    effectiveness digest (does a landed fix's friction actually stop recurring) is the
    stronger signal for that and is why it's snapshotted here alongside the raw trend."""
    rows = _load_metrics()[-days:]
    if not rows:
        print("no metrics.jsonl history yet")
        return
    print(f"{'date':<12} {'sess':>4} {'cov_h':>6} {'tools/hr':>9} {'tf/hr':>7} "
          f"{'err/hr':>7} {'friction':>9} {'gates':>6} {'max_gate_wait':>13} "
          f"{'human_wait%':>12}")
    usable = []
    for r in rows:
        ch = r.get("coverage_hours")
        low = ch is None or ch < MIN_TREND_COVERAGE_HOURS
        hw, w, b = r.get("human_wait_secs") or 0, r.get("work_secs") or 0, r.get("blocked_secs") or 0
        ml, idl = r.get("model_latency_secs") or 0, r.get("idle_secs") or 0
        total = hw + w + b + ml + idl
        hw_pct = round(100 * hw / total, 1) if total else None

        def cell(v, width):
            return f"{v if v is not None else '-':>{width}}"

        print(f"{r['date']:<12} {r.get('sessions', 0):>4} {cell(ch, 6)} "
              f"{cell(r.get('tool_calls_per_hour'), 9)} "
              f"{cell(r.get('tool_failures_per_hour'), 7)} "
              f"{cell(r.get('errors_per_hour'), 7)} "
              f"{r.get('top_friction', 0):>9} {r.get('gate_calls', 0):>6} "
              f"{r.get('max_gate_wait_secs', 0):>13} {cell(hw_pct, 12)}"
              f"{' low-coverage' if low else ''}")
        if not low:
            usable.append(r)

    def avg(rs, key):
        vals = [x[key] for x in rs if x.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    if len(usable) >= MIN_TREND_SPLIT_DAYS:
        half = len(usable) // 2
        first, second = usable[:half], usable[half:]
        for key, label_ in (("tool_failures_per_hour", "tool_failures/hr (primary)"),
                            ("errors_per_hour", "errors/hr (legacy mixed counter)"),
                            ("tool_calls_per_hour", "tool_calls/hr"),
                            ("top_friction", "friction_score (noisier axis)")):
            # A key missing from part of the window changed DEFINITION inside it (added,
            # or split off an older counter), so the two halves measure different things
            # and the printed direction is an artifact of WHEN the counter landed. Refuse
            # the split instead of printing a number nobody can read. Measured 2026-09-02
            # on this machine's own history: `tool_failures_per_hour` existed on 7 of 30
            # usable days, while this function printed `errors/hr (primary) ... (down)`
            # off the undivided counter that scan_file's own comments say must not be read
            # as friction - across both the 08-17 wall-clock re-bucketing and the 08-26
            # error split.
            have = sum(1 for r in usable if r.get(key) is not None)
            if have < len(usable):
                print(f"trend {label_}: NOT COMPARABLE - present on {have} of "
                      f"{len(usable)} usable days (counter added or redefined mid-window)")
                continue
            a, b_ = avg(first, key), avg(second, key)
            if a is not None and b_ is not None:
                direction = "down" if b_ < a else ("up" if b_ > a else "flat")
                print(f"trend {label_}: first-half avg {a} -> second-half avg {b_} ({direction})")
    else:
        print(f"only {len(usable)} usable (non-low-coverage) day(s) in range "
              f"({MIN_TREND_SPLIT_DAYS} needed) - too few for a trend split")

    reports = sorted(p.stem for p in REPORTS.glob("????-??-??.md")
                     if COMPLETE_MARKER in p.read_text(encoding="utf-8", errors="replace"))
    if reports:
        latest = date.fromisoformat(reports[-1])
        c = effectiveness_counts(latest)
        holding, recurred = c["eff_holding"], c["eff_recurred_after_fix"]
        too_soon, chronic = c["eff_too_soon"], c["eff_chronic"]
        print(f"effectiveness as of {latest.isoformat()}: holding={holding} "
              f"recurred-after-fix={recurred} too-soon={too_soon} CHRONIC={chronic}")
        if chronic:
            print("  a CHRONIC rec recurring across weeks despite landing is the strongest "
                  "single signal something isn't actually improving - see that day's report's "
                  "Fix effectiveness section for which one(s)")
        pm, pi = c.get("probes_measured") or 0, c.get("probes_improved") or 0
        pu = c.get("probes_unmeasured") or 0
        if pm:
            print(f"probes: {pi}/{pm} measured fixes reduced their own friction signature"
                  + (f" ({pu} more landed unmeasured)" if pu else ""))
        else:
            print(f"probes: none measured yet ({pu} taken mechanism recs carry no usable "
                  f"probe; a measurable one needs {PROBE_MIN_BASELINE}+ baseline matches "
                  f"and {PROBE_MIN_WINDOW_DAYS} covered days each side)")


def selftest():
    import tempfile
    ts = "2026-07-08T12:00:00.000Z"
    lines = [
        {"type": "user", "timestamp": ts, "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant", "timestamp": ts, "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "assistant", "timestamp": ts, "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "timestamp": ts, "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "boom"}]}},
        {"type": "user", "timestamp": ts, "message": {"content": [
            {"type": "text", "text": "[Request interrupted by user]"}]}},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        for l in lines:
            f.write(json.dumps(l) + "\n")
        f.write('{"partial')  # simulated active writer
        p = Path(f.name)
    # Emitted timestamps must be LOCAL, matching day_bounds' local windowing. A UTC-stamped
    # transcript time rendered as UTC is unreconcilable against verify.log / ps / the user's
    # own recollection; before 2026-08-06 every quoted extract time was offset by the UTC
    # offset (7h on PDT). Assert against the zone's own conversion rather than a fixed
    # expected string, so this passes wherever it runs.
    _utc_noon = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
    assert hhmmss(_utc_noon) == _utc_noon.astimezone().strftime("%H:%M:%S")
    if _utc_noon.astimezone().utcoffset() != timedelta(0):
        assert hhmmss(_utc_noon) != "12:00:00", "emitted stamp is still UTC, not local"

    start, end = day_bounds(date(2026, 7, 8))
    s = scan_file(p, start, end)
    assert s["user_turns"] == 2, s
    assert s["assistant_turns"] == 2, s
    assert s["tools"] == {"Bash": 2}, s
    assert s["retries"] == 1, s
    assert s["errors"] == 1 and s["error_samples"] == ["boom"], s
    assert s["interrupts"] == 1, s
    assert friction(s) > 0
    # intentional sandbox probe (errors only, no thrash) suppresses to 0
    probe = {"errors": 2, "interrupts": 0, "retries": 0, "denials": 0,
             "user_turns": 1, "assistant_turns": 1,
             "error_samples": ["EACCES: operation not permitted, open '/etc/x'"]}
    assert friction(probe) == 0.0, friction(probe)
    # a real error sample is NOT suppressed even with no retries/interrupts
    probe["error_samples"] = ["TypeError: undefined is not a function"]
    assert friction(probe) > 0, friction(probe)
    p.unlink()
    # --- verbal friction: corrections + nudges (score friction with ZERO tool errors) ---
    # regex precision (the boundary + drop-list cases codex R1 flagged)
    assert CORRECTION_RE.search("no, that is wrong")            # leading-\b 'no,' matches
    assert not CORRECTION_RE.search("stop the server after the test")   # bare verb dropped
    assert not CORRECTION_RE.search("revert to the previous API")       # bare verb dropped
    assert CORRECTION_RE.search("you forgot the import")
    # self_retractions: assistant-voice retraction, distinct from user-voice CORRECTION_RE
    assert RETRACTION_RE.search("Correction: `.dev-loop.conf` **does** exist")
    assert RETRACTION_RE.search("Correction to what I just said: I misread my own process tree")
    assert RETRACTION_RE.search("My test was wrong - zsh doesn't word-split unquoted $c")
    assert RETRACTION_RE.search("I was wrong about the trap firing")
    assert RETRACTION_RE.search("I misread the process tree")
    assert RETRACTION_RE.search("Actually, I need to correct that.")
    # the ^|\n anchor must survive multi-block joining (assistant text is "\n"-joined, not " ")
    assert RETRACTION_RE.search("Here is the analysis.\nCorrection: the log is destroyed.")
    assert not RETRACTION_RE.search("Here is the analysis. Correction-free summary follows.")
    # --- 2026-08-06 widening: one assert per added clause, each a real 2026-08-05 string ---
    assert RETRACTION_RE.search("One correction to my earlier summary: it holds 11 project dirs")
    assert RETRACTION_RE.search("One correction I owe you: I predicted LOW for this diff")
    assert RETRACTION_RE.search("my published number was wrong")
    assert RETRACTION_RE.search("The provenance check is right; my ad-hoc-entry test was wrong")
    assert RETRACTION_RE.search("that's the class-fix mistake, on me")
    assert RETRACTION_RE.search("I conflated \"needs headroom below the observed rate\" with the threshold")
    assert RETRACTION_RE.search("hh113 is 13.24/home-month, not 11.25 - I understated it by 15%")
    assert RETRACTION_RE.search("The flake root cause is sharper than I assumed")
    assert RETRACTION_RE.search("The control line is confounded - it counts messages already dropped")
    assert RETRACTION_RE.search("My design's 6.1 table names it wrongly")
    assert RETRACTION_RE.search("my last line about it still being up is now stale")
    # ...and the clauses deliberately NOT taken must stay un-matched: a bare statement of method,
    # and a bare "on me" that is not a self-attribution of error.
    assert not RETRACTION_RE.search("I assumed a 200-day span for this run, per the config.")
    assert not RETRACTION_RE.search("The dependency on me is what makes this serial.")
    # the two real 2026-08-05 false positives that a bare `correction to` produced: prose ABOUT
    # corrections is not a retraction. These are what took precision to 0.92 before narrowing.
    assert not RETRACTION_RE.search("every correction to one has to be checked against the other")
    assert not RETRACTION_RE.search("every correction to one needs checking against the other")
    # --- 2026-08-10 widening: one assert per added clause, each a VERBATIM 2026-08-09 string
    # from the hand-labelled session a59acf3f or the held-out session 8d7a8a76 ---
    assert RETRACTION_RE.search("The starvation test is hollow - the mutation passes.")
    assert RETRACTION_RE.search("test_the_source_actually_sets_body_truncated passes pre-fix")
    assert RETRACTION_RE.search('so my "identical for contacts" claim may be wrong')
    assert RETRACTION_RE.search("My arithmetic, not the code: the prefix is 22 chars, not 21")
    assert RETRACTION_RE.search("the same vacuity class again, in a subtler form I missed")
    assert RETRACTION_RE.search("and it caught something I'd have shipped")
    assert RETRACTION_RE.search("my contact fixture never asserted anything")
    assert RETRACTION_RE.search("so that half was vacuous in the failing direction")
    assert RETRACTION_RE.search("My 'id-keyed, so no collision' claim was an unverified assertion")
    assert RETRACTION_RE.search("it refutes a claim I made: latest_signal is an overwrite path")
    assert RETRACTION_RE.search("Deleting the hollow test and correcting the overclaiming comment")
    assert RETRACTION_RE.search("my own mutation evidence is worth checking, not trusting")
    assert RETRACTION_RE.search("My commit 64c6b3d1 does state that measurement - and I never ran it")
    assert RETRACTION_RE.search("I asserted *absence after the fence* without ever proving presence")
    assert RETRACTION_RE.search("my mutation evidence was real and still didn't prove what I claimed")
    assert RETRACTION_RE.search("Four codex rounds, each finding a real layer I'd missed")
    # ...and the 2026-08-10 narrowings must hold. The bare `(is|was) now stale` matched a
    # fact about an ARTIFACT - the single false positive in the 128-block labelled set.
    assert not RETRACTION_RE.search("The verdict record keyed to 1c17fb1f is now stale")
    assert RETRACTION_RE.search("my last line about it still being up is now stale")
    # A bare `asserted absence` matched running-tally RESTATEMENTS, which report a prior
    # retraction rather than making one. Require first person.
    assert not RETRACTION_RE.search("Six tests this session asserted absence without establishing presence")
    # --- quote/citation stripping: reported evidence is not a live retraction ---
    assert not RETRACTION_RE.search(strip_quoted("> my published number was wrong"))
    assert not RETRACTION_RE.search(strip_quoted("```\nfix: stop overclaiming the guard\n```"))
    assert not RETRACTION_RE.search(strip_quoted("at 21:31:27 the agent said the test is hollow"))
    # ...but stripping must NOT eat the whole block after a blockquote. A global re.S made
    # the `>` branch swallow every following line, silently deleting a real retraction.
    assert RETRACTION_RE.search(strip_quoted(
        "> codex: the CAS proves nothing here\n\nI asserted *absence* without proving presence."))
    # the >=5-token gap that the 2026-08-04 probe rejected must STILL be rejected
    assert not RETRACTION_RE.search("My test covers the case where the input was wrong on purpose")
    # must NOT fire on ordinary prose that merely contains the words
    assert not RETRACTION_RE.search("The test asserts the old value, which was wrong before the fix.")
    assert not RETRACTION_RE.search("If the predicate is wrong the guard fails open.")
    assert not RETRACTION_RE.search("The correction factor is 1.5 in the calibration table.")
    # user-voice stays with CORRECTION_RE, not this one
    assert not RETRACTION_RE.search("You forgot the import")
    assert NUDGE_RE.match("continue") and NUDGE_RE.match("proceed")
    assert not NUDGE_RE.match("2pm") and not NUDGE_RE.match("ok")
    assert not NUDGE_RE.match("keep going and also check the logs")
    # a probe WITH a correction is no longer suppressed to 0 (R1 finding 1)
    probe2 = {"errors": 2, "interrupts": 0, "retries": 0, "denials": 0,
              "corrections": 1, "nudges": 0, "user_turns": 2, "assistant_turns": 1,
              "error_samples": ["EACCES: operation not permitted, open '/x'"]}
    assert friction(probe2) > 0, friction(probe2)
    # scan_file end-to-end: a correction + two nudges, interleaved with assistant turns,
    # and NO errors/interrupts/retries/denials - so friction(sv) can ONLY be > 0 via the
    # new corrections/nudges weight terms. This is the valid weight go-red: it FAILS on
    # pre-change code (weighted == 0) and passes only once §4's terms are added. (An
    # interrupt/error in this case would mask the weights - see codex R2 finding 1.)
    vturns = ["please refactor the auth module", "no, that's wrong",
              "stop the server after the test", "revert to the previous API",
              "2pm", "ok", "continue", "proceed",
              "keep going and also check the logs"]
    vlines = []
    for txt in vturns:
        vlines.append({"type": "user", "timestamp": ts,
                       "message": {"content": [{"type": "text", "text": txt}]}})
        vlines.append({"type": "assistant", "timestamp": ts,
                       "message": {"content": [{"type": "text", "text": "on it"}]}})
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fv:
        for l in vlines:
            fv.write(json.dumps(l) + "\n")
        pv = Path(fv.name)
    sv = scan_file(pv, start, end)
    assert sv["corrections"] == 1, sv          # only "no, that's wrong"
    assert sv["nudges"] == 2, sv               # only "continue" + "proceed"
    assert sv["errors"] == 0 and sv["interrupts"] == 0 and sv["retries"] == 0, sv
    assert friction(sv) > 0, sv                # THE FIX: pure-verbal friction now scores
    pv.unlink()
    # interrupt marker is a user text turn but must NOT count as correction/nudge
    # (it is already counted as an interrupt) - kept as its own case so the weight
    # go-red above stays interrupt-free.
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fi:
        fi.write(json.dumps({"type": "user", "timestamp": ts, "message": {"content": [
            {"type": "text", "text": "[Request interrupted by user]"}]}}) + "\n")
        pi = Path(fi.name)
    si = scan_file(pi, start, end)
    assert si["interrupts"] == 1 and si["corrections"] == 0 and si["nudges"] == 0, si
    pi.unlink()
    # --- codex/agy gate-latency detection (name-gated: only job DISPATCHERS count) ---
    assert is_gate_call("Bash", '{"command": "node .../agy-companion.mjs adversarial-review --wait"}')
    assert is_gate_call("Skill", '{"skill": "dev-loop-core:review-gate"}')
    assert is_gate_call("Skill", '{"skill": "gated-land:gate-loop"}')
    assert is_gate_call("Bash", '{"command": "codex exec resume abc"}')
    assert is_gate_call("Agent", '{"subagent_type": "codex:codex-rescue", "prompt": "re-gate"}')
    assert is_gate_call("Agent", '{"subagent_type": "agy:agy-rescue", "prompt": "review this"}')
    # ...and an SDD/Explore/general subagent whose BRIEF merely mentions gate vocabulary is not a
    # gate dispatch. Charging its whole lane to gate_wait_secs is what produced 3ece6495's
    # "63-minute gate round" on 2026-08-25 (it was one sdd-implementer lane).
    assert not is_gate_call("Agent",
                            '{"subagent_type": "sdd-implementer", "prompt": "when green run gate-loop"}')
    assert not is_gate_call("Agent",
                            '{"subagent_type": "explore-readonly", "prompt": "analyze gate-loop cost"}')
    assert not is_gate_call("Agent",
                            '{"subagent_type": "general-purpose", "prompt": "audit codex exec config"}')
    assert not is_gate_call("Agent", '{"prompt": "run lander.sh prepare"}')   # no subagent_type
    assert not is_gate_call("Bash", '{"command": "git status --short"}')
    # the tightening: a QUESTION / READ that only MENTIONS a gate must NOT count as gate time
    assert not is_gate_call("AskUserQuestion",
                            '{"questions": [{"question": "override the codex review-gate no-ship?"}]}')
    assert not is_gate_call("Read", '{"file_path": "codex-adversarial-review-notes.md"}')
    assert not is_gate_call("Edit", '{"file_path": "ship_it.py"}')   # bare 'ship' in a name != gate
    # a Bash READ of a gate script/skill file must not count either - the same "merely
    # mentions a gate" exclusion GATE_TOOLS already gives Read/AskUserQuestion, applied to
    # Bash's own command content (session-retro dimension-1 audit, 2026-08-20: this class
    # inflated 3f02f940's gate_calls to 51/gate_wait_secs to ~3.06h vs a ~1.6h hand-count).
    assert not is_gate_call("Bash", '{"command": "sed -n \'48,62p\' scripts/lander.sh"}')
    assert not is_gate_call("Bash", '{"command": "grep -n review-gate SKILL.md"}')
    assert not is_gate_call("Bash", '{"command": "cat plugins/gated-land/skills/gate-loop/SKILL.md"}')
    assert not is_gate_call("Bash", '{"command": "head -20 skills/review-gate/scripts/x.mjs"}')
    # real invocations must still count
    assert is_gate_call("Bash", '{"command": "bash scripts/lander.sh prepare"}')
    assert is_gate_call("Bash", '{"command": "\\"$CLAUDE_PLUGIN_ROOT/engine/lander.sh\\" commit a b"}')
    # scan_file attributes the wait AFTER a gate call to gate_wait_secs (a 40s block here stands
    # in for a real 25-min codex wait). FAILS on pre-change code (no gate_* keys).
    glines = [
        {"type": "assistant", "timestamp": "2026-07-08T12:00:00.000Z", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "id": "g",
             "input": {"command": "node codex-companion.mjs adversarial-review --base main --wait"}}]}},
        {"type": "user", "timestamp": "2026-07-08T12:00:40.000Z", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "g", "content": "Verdict: needs-attention"}]}},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fg:
        for l in glines:
            fg.write(json.dumps(l) + "\n")
        pg = Path(fg.name)
    sg = scan_file(pg, start, end)
    assert sg["gate_calls"] == 1, sg
    assert sg["gate_wait_secs"] == 40.0 and sg["max_gate_wait_secs"] == 40.0, sg
    assert sg["gaps"][0]["after"] == "gate:Bash", sg["gaps"]
    pg.unlink()
    # --- increment 1: time/cost extraction ---
    # distinct timestamps + usage + a matched tool pair + two consecutive identical errors
    def ev(t, **kw):
        return dict(timestamp=f"2026-07-08T12:00:{t:02d}.000Z", **kw)
    tlines = [
        ev(0, type="user", message={"content": [{"type": "text", "text": "hi"}]}),
        ev(0, type="assistant", message={"usage": {
            "input_tokens": 100, "output_tokens": 10,
            "cache_creation_input_tokens": 50, "cache_read_input_tokens": 200},
            "content": [{"type": "tool_use", "name": "Bash", "id": "a", "input": {"command": "ls"}}]}),
        ev(5, type="user", message={"content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "ok"}]}),
        ev(5, type="assistant", message={"content": [
            {"type": "tool_use", "name": "Grep", "id": "b", "input": {"pattern": "x"}}]}),
        ev(10, type="user", message={"content": [
            {"type": "tool_result", "tool_use_id": "b", "is_error": True, "content": "ENOENT rg"}]}),
        ev(10, type="assistant", message={"content": [
            {"type": "tool_use", "name": "Grep", "id": "c", "input": {"pattern": "y"}}]}),
        ev(40, type="user", message={"content": [
            {"type": "tool_result", "tool_use_id": "c", "is_error": True, "content": "ENOENT rg"}]}),
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f2:
        for l in tlines:
            f2.write(json.dumps(l) + "\n")
        p2 = Path(f2.name)
    s2 = scan_file(p2, start, end)
    assert (s2["in_tokens"], s2["out_tokens"], s2["cache_write_tokens"], s2["cache_read_tokens"]) \
        == (100, 10, 50, 200), s2
    assert s2["tool_secs"] == {"Bash": 5.0, "Grep": 35.0}, s2   # 5 + (5 then 30)
    assert s2["gaps"] and s2["gaps"][0]["secs"] == 30.0 and "Grep" in s2["gaps"][0]["after"], s2["gaps"]
    assert s2["repeated_error_runs"] == [{"snippet": "ENOENT rg", "count": 2}], s2["repeated_error_runs"]
    dur = (parse_ts(s2["last_ts"]) - parse_ts(s2["first_ts"])).total_seconds()
    assert dur == 40.0, dur
    p2.unlink()
    # --- wall-clock partition + background-job correlation ---
    # A background gate job: dispatched at t=1, completion notification at t=203. The agent
    # ends its turn at t=3 (blocked on the job, NOT on a human), gets the notification, ends
    # its turn again at t=204 with nothing running (now genuinely waiting on a human).
    def evt(sec, **kw):
        mi, se = divmod(sec, 60)
        return dict(timestamp=f"2026-07-08T12:{mi:02d}:{se:02d}.000Z", **kw)
    notif = ("<task-notification>\n<task-id>j1</task-id>\n"
             "<tool-use-id>bg1</tool-use-id>\n<status>completed</status>\n</task-notification>")
    wlines = [
        evt(0, type="user", message={"content": [{"type": "text", "text": "go"}]}),
        evt(1, type="assistant", message={"content": [
            {"type": "tool_use", "name": "Bash", "id": "bg1",
             "input": {"command": "bash lander.sh prepare", "run_in_background": True}}]}),
        evt(2, type="user", message={"content": [
            {"type": "tool_result", "tool_use_id": "bg1", "content": "started"}]}),
        evt(3, type="assistant", message={"content": [{"type": "text", "text": "dispatched"}]}),
        evt(100, type="system", content="a meta row must not terminate a gap"),
        evt(203, type="user", message={"content": notif}),
        evt(204, type="assistant", message={"content": [{"type": "text", "text": "done"}]}),
        evt(304, type="user", message={"content": [{"type": "text", "text": "next"}]}),
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f3:
        for l in wlines:
            f3.write(json.dumps(l) + "\n")
        p3 = Path(f3.name)
    w = scan_file(p3, start, end)
    assert (w["bg_jobs"], w["bg_job_secs"], w["bg_blocked_secs"]) == (1, 202.0, 202.0), w
    # blocked = 1s tool latency + 200s turn-ended-while-job-in-flight; human_wait = the 100s
    # after the job finished; work = the one tool_result->assistant thinking gap; the two
    # user->assistant gaps (t=0->1, t=203->204) are generation, not work.
    assert (w["blocked_secs"], w["human_wait_secs"], w["work_secs"]) == (201.0, 100.0, 1.0), w
    assert (w["model_latency_secs"], w["idle_secs"]) == (2.0, 0.0), w
    span = (parse_ts(w["last_ts"]) - parse_ts(w["first_ts"])).total_seconds()
    assert sum(w[k] for k in ("blocked_secs", "human_wait_secs", "work_secs",
                              "model_latency_secs", "idle_secs")) == span == 304.0, (w, span)
    # the `system` row at t=100 must not steal the gap's attribution from the assistant turn
    assert (w["gaps"][0]["secs"], w["gaps"][0]["after"]) == (200.0, "assistant"), w["gaps"]
    # a gate dispatched into the background: its wait ends at the notification, not at the
    # tool_result that returns immediately (1s tool gap + 202s job)
    assert (w["gate_calls"], w["gate_wait_secs"]) == (1, 203.0), w
    p3.unlink()
    # --- an AskUserQuestion dispatch whose answer comes 20 min later is human_wait, not
    # blocked (session-retro dimension-1 audit, 2026-08-20: the tool_use:*/gate: check ran
    # BEFORE the close=="user" check, so this launders human wait into blocked_secs) ---
    auq_lines = [
        evt(0, type="user", message={"content": [{"type": "text", "text": "go"}]}),
        evt(1, type="assistant", message={"content": [
            {"type": "tool_use", "name": "AskUserQuestion", "id": "q1",
             "input": {"questions": [{"question": "which option?"}]}}]}),
        evt(1201, type="user", message={"content": [
            {"type": "tool_result", "tool_use_id": "q1", "content": "picked option A"}]}),
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fq:
        for l in auq_lines:
            fq.write(json.dumps(l) + "\n")
        pq = Path(fq.name)
    sq = scan_file(pq, start, end)
    assert sq["human_wait_secs"] == 1200.0, sq
    assert sq["blocked_secs"] == 0.0, sq
    pq.unlink()
    # --- pair-invariant: a gap is classified by the events that BOUND it (rec:2026-08-15#7) ---
    # Both halves of the 2026-08-16 defect, on one fixture with NO background job in flight:
    #   t=0 user -> t=2h assistant : a pending turn nobody answered for 2h. Was `work` (100% of
    #                               that day's work_secs came from one such span). Must be `idle`.
    #   t=2h assistant -> +10s assistant : generation between two assistant rows. Was
    #                               `human_wait` (nothing was running), and no human was waited on.
    def evtd(sec, **kw):
        h, r = divmod(sec, 3600)
        mi, se = divmod(r, 60)
        return dict(timestamp=f"2026-07-08T{12 + h:02d}:{mi:02d}:{se:02d}.000Z", **kw)
    plines = [
        evtd(0, type="user", message={"content": [{"type": "text", "text": "go"}]}),
        evtd(7200, type="assistant", message={"content": [{"type": "text", "text": "late"}]}),
        evtd(7210, type="assistant", message={"content": [{"type": "text", "text": "more"}]}),
        evtd(7215, type="user", message={"content": [{"type": "text", "text": "next"}]}),
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f4:
        for l in plines:
            f4.write(json.dumps(l) + "\n")
        p4 = Path(f4.name)
    pr = scan_file(p4, start, end)
    assert (pr["idle_secs"], pr["work_secs"]) == (7200.0, 0.0), pr
    assert (pr["model_latency_secs"], pr["human_wait_secs"]) == (10.0, 5.0), pr
    assert pr["blocked_secs"] == 0.0, pr
    assert sum(pr[k] for k in ("blocked_secs", "human_wait_secs", "work_secs",
                               "model_latency_secs", "idle_secs")) == 7215.0, pr
    p4.unlink()
    # --- middle-clipping: the END of a long message must survive (rec:2026-08-16#3) ---
    # End-truncation cut six of nine 2026-08-16 extracts mid-word and every analyst
    # correctly refused to assess the tail it could not see.
    long_msg = "HEAD" + ("x" * 2000) + "CONCLUSION"
    tally = [0]
    c = clip(long_msg, 800, tally)
    assert c.startswith("HEAD") and c.endswith("CONCLUSION"), c
    assert "[... 1214 chars elided ...]" in c, c
    assert tally == [1214], tally
    assert len(long_msg) - tally[0] == 800, (len(long_msg), tally)   # kept exactly the cap
    assert clip("short", 800, tally) == "short" and tally == [1214], tally  # no-op, no tally
    ev = {"timestamp": "2026-07-08T12:00:00.000Z", "type": "assistant",
          "message": {"content": [{"type": "text", "text": long_msg}]}}
    o, t2 = [], [0]
    prune_event(ev, start, end, o, t2)
    assert o[0].endswith("CONCLUSION") and t2 == [1214], (o, t2)
    # --- trajectory clipping: the TAIL of a long session must survive the byte cap ---
    # The 2026-08-13 extract stopped at [08:38:29] and the 20:51 `worktree remove --force`
    # that destroyed two hours of work was never in the retro's input at all.
    traj = [f"[12:00:{i:02d}] ASSISTANT: {'y' * 100}" for i in range(50)]
    traj[0] = "[12:00:00] USER: FIRST"
    traj[-1] = "[12:00:49] tool_use Bash: git worktree remove --force LAST"
    fits = list(traj)
    assert trajectory_clip(fits, 1_000_000) == 0 and fits == traj, fits  # no-op under cap
    cut = list(traj)
    dropped = trajectory_clip(cut, 2000)
    assert dropped > 0, dropped
    assert cut[0] == traj[0], cut[0]                    # head kept
    assert cut[-1] == traj[-1], cut[-1]                 # TAIL kept - this is the whole fix
    assert sum(len(x) + 1 for x in cut) <= 2000, sum(len(x) + 1 for x in cut)
    marker = [x for x in cut if "elided from the MIDDLE" in x]
    assert len(marker) == 1 and str(dropped) in marker[0], (marker, dropped)
    kept = sum(len(x) + 1 for x in traj) - dropped
    assert kept == sum(len(x) + 1 for x in cut) - len(marker[0]) - 1, (kept, cut)
    # degenerate: a cap that admits nothing still keeps the marker and drops everything
    none_fits = list(traj)
    assert trajectory_clip(none_fits, 0) == sum(len(x) + 1 for x in traj), none_fits
    assert len(none_fits) == 1 and "50 whole events" in none_fits[0], none_fits
    # extract slot allocation: the 2 reserved slots go to the longest sessions BELOW the
    # friction cut - never to short stubs that merely happen to score 0 (the 2026-08-06
    # defect: two <5min sessions took 2 of 8 slots while 9.1h and 7.4h sessions were cut).
    def _s(sid, fs, dur, project="p", tools=..., user_turns=3):
        # tools/user_turns are real record fields; is_retro_stub indexes them directly on
        # purpose, so a record missing them raises instead of quietly reading as a stub.
        # `...` not None as the default: {} is falsy and an `or` default would silently
        # turn every "no tools" fixture back into a tool-using one.
        return {"project": project, "session": sid, "friction_score": fs,
                "duration_secs": dur,
                "tools": {"Bash": 1} if tools is ... else tools,
                "user_turns": user_turns}
    day_sessions = ([_s(f"f{i}", 9 - i, 1000) for i in range(8)]     # the friction top-8
                    + [_s("long", 0.5, 30000), _s("stub1", 0, 60), _s("stub2", 0, 57)])
    picked, reserved = pick_sessions(day_sessions, 8)
    ids = [s["session"] for s in picked]
    assert len(picked) == 8, ids
    assert "long" in ids and [s["session"] for s in reserved] == ["long"], (ids, reserved)
    assert "stub1" not in ids and "stub2" not in ids, ids
    # `--top` lowers, never raises; the ceiling is what the env var moves
    assert analysis_cap(3) == 3 and analysis_cap(50) == MAX_ANALYSIS_SESSIONS
    assert analysis_cap(MAX_ANALYSIS_SESSIONS + 1) == MAX_ANALYSIS_SESSIONS
    # The coverage denominator is the ELIGIBLE set - every session that could have taken
    # a slot, including the quiet ones the cap never reaches. Only the retro's own calls
    # are excluded (they are not work). These two "stub" fixtures are ordinary quiet
    # sessions, so they stay in the denominator: 8 of 11 analysed, 72.7%.
    assert len(eligible_sessions(day_sessions)) == 11, len(eligible_sessions(day_sessions))
    assert coverage_pct(len(picked), len(eligible_sessions(day_sessions))) == 72.7
    assert coverage_pct(1, 2) == 50.0 and coverage_pct(0, 0) is None
    # a retro stub IS excluded - it is the pipeline analysing itself, not a session
    assert len(eligible_sessions(day_sessions + [_s("r", 0, 100, project="-", tools={},
                                                    user_turns=1)])) == 11
    # floor: on a quiet day nothing below SLOW_RESERVE_MIN_SECS may be reserved
    _, quiet = pick_sessions([_s("f0", 5, 100), _s("stub1", 0, 60)], 8)
    assert quiet == [], quiet
    # retro stubs never take an analysis slot (rec:2026-08-16#2). The discriminator is the
    # record, not the text: a REAL session with no tools and one turn is still analysed, and
    # a `-` session that DID use tools is still analysed.
    retro = _s("retro1", 3, 100, project="-", tools={}, user_turns=1)
    real_quiet = _s("realq", 3, 100, project="scratch", tools={}, user_turns=1)
    dash_worked = _s("dashw", 3, 100, project="-", tools={"Bash": 2}, user_turns=1)
    assert is_retro_stub(retro) and not is_retro_stub(real_quiet), (retro, real_quiet)
    assert not is_retro_stub(dash_worked), dash_worked
    ids = [s["session"] for s in pick_sessions([retro, real_quiet, dash_worked], 8)[0]]
    assert ids == ["realq", "dashw"], ids
    # a day of nothing but stubs yields zero extracts, not a fallback pick
    assert pick_sessions([retro, _s("retro2", 0, 90, project="-", tools={}, user_turns=1)],
                         8) == ([], []), "stub-only day must produce no extracts"
    # ...but the daily REPORT-WRITER call is kept: same record shape, distinguished only by
    # the retro's own reduce prompt (a first-party string, not transcript-derived content).
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f5:
        f5.write(json.dumps({"timestamp": "2026-07-08T12:00:00.000Z", "type": "user",
                             "message": {"content": REDUCE_ANCHOR + ". Appended below..."}})
                 + "\n")
        p5 = Path(f5.name)
    reducer = _s("reducer", 3, 100, project="-", tools={}, user_turns=1)
    reducer["path"] = str(p5)
    mapper = dict(retro, path=str(p5.parent / "does-not-exist.jsonl"))
    assert not is_retro_stub(reducer), "the report-writer call must survive the filter"
    assert is_retro_stub(mapper), "a map call must not"
    assert [s["session"] for s in pick_sessions([reducer, mapper], 8)[0]] == ["reducer"]
    p5.unlink()

    # metrics upsert is idempotent per date (atomic replace-by-date)
    global REPORTS
    real_reports = REPORTS
    with tempfile.TemporaryDirectory() as td:
        REPORTS = Path(td)
        work = REPORTS / "work" / "2026-07-08"
        work.mkdir(parents=True)
        (work / "scan.json").write_text(json.dumps({"sessions": [
            {"tools": {"Bash": 2}, "errors": 1, "interrupts": 0, "retries": 1,
             "denials": 0, "friction_score": 12.5, "total_tokens": 300,
             "cache_write_tokens": 50, "duration_secs": 40.0,
             "gate_wait_secs": 30.0, "max_gate_wait_secs": 20.0},
            {"tools": {}, "errors": 0, "interrupts": 0, "retries": 0, "denials": 0,
             "friction_score": 1.0, "gate_wait_secs": 12.0,
             "max_gate_wait_secs": 12.0}]}))
        cmd_metrics(date(2026, 7, 8))
        cmd_metrics(date(2026, 7, 8))
        lines = (REPORTS / "metrics.jsonl").read_text().splitlines()
        assert len(lines) == 1, lines
        m = json.loads(lines[0])
        assert m["top_friction"] == 12.5
        assert (m["tokens"], m["cache_write_tokens"], m["max_duration_secs"]) == (300, 50, 40.0), m
        # gate wait is SUMMED across sessions while its max is a MAX - two sessions of 30s and 12s
        # are 42s of gating whose worst single round was 20s. Summing the maxes, or maxing the
        # sums, both read 42/42 here and neither answers the question the other one asks.
        assert (m["gate_wait_secs"], m["max_gate_wait_secs"]) == (42.0, 20.0), m
        # a pre-migration scan.json (no token fields) must not KeyError -> .get defaults
        (work / "scan.json").write_text(json.dumps({"sessions": [
            {"tools": {}, "errors": 0, "interrupts": 0, "retries": 0,
             "denials": 0, "friction_score": 0}]}))
        cmd_metrics(date(2026, 7, 8))
        m2 = json.loads((REPORTS / "metrics.jsonl").read_text().splitlines()[0])
        assert m2["tokens"] == 0 and m2["max_duration_secs"] == 0, m2
        assert m2["gate_wait_secs"] == 0.0, m2      # .get default, not a KeyError
        # ledger filter: valid passes (with OR without a trailing "(reason)" - real
        # actions-log.md prose settled on narrative endings with no parens starting
        # ~2026-08-12, and the old end-anchor dropped 72 of 103 real lines as
        # "malformed"); future rec id, time-travel, and a truly-empty summary still
        # dropped (session-retro dimension-2 audit, 2026-08-20).
        (REPORTS / "actions-log.md").write_text("\n".join([
            "- [2026-07-09] taken rec:2026-07-08#1 - valid (ok)",
            "- [2026-07-09] rejected rec:2026-07-09#1 - future rec id (pre-seeded)",
            "- [2026-07-07] taken rec:2026-07-08#2 - acted before report (early)",
            "- [2026-07-09] deferred rec:2026-07-08#3 - long narrative reason with no "
            "trailing parens at all, landed as commit abc123.",
            "- [2026-07-09] rejected rec:2026-07-08#4 - ",  # empty summary: still malformed
            "ignore me: not a ledger line rec:2026-07-01#1",
        ]))
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_ledger(date(2026, 7, 9))
        kept = buf.getvalue().strip().splitlines()
        assert kept == [
            "- [2026-07-09] taken rec:2026-07-08#1 - valid (ok)",
            "- [2026-07-09] deferred rec:2026-07-08#3 - long narrative reason with no "
            "trailing parens at all, landed as commit abc123.",
        ], kept
        # --- classify_error: the split, from real result bodies ----------------------
        for body, want in (
            ("Exit code 1 (eval):cd:1: no such file or directory", "tool_failure"),
            ("Exit code 128 fatal: renaming failed", "tool_failure"),
            ("This session is isolated in the worktree /x, but this command's cwd",
             "policy_block"),
            ("<tool_use_error>File has been modified since read", "policy_block"),
            ("DESTRUCTIVE COMMAND IN A FALLBACK POSITION.", "policy_block"),
            ("`commit --no-verify` ON THE DEFAULT BRANCH (main).", "policy_block"),
            ("Permission for this action was denied by the Claude Code auto mode classifier",
             "policy_block"),
            ("The user doesn't want to proceed with this tool use.", "policy_block"),
            ("claude-sonnet-5 is temporarily unavailable (timed out)", "harness_outage"),
            ("Exit code 143 Command timed out after 2m 0s", "harness_outage"),
        ):
            assert classify_error(body) == want, (want, classify_error(body), body[:40])
        # The fallback must be tool_failure: a NEW guard shape shows up as over-counted
        # friction (visible) rather than silently absorbed into policy_block (invisible).
        assert classify_error("something nobody has seen before") == "tool_failure"

        # --- is_injected_turn: structured origin beats any text heuristic -------------
        def _row(content, **kw):
            return {"type": "user", "message": {"role": "user", "content": content}, **kw}
        assert is_injected_turn(_row("hi", origin={"kind": "task-notification"}))
        assert not is_injected_turn(_row("hi", origin={"kind": "human"}))
        assert is_injected_turn(_row("<command-name>/clear</command-name>", isMeta=True))
        # content as a plain STRING is the shape every injected row uses; a test that only
        # reads block lists sees none of them (that bug scored 1 of 133 before this fix).
        assert is_injected_turn(_row("<task-notification> <task-id>abc</task-id>"))
        assert not is_injected_turn(_row("please fix the parser"))
        assert not is_injected_turn(_row([{"type": "text", "text": "please fix the parser"}]))
        # origin.kind wins over the text fallback, in BOTH directions
        assert not is_injected_turn(_row("<task-notification> x", origin={"kind": "human"}))

        # --- scan.json totals: the number reduce is told to copy must exist ------------
        # Asserted against an INDEPENDENT re-sum, not against itself, and the degenerate
        # case is pinned: an empty day must give 0, not a missing key.
        _sess = [{"tools": {"Bash": 3, "Read": 2}, "errors": 1, "interrupts": 0,
                  "retries": 1, "denials": 0},
                 {"tools": {"Bash": 4}, "errors": 2, "interrupts": 1, "retries": 0,
                  "denials": 1}]
        _t = _scan_totals(_sess)
        assert _t == {"tool_calls": 9, "errors": 3, "interrupts": 1, "retries": 1,
                      "denials": 1, "sessions": 2}, _t
        assert _scan_totals([])["tool_calls"] == 0, _scan_totals([])

        # --- widened LEDGER_RE: both parenthetical positions + alpha rec-id suffix ---
        # Each of these shapes occurs in the real actions-log and was SILENTLY dropped
        # before 2026-08-26 (31 of 41 drops, 11 of them "taken").
        for good in (
            "- [2026-07-09] deferred (never started) rec:2026-07-08#1 - qualifier after verb",
            "- [2026-07-09] taken rec:2026-07-08#1 (part 2 only) - qualifier after rec id",
            "- [2026-07-09] taken rec:2026-07-08#4a - alpha suffix on the rec number",
            "- [2026-07-09] taken rec:2026-07-08#1 (re-scopes rec:2026-07-07#8) - nested rec ref",
        ):
            assert LEDGER_RE.match(good), good
        # ...and the shapes that must STILL be rejected, so widening did not open the gate.
        for bad in (
            "- [2026-07-09] taken rec:2026-07-08#1 - ",          # empty summary
            "- [2026-07-09] maybe rec:2026-07-08#1 - bad verb",
            "- [2026-07-09] taken rec:2026-07-08 - no rec number",
            "- [not-a-date] taken rec:2026-07-08#1 - bad line date",
        ):
            assert not LEDGER_RE.match(bad), bad

        # --- ledger_drops: nothing is dropped without being counted -------------------
        (REPORTS / "actions-log.md").write_text(
            "- [2026-07-09] taken rec:2026-07-08#1 (partial) - parses now\n"
            "- [2026-07-09] FINDING (not a rec) - a note, not a disposition\n"
            "- [2026-07-09] NOTE rec:2026-07-08#1 - also not a disposition\n"
            "- [2026-07-09] taken rule:some-slug - a different id namespace\n"
            "- [2026-07-09] considered rule:some-other - a `rule:` line with no real verb\n"
            "- [2026-07-09] garbled line with no shape at all\n")
        malformed, non_disp = ledger_drops()
        # `taken rule:<slug>` is a REAL disposition, so it is parsed rather than filed as
        # prose: non_disp drops 3 -> 2. Asserted positively too, because the count alone is
        # also satisfied by a regex that merely stops recognising the line at all.
        # 2 malformed: the garbled line, and `considered rule:` - a non-verb, which the old
        # `\w+ rule:` arm filed as benign prose. Anything carrying a rule id but no valid verb
        # is malformed, not a deliberate non-disposition.
        assert (malformed, non_disp) == (2, 2), (malformed, non_disp)
        # the `rule:` line PARSES (so it is not malformed, and cmd_ledger shows it) but is not
        # judged: it has no recs.jsonl entry, so a status for it could never be anything but
        # a permanent `holding`.
        assert set(_taken_recs()) == {"2026-07-08#1"}, _taken_recs()
        assert LEDGER_RE.match("- [2026-07-09] taken rule:some-slug - a different id namespace")

        # --- slug rec ids parse, and a bare date still does not ----------------------
        for good, rid in (
            ("- [2026-08-27] taken rec:loop-detection-latency - slug id, no report date", "rec:loop-detection-latency"),
            ("- [2026-08-26] rejected rule:bash-command-shape-hook - a rule adoption", "rule:bash-command-shape-hook"),
            ("- [2026-08-26] superseded rec:2026-08-15#4 - subsumed by a later rec", "2026-08-15#4"),
            ("- [2026-09-01] applied rec:2026-08-15#4 - written, effect unmeasured", "2026-08-15#4"),
        ):
            m = LEDGER_RE.match(good)
            assert m, good
            assert _ledger_rid(m) == rid, (good, _ledger_rid(m))

        # --- a LATER non-taken verb retracts an earlier taken -------------------------
        (REPORTS / "actions-log.md").write_text(
            "- [2026-07-10] taken rec:2026-07-08#1 - adopted\n"
            "- [2026-07-12] rejected rec:2026-07-08#1 - reverted after measuring\n"
            "- [2026-07-10] taken rec:2026-07-08#2 - adopted and kept\n")
        assert set(_taken_recs()) == {"2026-07-08#2"}, _taken_recs()

        # --- restatement clusters: recurrence follows the writer's own backref --------
        hist = [
            {"report_date": "2026-07-08", "recs": [{"id": "2026-07-08#1", "summary": "the original finding", "dedup": ""}]},
            {"report_date": "2026-07-20", "recs": [{"id": "2026-07-20#3", "summary": "REPEAT of rec:2026-07-08#1", "dedup": ""}]},
            {"report_date": "2026-07-21", "recs": [{"id": "2026-07-21#9", "summary": "unrelated", "dedup": ""}]},
        ]
        cl = _cluster_recs(hist)
        assert cl["2026-07-20#3"] == "2026-07-08#1", cl   # newer id folds into the older
        assert cl["2026-07-21#9"] == "2026-07-21#9", cl   # no backref -> its own cluster
        # A backref under NEGATION is not an edge. Measured on the real corpus 2026-08-27:
        # 5 of 20 edges were "distinct from rec:<id>", i.e. the writer explicitly saying this
        # is NOT the same finding, and reading them as restatements produced 3 false
        # recurred-after-fix verdicts (14 -> 11 once guarded).
        neg = _cluster_recs([
            {"report_date": "2026-07-08", "recs": [{"id": "2026-07-08#1", "summary": "original", "dedup": ""}]},
            {"report_date": "2026-07-09", "recs": [{"id": "2026-07-09#2", "summary": "new rule, distinct from rec:2026-07-08#1", "dedup": ""}]},
        ])
        assert neg["2026-07-09#2"] == "2026-07-09#2", neg
        # ...and the window is per-reference, so a mixed line keeps the asserted edge and
        # drops only the negated one.
        mix = _cluster_recs([
            {"report_date": "2026-07-08", "recs": [{"id": "2026-07-08#1", "summary": "a", "dedup": ""}]},
            {"report_date": "2026-07-08", "recs": [{"id": "2026-07-08#5", "summary": "b", "dedup": ""}]},
            {"report_date": "2026-07-09", "recs": [{"id": "2026-07-09#3",
             "summary": "REPEAT of rec:2026-07-08#1 and it is distinct from rec:2026-07-08#5", "dedup": ""}]},
        ])
        assert mix["2026-07-09#3"] == "2026-07-08#1", mix
        assert mix["2026-07-08#5"] == "2026-07-08#5", mix

        # --- _taken_recs is bounded by `day`, which is what makes a backfill honest ----
        (REPORTS / "actions-log.md").write_text(
            "- [2026-07-09] taken rec:2026-07-08#1 - early\n"
            "- [2026-07-20] taken rec:2026-07-08#2 - late\n")
        assert set(_taken_recs(date(2026, 7, 10))) == {"2026-07-08#1"}, _taken_recs(date(2026, 7, 10))
        assert set(_taken_recs()) == {"2026-07-08#1", "2026-07-08#2"}, _taken_recs()

        # --- effectiveness_counts returns the persisted integers ----------------------
        # Pinned as an exact set on purpose: every key here is written onto the metrics row
        # by cmd_metrics, so a silently added or renamed one changes the schema of a file
        # with two years of history and no migration.
        counts = effectiveness_counts(date(2026, 7, 20))
        assert set(counts) == {"eff_holding", "eff_recurred_after_fix",
                               "eff_too_soon", "eff_chronic",
                               "probes_measured", "probes_improved",
                               "probes_unmeasured"}, counts
        assert all(isinstance(v, int) for v in counts.values()), counts
        (REPORTS / "actions-log.md").unlink()

        # missing-dates must not strand a gap an out-of-order completion jumped over:
        # a newer date (D-1) complete while an older one (D-2) is still missing.
        for f in REPORTS.glob("????-??-??.md"):
            f.unlink()
        today = date(2026, 7, 14)
        for iso in ("2026-07-11", "2026-07-13"):  # 07-12 deliberately missing
            (REPORTS / f"{iso}.md").write_text("x\n" + COMPLETE_MARKER + "\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_missing_dates(today)
        emitted = buf.getvalue().split()
        assert "2026-07-12" in emitted, emitted          # the stranded gap is revisited
        assert "2026-07-13" not in emitted, emitted       # complete dates are skipped
        # --- recs.jsonl extraction (fix-effectiveness increment) ---
        (REPORTS / "2026-07-10.md").write_text(
            "# Session retro 2026-07-10\n"
            "### [rec: 2026-07-08#1] tighten the guard **REPEAT**\n"
            "### [rec: 2026-07-10#2] new thing\n"
            "junk [rec: 2026-00-99#9] non-calendar date must be dropped\n"
            "<script>[rec: 2026-07-10#3] x</script>\n" + COMPLETE_MARKER + "\n")
        cmd_recs(date(2026, 7, 10))
        cmd_recs(date(2026, 7, 10))
        rrows = (REPORTS / "recs.jsonl").read_text().splitlines()
        assert len(rrows) == 1, rrows
        rec = json.loads(rrows[0])
        ids = {r["id"]: r for r in rec["recs"]}
        assert set(ids) == {"2026-07-08#1", "2026-07-10#2", "2026-07-10#3"}, ids
        assert ids["2026-07-08#1"]["repeat"] is True, ids
        assert ids["2026-07-10#2"]["repeat"] is False, ids
        assert "<" not in ids["2026-07-10#3"]["summary"], ids
        # a canonical "**[rec: ...] ...**" heading must win over an EARLIER casual
        # cross-reference on the same id, regardless of line order in the report - else the
        # cross-reference's trailing fragment becomes the stored summary (confirmed live in
        # 2026-08-19's recs.jsonl: session-retro dimension-2 audit, 2026-08-20).
        (REPORTS / "2026-07-11.md").write_text(
            "# Session retro 2026-07-11\n"
            "## Global rules health\n"
            "See [rec: 2026-07-11#1] below for the sharpening.\n"
            "## Recommendations\n"
            "**[rec: 2026-07-11#1] [claude-md] REPEAT tighten the sweep rule**\n"
            "Dedup: distinct from rec:2026-07-08#1 - that one was the guard, this is "
            "the sweep\n"
            "Evidence: ...\n" + COMPLETE_MARKER + "\n")
        cmd_recs(date(2026, 7, 11))
        rrow2_line = next(l for l in (REPORTS / "recs.jsonl").read_text().splitlines()
                          if json.loads(l)["report_date"] == "2026-07-11")
        rid2 = {r["id"]: r for r in json.loads(rrow2_line)["recs"]}["2026-07-11#1"]
        assert rid2["summary"].startswith("[claude-md]"), rid2
        assert "below for the sharpening" not in rid2["summary"], rid2
        # the writer's dedup decision is stored with the rec, so "fresh id, no dedup
        # line" is countable in-band instead of only by re-clustering by hand (item D)
        assert rid2["dedup"].startswith("distinct from rec:2026-07-08#1"), rid2
        # --- effectiveness digest ---
        import io as _io
        from contextlib import redirect_stdout as _rso
        (REPORTS / "recs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
            {"report_date": "2026-07-05", "recs": [{"id": "2026-07-05#1", "repeat": False, "summary": "a"}]},
            {"report_date": "2026-07-06", "recs": [{"id": "2026-07-05#1", "repeat": True, "summary": "a"},
                                                    {"id": "2026-07-06#2", "repeat": False, "summary": "b"}]},
            {"report_date": "2026-07-09", "recs": [{"id": "2026-07-05#1", "repeat": True, "summary": "a"},
                                                    {"id": "2026-07-09#3", "repeat": False, "summary": "c"}]},
        ]))
        (REPORTS / "actions-log.md").write_text("\n".join([
            "- [2026-07-07] taken rec:2026-07-05#1 - fixed a (landed)",
            "- [2026-07-08] taken rec:2026-07-06#2 - fixed b (landed)",
            "- [2026-07-09] taken rec:2026-07-09#3 - fixed c (landed)",
        ]))
        buf = _io.StringIO()
        with _rso(buf):
            cmd_effectiveness(date(2026, 7, 10))
        eff = buf.getvalue()
        assert "EFFECTIVENESS rec:2026-07-05#1" in eff and "status:recurred-after-fix" in eff, eff
        assert "EFFECTIVENESS rec:2026-07-06#2" in eff and "status:holding" in eff, eff
        assert "EFFECTIVENESS rec:2026-07-09#3" in eff and "status:too-soon" in eff, eff
        assert '"a"' in eff, eff
        assert "CHRONIC rec:2026-07-05#1" in eff, eff
        assert "CHRONIC rec:2026-07-06#2" not in eff, eff
        # --- prior-rec dedup corpus (item D): the window reduce dedups against --------
        # Most-recent-first by LAST seen date, one line per distinct id, and the day
        # itself is excluded (its report is what is being written).
        assert prior_rec_lines(date(2026, 7, 10), window=21) == [
            'PRIOR rec:2026-07-09#3 last_seen:2026-07-09 "c"',
            'PRIOR rec:2026-07-05#1 last_seen:2026-07-09 "a"',
            'PRIOR rec:2026-07-06#2 last_seen:2026-07-06 "b"',
        ], prior_rec_lines(date(2026, 7, 10), window=21)
        # the window is load-bearing: 3 days back drops the 07-06 row entirely
        assert [l.split()[1] for l in prior_rec_lines(date(2026, 7, 10), window=3)] == [
            "rec:2026-07-09#3", "rec:2026-07-05#1"], prior_rec_lines(date(2026, 7, 10), window=3)
        # --- day-artifacts (item H): the counting, without needing a real repo ---------
        # `%p` (parent list) is the merge discriminator, not the subject prefix - a
        # non-merge commit whose subject starts with "Merge" must not count as one.
        log = "\n".join([
            "aaa\tfix: real work",
            "bbb ccc\tdocs: regenerate metrics on the merged tree",
            "ddd eee\tdocs: regenerate metrics on the merged tree",
            "fff\tdocs: regenerate metrics",
            "ggg\tdocs: regenerate metrics",
            "hhh\tMerge-sort the queue (NOT a merge commit: one parent)",
            "malformed-line-with-no-tab",
        ])
        verify = "\n".join([
            "2026-07-10T01:00:00Z\tgateloop-block\tabc\tdetail",
            "2026-07-10T02:00:00Z\tgateloop-block\tabc\tdetail",
            "2026-07-10T03:00:00Z\tland-error\tabc\tMERGE CONFLICT",
            "2026-07-09T03:00:00Z\tgateloop-capout\tabc\tother day, must not count",
            "not a verify line at all",
        ])
        al = _artifact_lines_from("myrepo", log, verify, date(2026, 7, 10))
        assert al[0] == ("ARTIFACTS repo:myrepo commits:6 merges:2 non_merges:4 "
                         "merge_pct:33"), al
        # BOTH repeats are reported, and the merge-commit one is the point: on
        # 2026-08-25 all five identical "regenerate metrics on the merged tree" commits
        # were merges, so a non-merge-only scan reported zero repeated subjects.
        assert sorted(al[1:3]) == [
            'ARTIFACTS repo:myrepo repeated_subject:2x "docs: regenerate metrics on the '
            'merged tree"',
            'ARTIFACTS repo:myrepo repeated_subject:2x "docs: regenerate metrics"',
        ], al
        assert len(al) == 4, al          # 2 repeats + the split + the gate log; singletons dropped
        assert al[3] == "ARTIFACTS repo:myrepo gatelog gateloop-block:2 land-error:1", al[3]
        # empty in, empty out - a repo with no commits and no gate log emits nothing at all
        assert _artifact_lines_from("myrepo", "", "", date(2026, 7, 10)) == []
        # Every "nothing to report" exit must SAY so. The roots-empty case is the one
        # measured live (a replayed day whose transcripts were pruned); a session row whose
        # transcript no longer exists reproduces it exactly.
        assert day_artifact_lines(date(2026, 7, 10)) == [
            "ARTIFACTS none: no scan.json for this date"], day_artifact_lines(date(2026, 7, 10))
        _w = REPORTS / "work" / "2026-07-10"
        _w.mkdir(parents=True, exist_ok=True)
        (_w / "scan.json").write_text(json.dumps(
            {"sessions": [{"path": str(REPORTS / "gone.jsonl")}]}))
        assert day_artifact_lines(date(2026, 7, 10)) == [
            "ARTIFACTS none: no repo resolved from the day's sessions "
            "(transcripts pruned, or no session ran inside a git checkout)"], \
            day_artifact_lines(date(2026, 7, 10))
        (_w / "scan.json").write_text("{ not json")
        assert day_artifact_lines(date(2026, 7, 10)) == [
            "ARTIFACTS none: scan.json is unreadable"], day_artifact_lines(date(2026, 7, 10))
        # The .git gate must stop git from ever RUNNING in a path that is not a repo.
        # Returning None is not enough - _primary_checkout returns None anyway when git
        # errors, so only "the subprocess never happened" pins the gate.
        _real_git, git_calls = _git, []
        def _spy_git(root, *a):
            git_calls.append(str(root))
            return ""
        globals()["_git"] = _spy_git
        try:
            assert _primary_checkout(str(REPORTS)) is None
            assert git_calls == [], git_calls
        finally:
            globals()["_git"] = _real_git
        # a rec first proposed TODAY is not its own prior
        assert all("2026-07-09#3" not in l for l in prior_rec_lines(date(2026, 7, 9))), \
            prior_rec_lines(date(2026, 7, 9))
        (REPORTS / "actions-log.md").write_text("\n".join([
            "- [2026-07-07] taken rec:2026-07-05#1 - fixed bug #42 in parser (landed)",
            "- [2026-99-99] taken rec:2026-07-08#7 - non-calendar line date (bad)",
        ]))
        assert _taken_recs() == {"2026-07-05#1": "2026-07-07"}, _taken_recs()
        with _rso(_io.StringIO()):
            cmd_effectiveness(date(2026, 7, 10))

        # --- cmd_trends: rate-normalized split + low-coverage exclusion ---
        # explicit, self-contained fixture - does not depend on residue from the tests
        # above, so reordering them can't silently break this one.
        for f in REPORTS.glob("????-??-??.md"):
            f.unlink()
        def _write_metrics(tf_from):
            """Fixture writer. `tf_from` = first date carrying tool_failures_per_hour,
            so one call makes the counter partial (added mid-window, like the real
            2026-08-26 error split) and another makes it cover every usable day."""
            for iso, epr, tph, fric in [
                ("2026-07-20", 10.0, 100.0, 30.0), ("2026-07-21", 12.0, 110.0, 32.0),
                ("2026-07-22", 8.0, 90.0, 28.0), ("2026-07-23", 10.0, 100.0, 30.0),
                ("2026-07-24", 2.0, 50.0, 10.0), ("2026-07-25", 3.0, 55.0, 12.0),
                ("2026-07-26", 1.0, 45.0, 8.0), ("2026-07-27", 2.0, 50.0, 10.0),
            ]:
                row = {
                    "date": iso, "sessions": 5, "coverage_hours": 5.0,
                    "errors_per_hour": epr, "tool_calls_per_hour": tph,
                    "top_friction": fric,
                    "gate_calls": 1, "max_gate_wait_secs": 1.0, "work_secs": 10.0,
                    "human_wait_secs": 10.0, "blocked_secs": 0.0,
                    "model_latency_secs": 0.0, "idle_secs": 0.0,
                }
                if iso >= tf_from:
                    row["tool_failures_per_hour"] = epr / 2
                _upsert_jsonl(REPORTS / "metrics.jsonl", row, key="date")

        _write_metrics("2026-07-24")   # counter present on the last 4 of 8 days
        # a low-coverage day in the SAME window must not pollute the trend average
        _upsert_jsonl(REPORTS / "metrics.jsonl", {
            "date": "2026-07-19", "sessions": 0, "coverage_hours": None,
            "errors_per_hour": None, "tool_calls_per_hour": None, "top_friction": 0,
            "gate_calls": 0, "max_gate_wait_secs": 0,
        }, key="date")
        (REPORTS / "2026-07-27.md").write_text("x\n" + COMPLETE_MARKER + "\n")
        (REPORTS / "recs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
            {"report_date": "2026-07-20", "recs": [{"id": "2026-07-20#1", "repeat": False, "summary": "x"}]},
        ]))
        (REPORTS / "actions-log.md").write_text(
            "- [2026-07-20] taken rec:2026-07-20#1 - fixed x (landed)\n")
        buf_t = _io.StringIO()
        with _rso(buf_t):
            cmd_trends(days=9)
        out_t = buf_t.getvalue()
        # tool_failures_per_hour is on the last 4 rows only -> the split must be
        # REFUSED for it, while the fully-present keys still trend.
        assert ("trend tool_failures/hr (primary): NOT COMPARABLE - present on 4 of 8 "
                "usable days (counter added or redefined mid-window)") in out_t, out_t
        assert ("trend errors/hr (legacy mixed counter): first-half avg 10.0 -> "
                "second-half avg 2.0 (down)") in out_t, out_t
        assert "trend tool_calls/hr: first-half avg 100.0 -> second-half avg 50.0 (down)" in out_t, out_t
        assert ("trend friction_score (noisier axis): first-half avg 30.0 -> "
                "second-half avg 10.0 (down)") in out_t, out_t
        row_2026_07_19 = next(l for l in out_t.splitlines() if l.startswith("2026-07-19"))
        assert "low-coverage" in row_2026_07_19, row_2026_07_19
        assert "effectiveness as of 2026-07-27: holding=1" in out_t, out_t

        # Same window, same numbers, counter now on EVERY usable day -> the refusal
        # lifts and the primary axis trends. Without this half the assert above passes
        # for a cmd_trends that simply never trends tool_failures/hr at all.
        _write_metrics("2026-07-20")
        buf_t2 = _io.StringIO()
        with _rso(buf_t2):
            cmd_trends(days=9)
        out_t2 = buf_t2.getvalue()
        assert ("trend tool_failures/hr (primary): first-half avg 5.0 -> "
                "second-half avg 1.0 (down)") in out_t2, out_t2
        assert "NOT COMPARABLE" not in out_t2, out_t2
    REPORTS = real_reports

    # --- still_active must name only sessions that HAVE a record (2026-08-10) ---
    # A live file whose events all belong to a LATER day is correctly excluded from
    # sessions[]; naming it in still_active told a reader its figures were missing
    # from the totals, and the 2026-08-09 report opened with exactly that wrong
    # caveat. Two files, both freshly mtimed so both look "active": one with in-day
    # events, one with next-day events only.
    global PROJECTS
    real_projects = PROJECTS
    try:
        with tempfile.TemporaryDirectory() as td:
            PROJECTS = Path(td)
            proj = PROJECTS / "-proj"
            proj.mkdir()
            def _row(stamp):
                return json.dumps({"type": "user", "timestamp": stamp,
                                   "message": {"content": [{"type": "text", "text": "x"}]}}) + "\n"
            (proj / "inday.jsonl").write_text(_row("2026-07-08T12:00:00.000Z"))
            (proj / "nextday.jsonl").write_text(_row("2026-07-09T12:00:00.000Z"))
            r = scan_day(date(2026, 7, 8))
            ids = {s["session"] for s in r["sessions"]}
            assert ids == {"inday"}, ids
            # the invariant the fix exists to hold
            for name in r["still_active"]:
                assert name.split("/", 1)[1] in ids, (name, ids)
            assert r["still_active"] == ["-proj/inday"], r["still_active"]
            assert r["active_no_in_day_events"] == 1, r["active_no_in_day_events"]
            # cmd_scan must WRITE totals, not merely be able to compute them. Asserting
            # _scan_totals directly stays GREEN when the call site is deleted - which is
            # exactly how the first version of this test passed under mutation.
            _real_reports = REPORTS
            try:
                with tempfile.TemporaryDirectory() as rd:
                    REPORTS = Path(rd)
                    cmd_scan(date(2026, 7, 8))
                    on_disk = json.loads(
                        (REPORTS / "work" / "2026-07-08" / "scan.json").read_text())
                    assert "totals" in on_disk, list(on_disk)
                    assert on_disk["totals"]["sessions"] == 1, on_disk["totals"]
                    assert on_disk["totals"]["tool_calls"] == 0, on_disk["totals"]
                    # cmd_extract must WRITE the coverage back, not merely compute it
                    cmd_extract(date(2026, 7, 8), 8)
                    after = json.loads(
                        (REPORTS / "work" / "2026-07-08" / "scan.json").read_text())
                    assert after["totals"]["sessions_eligible"] == 1, after["totals"]
                    assert after["totals"]["sessions_analyzed"] == 0, after["totals"]
                    assert after["totals"]["analysis_coverage_pct"] == 0.0, after["totals"]
            finally:
                REPORTS = _real_reports
    finally:
        PROJECTS = real_projects
    # --- probe counter (Task 1) ---
    import shutil
    pdir = Path(tempfile.mkdtemp()) / "probes"
    (pdir / "-proj-a").mkdir(parents=True)
    pts = "2026-07-08T12:00:00.000Z"
    outside = "2026-07-09T12:00:00.000Z"
    prows = [
        {"type": "assistant", "timestamp": pts, "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "git -C /other/repo log"}}]}},
        {"type": "user", "timestamp": pts, "message": {"content": [
            {"type": "tool_result", "is_error": True,
             "content": "this session is isolated in the worktree X"}]}},
        {"type": "user", "timestamp": outside, "message": {"content": [
            {"type": "tool_result", "is_error": True,
             "content": "this session is isolated in the worktree X"}]}},
    ]
    with (pdir / "-proj-a" / "s1.jsonl").open("w") as f:
        for r in prows:
            f.write(json.dumps(r) + "\n")
    _real_projects_t1 = globals()["PROJECTS"]
    globals()["PROJECTS"] = pdir
    try:
        got = probe_counts(date(2026, 7, 8), {
            "a#1": "Isolated In The Worktree",     # case-insensitive
            "a#2": "git -C /other",
            "a#3": "nothing matches this",
            "a#4": "short",                        # under 8 chars -> dropped entirely
            "a#5": "x" * (MAX_PROBE_LEN + 1),      # over length -> dropped entirely
        })
    finally:
        globals()["PROJECTS"] = _real_projects_t1
        shutil.rmtree(pdir.parent, ignore_errors=True)
    # Counts are per EVENT, and the 07-09 row must not leak into the 07-08 window.
    assert got == {"a#1": 1, "a#2": 1, "a#3": 0}, got
    assert valid_probe("short") is None
    assert valid_probe("x" * (MAX_PROBE_LEN + 1)) is None
    assert valid_probe("   ") is None
    assert valid_probe("caf\u00e9 au lait") is None        # non-ASCII rejected
    # A literal is stored verbatim: regex metacharacters are DATA, never syntax, so a probe
    # for a shell fragment cannot be reinterpreted or fail to compile.
    assert valid_probe("rc=$?  (") == "rc=$?  ("
    # a#2 lives ONLY in a tool_use `input`, never in any `text` block: raw_text_of cannot
    # see it. A probe for a command shape is the contract's own first example, so this
    # assertion is load-bearing, not an edge case.
    assert got["a#2"] == 1, got
    # Friction text does not live in message.content. Measured on the real corpus, one error
    # token appeared under `error`, a TOP-LEVEL `content`, `toolUseResult` and `attachment` as
    # well - 62 of 83 rows were in fields a message.content reader cannot see. Each field here
    # is one that was actually missed.
    mf = Path(tempfile.mkdtemp()) / "mf"
    (mf / "-proj-b").mkdir(parents=True)
    mrows = [
        {"type": "x", "timestamp": pts, "error": "overloaded_error retry"},
        {"type": "x", "timestamp": pts, "content": "top level content string"},
        {"type": "x", "timestamp": pts, "toolUseResult": {"stderr": "a nested tool result"}},
        {"type": "x", "timestamp": pts, "attachment": {"body": ["deep in a list"]}},
    ]
    with (mf / "-proj-b" / "s.jsonl").open("w") as f:
        for r in mrows:
            f.write(json.dumps(r) + "\n")
    globals()["PROJECTS"] = mf
    try:
        deep = probe_counts(date(2026, 7, 8), {
            "e": "overloaded_error", "c": "top level content",
            "t": "a nested tool result", "a": "deep in a list"})
    finally:
        globals()["PROJECTS"] = _real_projects_t1
        shutil.rmtree(mf.parent, ignore_errors=True)
    assert deep == {"e": 1, "c": 1, "t": 1, "a": 1}, deep
    print("probe counter OK")

    # --- probe recording + baseline validation (Task 2) ---
    # Each block captures and restores its OWN globals: a block that restores from a name
    # bound in an earlier block is order-dependent, and a selftest whose correctness depends
    # on statement order is one reordering away from passing vacuously.
    t2_reports, t2_projects = globals()["REPORTS"], globals()["PROJECTS"]
    prep_dir = Path(tempfile.mkdtemp())
    pdir_2 = Path(tempfile.mkdtemp()) / "p2"
    globals()["REPORTS"] = prep_dir
    globals()["PROJECTS"] = pdir_2
    (prep_dir / "2026-07-08.md").write_text(
        "# Session retro 2026-07-08\n\n## Recommendations\n\n"
        "**[rec: 2026-07-08#1] NEW - stop the worktree refusals**\n"
        "Dedup: no prior\n"
        "tier: hook\n"
        "Probe: isolated in the worktree\n\n"
        "**[rec: 2026-07-08#2] NEW - a probe that never fired**\n"
        "Dedup: no prior\n"
        "tier: script\n"
        "Probe: a signature absent from every transcript\n\n"
        "**[rec: 2026-07-08#3] NEW - a claude-md rec, no probe required**\n"
        "Dedup: no prior\n"
        "tier: hook\n\n"
        "**[rec: 2026-07-08#4] NEW - a probe that barely fired**\n"
        "Dedup: no prior\n"
        "tier: script\n"
        "Probe: a rare and thinly attested phrase\n\n"
        "**[rec: 2026-07-08#5] NEW - a probe too short to mean anything**\n"
        "Dedup: no prior\n"
        "tier: hook\n"
        "Probe: git\n\n"
        "**[rec: 2026-07-08#6] NEW - friction with no textual trace**\n"
        "Dedup: no prior\n"
        "tier: script\n"
        "Probe: none - the friction is a missing turn, which emits nothing\n\n"
        "## Next actions\n")
    (pdir_2 / "-proj-a").mkdir(parents=True)
    with (pdir_2 / "-proj-a" / "s1.jsonl").open("w") as f:
        # PROBE_MIN_BASELINE hits for #1, exactly one for #4 (thin), none for #2.
        for _i in range(PROBE_MIN_BASELINE):
            f.write(json.dumps({"type": "user", "timestamp": "2026-07-01T12:00:00.000Z",
                                "message": {"content": [{"type": "tool_result",
                                            "content": "this session is isolated in the worktree X"}]}}) + "\n")
        f.write(json.dumps({"type": "user", "timestamp": "2026-07-01T12:00:00.000Z",
                            "message": {"content": [{"type": "tool_result",
                                        "content": "a rare and thinly attested phrase"}]}}) + "\n")
    try:
        cmd_recs(date(2026, 7, 8))
        row = json.loads((prep_dir / "recs.jsonl").read_text().splitlines()[0])
    finally:
        globals()["REPORTS"], globals()["PROJECTS"] = t2_reports, t2_projects
        shutil.rmtree(prep_dir, ignore_errors=True)
        shutil.rmtree(pdir_2.parent, ignore_errors=True)
    by_id = {r["id"]: r for r in row["recs"]}
    # Fired enough times in its baseline window -> KEPT and scoreable.
    assert by_id["2026-07-08#1"]["probe"] == "isolated in the worktree", by_id
    assert by_id["2026-07-08#1"]["probe_baseline"] == PROBE_MIN_BASELINE, by_id
    assert by_id["2026-07-08#1"]["probe_drop"] == "", by_id
    # Never fired -> DROPPED. It cannot tell a fix from a typo.
    assert by_id["2026-07-08#2"]["probe"] == "", by_id
    assert by_id["2026-07-08#2"]["probe_baseline"] == 0, by_id
    assert by_id["2026-07-08#2"]["probe_drop"] == "no-baseline", by_id
    # No Probe: line at all -> absence recorded as null, never as 0.
    assert by_id["2026-07-08#3"]["probe"] == "", by_id
    assert by_id["2026-07-08#3"]["probe_baseline"] is None, by_id
    assert by_id["2026-07-08#3"]["probe_drop"] == "", by_id
    # Fired, but too thinly to score. KEPT with its count, flagged, never silently scored.
    assert by_id["2026-07-08#4"]["probe"] == "a rare and thinly attested phrase", by_id
    assert by_id["2026-07-08#4"]["probe_baseline"] == 1, by_id
    assert by_id["2026-07-08#4"]["probe_drop"] == "thin-baseline", by_id
    # Fails valid_probe outright -> never even counted, so baseline is 0 and the reason says
    # `invalid` rather than `no-baseline`: a rejected probe and an absent friction are
    # different failures and must not be reported as the same one.
    assert by_id["2026-07-08#5"]["probe"] == "", by_id
    assert by_id["2026-07-08#5"]["probe_drop"] == "invalid", by_id
    # `Probe: none - ...` is the contract's declared absence. It satisfies the pre-stamp gate
    # and must NOT be matched as the literal "none - ...": that would find nothing and be
    # filed as `no-baseline`, reporting a declared gap as a broken probe.
    assert by_id["2026-07-08#6"]["probe"] == "", by_id
    assert by_id["2026-07-08#6"]["probe_drop"] == "declared-none", by_id
    assert by_id["2026-07-08#6"]["probe_baseline"] is None, by_id
    print("probe recording OK")

    # --- pre-stamp probe gate (Task 2) ---
    ok = ("**[rec: 2026-07-08#1] a**\ntier: hook\nProbe: a real signature here\n"
          "**[rec: 2026-07-08#2] b**\ntier: script\nProbe: another real signature\n")
    assert report_probe_gaps(ok)[0] == [], report_probe_gaps(ok)[0]
    # The count-comparison bug this replaced: two probes on #1, none on #2. A whole-file
    # `probes >= tiers` passes this text; the per-rec check must not.
    masked = ("**[rec: 2026-07-08#1] a**\ntier: hook\nProbe: a real signature here\n"
              "Probe: a second signature on the same rec\n"
              "**[rec: 2026-07-08#2] b**\ntier: script\n")
    assert report_probe_gaps(masked)[0] == ["2026-07-08#2"], report_probe_gaps(masked)[0]
    # A non-mechanism rec needs no probe.
    prose = "**[rec: 2026-07-08#3] c**\ntier: claude-md\n"
    assert report_probe_gaps(prose)[0] == [], report_probe_gaps(prose)[0]
    # The LAST rec in the file is closed too - an off-by-one here silently exempts it.
    last = ("**[rec: 2026-07-08#1] a**\ntier: hook\nProbe: a real signature here\n"
            "**[rec: 2026-07-08#9] z**\ntier: hook\n")
    assert report_probe_gaps(last)[0] == ["2026-07-08#9"], report_probe_gaps(last)[0]
    # A block closes at the next SECTION too. A Probe: under `## Next actions` belongs to no
    # recommendation, and attaching it to the previous one is the masking bug again.
    spill = ("**[rec: 2026-07-08#7] a**\ntier: hook\n\n## Next actions\n"
             "Probe: a real signature here\n")
    assert report_probe_gaps(spill)[0] == ["2026-07-08#7"], report_probe_gaps(spill)[0]
    # Presence is not enough - the value must be usable at GATE time, since nothing after
    # the stamp can reject it.
    junk = "**[rec: 2026-07-08#8] a**\ntier: hook\nProbe: x\n"
    assert report_probe_gaps(junk)[0] == ["2026-07-08#8"], report_probe_gaps(junk)[0]
    # ...and the declared-absence form is accepted, with a reason.
    none_ok = "**[rec: 2026-07-08#9] a**\ntier: hook\nProbe: none - emits nothing\n"
    assert report_probe_gaps(none_ok)[0] == [], report_probe_gaps(none_ok)[0]
    none_bare = "**[rec: 2026-07-08#9] a**\ntier: hook\nProbe: none\n"
    assert report_probe_gaps(none_bare)[0] == ["2026-07-08#9"], report_probe_gaps(none_bare)[0]
    # Proportionality. A partial miss must NOT cost the whole report: the report is the
    # deliverable and the probe is a measurement of it. Only a wholesale ignore fails closed.
    import tempfile as _tf
    _pg = Path(_tf.mkdtemp())
    partial = _pg / "partial.md"
    partial.write_text("**[rec: 2026-07-08#1] a**\ntier: hook\nProbe: a real signature here\n"
                       "**[rec: 2026-07-08#2] b**\ntier: script\n")
    assert cmd_validate_report(str(partial)) == 0, "a partial miss must still stamp"
    total = _pg / "total.md"
    total.write_text("**[rec: 2026-07-08#1] a**\ntier: hook\n"
                     "**[rec: 2026-07-08#2] b**\ntier: script\n")
    assert cmd_validate_report(str(total)) == 1, "no rec complied -> fail closed"
    clean = _pg / "clean.md"
    clean.write_text("**[rec: 2026-07-08#1] a**\ntier: hook\nProbe: a real signature here\n")
    assert cmd_validate_report(str(clean)) == 0
    shutil.rmtree(_pg, ignore_errors=True)
    print("probe gate OK")

    # --- probe verdict lines (Task 3) ---
    t3_reports, t3_counts = globals()["REPORTS"], globals()["probe_counts"]

    def _pv_rec(rid, probe, baseline=40, drop=""):
        return {"id": rid, "repeat": False, "summary": "s", "dedup": "",
                "probe": probe, "probe_baseline": baseline, "probe_drop": drop}

    def _pv_setup(recs, metrics_rows, log=None):
        d = Path(tempfile.mkdtemp())
        globals()["REPORTS"] = d
        (d / "recs.jsonl").write_text(
            json.dumps({"report_date": "2026-07-08", "recs": recs}) + "\n")
        (d / "actions-log.md").write_text(log or "".join(
            "- [2026-07-09] taken rec:%s - did it\n" % r["id"] for r in recs))
        (d / "metrics.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in metrics_rows))
        return d

    def _pv_row(day, hours=10.0, failures=None):
        r = {"date": day, "coverage_hours": hours}
        if failures is not None:
            r["tool_failures"] = failures
        return r

    # tool_failures is deliberately NOT equal to coverage_hours, and NOT flat across the
    # split: with 10.0/10.0 on every day a control computed from the wrong field is
    # numerically identical to the right one, and the fixture cannot tell them apart.
    _before = ["2026-07-%02d" % n for n in range(2, 9)]     # 7 days
    _after = ["2026-07-%02d" % n for n in range(10, 17)]    # 7 days
    main_metrics = ([_pv_row(d, 10.0, 4.0) for d in _before]
                    + [_pv_row("2026-07-09", 10.0, 3.0)]     # the take date itself
                    + [_pv_row(d, 10.0, 2.0) for d in _after])
    calls = []

    def fake_counts(day, patterns):
        calls.append(day.isoformat())
        pre = day.isoformat() < "2026-07-09"
        return {rid: (2 if (pre and rid == "2026-07-08#1") else 0) for rid in patterns}

    pv_dir = _pv_setup([
        _pv_rec("2026-07-08#1", "isolated in the worktree"),
        # Stored baseline 40 from report time, but fires ZERO times in the window compared.
        _pv_rec("2026-07-08#2", "a rare and thinly attested phrase"),
        # Declared a probe and lost it: an unmeasured mechanism rec.
        _pv_rec("2026-07-08#3", "", baseline=0, drop="no-baseline"),
    ], main_metrics)
    globals()["probe_counts"] = fake_counts
    try:
        pl = probe_lines(date(2026, 7, 18))
        eff = effectiveness_lines(date(2026, 7, 18))
    finally:
        globals()["probe_counts"], globals()["REPORTS"] = t3_counts, t3_reports
        shutil.rmtree(pv_dir, ignore_errors=True)
    by_rec = {l.split()[1]: l for l in pl if l.startswith("PROBE rec:")}
    m = by_rec["rec:2026-07-08#1"]
    # 2 matches x 7 days / (10 coverage hours x 7 days) = 0.20 before; 0.00 after.
    assert "status:measured" in m and "reason:-" in m, m
    assert "before:0.20" in m and "after:0.00" in m, m
    assert "delta:-100.0%" in m, m
    # Counted in the window actually compared - NOT the stored baseline.
    assert "matches_before:14 matches_after:0" in m, m
    # Control from RAW tool_failures, coverage-weighted: 28/70 = 0.40 -> 14/70 = 0.20.
    # Computed from coverage_hours instead it would read +0.0%.
    assert "control:-50.0%" in m and "control_status:ok" in m, m
    # Only days WITH a metrics row are in the window; a day with no denominator is skipped,
    # never counted as a zero (which would read as an improvement).
    assert "n_before:7 n_after:7" in m, m
    # The take date belongs to NEITHER side. 2026-07-09 HAS a metrics row here, so this
    # assertion is live rather than incidentally satisfied by a gap in the fixture.
    assert "2026-07-09" not in calls, calls
    t = by_rec["rec:2026-07-08#2"]
    assert "status:unmeasurable" in t and "reason:thin-baseline" in t, t
    assert "matches_before:0" in t, t
    assert "rec:2026-07-08#3" not in by_rec, by_rec
    assert "PROBE-UNMEASURED n:1" in pl, pl
    # THE ROUND-1 BLOCKER, pinned: the digest that reaches reduce must carry these lines.
    # effectiveness_lines is the only one of the five subcommands on the reduce-input path
    # that this axis rides; a PROBE line that exists but never reaches it is a shipped no-op.
    assert any(l.startswith("PROBE rec:2026-07-08#1") for l in eff), eff[-4:]
    assert any(l.startswith("PROBE-UNMEASURED") for l in eff), eff[-4:]

    # (a) SHORT WINDOW. Baseline is healthy (3/day x 2 days = 6 >= PROBE_MIN_BASELINE) but
    #     only 2 covered days a side, so the window floor is the ONLY thing that can fire.
    def thick_counts(day, patterns):
        return {rid: (3 if day.isoformat() < "2026-07-09" else 0) for rid in patterns}

    pv2 = _pv_setup([_pv_rec("2026-07-08#1", "isolated in the worktree")],
                    [_pv_row(d, 10.0, 4.0) for d in
                     ("2026-07-07", "2026-07-08", "2026-07-10", "2026-07-11")])
    globals()["probe_counts"] = thick_counts
    try:
        pl2 = [l for l in probe_lines(date(2026, 7, 18)) if l.startswith("PROBE rec:")]
    finally:
        globals()["probe_counts"], globals()["REPORTS"] = t3_counts, t3_reports
        shutil.rmtree(pv2, ignore_errors=True)
    assert len(pl2) == 1, pl2
    assert "status:unmeasurable" in pl2[0] and "reason:short-window" in pl2[0], pl2
    assert "n_before:2 n_after:2" in pl2[0] and "matches_before:6" in pl2[0], pl2

    # (b) NO CONTROL. 7 covered days a side, healthy baseline - but only 2 days carry
    #     tool_failures, which is the real shape of this corpus before 2026-08-26.
    thin_ctrl = ([_pv_row(d, 10.0, 4.0 if d >= "2026-07-07" else None) for d in _before]
                 + [_pv_row(d, 10.0, 2.0 if d >= "2026-07-15" else None) for d in _after])
    pv3 = _pv_setup([_pv_rec("2026-07-08#1", "isolated in the worktree")], thin_ctrl)
    globals()["probe_counts"] = fake_counts
    try:
        pl3 = [l for l in probe_lines(date(2026, 7, 18)) if l.startswith("PROBE rec:")]
    finally:
        globals()["probe_counts"], globals()["REPORTS"] = t3_counts, t3_reports
        shutil.rmtree(pv3, ignore_errors=True)
    assert len(pl3) == 1, pl3
    assert "status:unmeasurable" in pl3[0] and "reason:no-control" in pl3[0], pl3
    assert "control_status:missing" in pl3[0], pl3

    # (c) An INVALID nonempty stored probe (a hand edit; _upsert_jsonl applies no schema).
    #     No PROBE line, AND still counted as a gap - never absent from both.
    pv4 = _pv_setup([_pv_rec("2026-07-08#1", "sh")], main_metrics)
    try:
        pl4 = probe_lines(date(2026, 7, 18))
    finally:
        globals()["REPORTS"] = t3_reports
        shutil.rmtree(pv4, ignore_errors=True)
    assert not [l for l in pl4 if l.startswith("PROBE rec:")], pl4
    assert "PROBE-UNMEASURED n:1" in pl4, pl4

    # (d) The after-window stops at the REPORT day. Here day=07-14, so 07-14..07-16 have
    #     metrics rows but are not yet observable; without the guard they would be counted
    #     as after-days, silently averaging in days the report cannot see.
    pv5 = _pv_setup([_pv_rec("2026-07-08#1", "isolated in the worktree")], main_metrics)
    globals()["probe_counts"] = fake_counts
    try:
        pl5 = [l for l in probe_lines(date(2026, 7, 14)) if l.startswith("PROBE rec:")]
    finally:
        globals()["probe_counts"], globals()["REPORTS"] = t3_counts, t3_reports
        shutil.rmtree(pv5, ignore_errors=True)
    assert len(pl5) == 1, pl5
    assert "n_before:7 n_after:4" in pl5[0], pl5   # 07-10..07-13 only
    print("probe verdict OK")

    print("selftest OK")


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    cmd = args[0]
    day = date.today() - timedelta(days=1)
    if "--date" in args:
        day = date.fromisoformat(args[args.index("--date") + 1])
    if cmd == "scan":
        cmd_scan(day)
    elif cmd == "extract":
        top = (int(args[args.index("--top") + 1]) if "--top" in args
               else MAX_ANALYSIS_SESSIONS)
        cmd_extract(day, analysis_cap(top))
    elif cmd == "metrics":
        cmd_metrics(day)
    elif cmd == "recs":
        cmd_recs(day)
    elif cmd == "validate-report":
        sys.exit(cmd_validate_report(args[args.index("--file") + 1]))
    elif cmd == "effectiveness":
        cmd_effectiveness(day)
    elif cmd == "probes":
        cmd_probes(day)
    elif cmd == "memory-health":
        cmd_memory_health()
    elif cmd == "prior-recs":
        cmd_prior_recs(day)
    elif cmd == "day-artifacts":
        cmd_day_artifacts(day)
    elif cmd == "ledger":
        cmd_ledger(day)
    elif cmd == "missing-dates":
        cmd_missing_dates()
    elif cmd == "trends":
        cmd_trends(int(args[args.index("--days") + 1]) if "--days" in args else 14)
    elif cmd == "selftest":
        selftest()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
