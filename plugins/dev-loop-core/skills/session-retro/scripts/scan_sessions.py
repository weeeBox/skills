#!/usr/bin/env python3
"""Deterministic pre-scan for the session-retro skill.

Commands:
  scan --date YYYY-MM-DD      stats for all sessions with events on that day
                              (writes work/<date>/scan.json + table to stdout)
  extract --date D --top N    pruned evidence extracts for top-N friction sessions
                              (writes work/<date>/<session-id>.md + previous-report.md)
  missing-dates               dates since last complete report, up to yesterday
  trends [--days N]           rate-normalized quality trend over the last N days of
                              metrics.jsonl (default 14) + an effectiveness-digest snapshot
  selftest                    run built-in assertions on a synthetic transcript

Stdlib only. Transcript content is untrusted data; this script only counts and
truncates it, never executes it.
"""
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
MAX_EXTRACT_BYTES = 100_000
# Longest a single model generation is allowed to be before the gap is called `idle` instead.
# A user->assistant gap of 30s is generation latency; the same gap at 10h47m is the machine
# sitting on a pending turn (2026-08-16 session 78476725, which alone was 98.4% of that day's
# reported work_secs).
MAX_GENERATION_SECS = float(os.environ.get("RETRO_MAX_GENERATION_SECS", 1800))
TRUNC_SLOT = "\x00truncation-header-slot"  # placeholder, filled once the body is built
DENIAL_RE = re.compile(r"doesn't want to proceed|denied by|permission denied", re.I)
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
        "user_turns": 0, "assistant_turns": 0, "tools": {}, "errors": 0,
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
                        s["errors"] += 1
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
    for k in ("errors", "interrupts", "denials", "retries", "assistant_turns",
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
    weighted = (3 * s["errors"] + 5 * s["interrupts"] + 2 * s["retries"] + s["denials"]
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


def cmd_scan(day):
    result = scan_day(day)
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
                out.append("[extract truncated at 100KB cap]")
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
    sessions = [s for s in sessions if not is_retro_stub(s)]
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
    cap = min(top, 8)
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
    # stage previous complete report for the repeat-findings comparison
    prev = [p for p in sorted(REPORTS.glob("????-??-??.md"))
            if p.stem < day.isoformat() and COMPLETE_MARKER in p.read_text()]
    if prev:
        (workdir / "previous-report.md").write_text(prev[-1].read_text())
    print(f"extracted {len(picked)} sessions to {workdir}")


def cmd_metrics(day):
    """Upsert one per-day summary line into metrics.jsonl (atomic replace-by-date)."""
    scan = json.loads((REPORTS / "work" / day.isoformat() / "scan.json").read_text())
    ss = scan["sessions"]
    line = {
        "date": day.isoformat(),
        "sessions": len(ss),
        "tool_calls": sum(sum(s["tools"].values()) for s in ss),
        "errors": sum(s["errors"] for s in ss),
        "interrupts": sum(s["interrupts"] for s in ss),
        "retries": sum(s["retries"] for s in ss),
        "denials": sum(s["denials"] for s in ss),
        "top_friction": max((s["friction_score"] for s in ss), default=0),
        # .get defaults: a scan.json written before this field existed must not KeyError
        "tokens": sum(s.get("total_tokens", 0) for s in ss),
        "cache_write_tokens": sum(s.get("cache_write_tokens", 0) for s in ss),
        "max_duration_secs": max((s.get("duration_secs", 0) for s in ss), default=0),
        "gate_calls": sum(s.get("gate_calls", 0) for s in ss),
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
    _upsert_jsonl(REPORTS / "metrics.jsonl", line, key="date")
    print(f"metrics upserted for {line['date']} (coverage_hours={line['coverage_hours']})")


# Prefix-anchored: date/verb/rec-id/dash and a non-empty summary are mandatory. The reason
# clause is NOT required to end in "(...)" - real actions-log.md entries settled on
# multi-sentence narrative endings (no trailing parens) starting ~2026-08-12, and the old
# `\(.+\)$` end-anchor silently dropped 72 of 103 real lines as "malformed" - including the
# line marking rec:2026-08-14#7 itself as taken, which is why that rec stayed CHRONIC even
# after landing (session-retro dimension-2 audit, 2026-08-20). A genuinely empty summary
# (nothing after "- ") still fails to match.
LEDGER_RE = re.compile(
    r"^- \[(\d{4}-\d{2}-\d{2})\] (?:taken|rejected|deferred) "
    r"rec:(\d{4}-\d{2}-\d{2})#\d+ - \S.*$")


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
        line_date, rec_date = m.group(1), m.group(2)
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
                                 "summary": _sanitize_summary(after)}
    _upsert_jsonl(REPORTS / "recs.jsonl",
                  {"report_date": day.isoformat(), "recs": list(recs.values())},
                  key="report_date")
    print(f"recs upserted for {day.isoformat()}: {len(recs)} rec-ids")


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


def _taken_recs():
    """rec-id -> earliest date it was marked taken, from the schema-validated actions-log.
    Non-calendar dates (regex-shaped but invalid) are skipped so cmd_effectiveness's
    date.fromisoformat can never crash on model-written garbage."""
    path = REPORTS / "actions-log.md"
    taken = {}
    if not path.exists():
        return taken
    for line in path.read_text().splitlines():
        m = LEDGER_RE.match(line)
        if not m or "] taken rec:" not in line:
            continue
        line_date, rec_date = m.group(1), m.group(2)
        try:
            date.fromisoformat(line_date); date.fromisoformat(rec_date)
        except ValueError:
            continue  # non-calendar date in the (model-written) actions-log -> skip, never crash
        if line_date < rec_date:
            continue  # acted before the report existed (see cmd_ledger)
        rid = re.search(r"rec:(\d{4}-\d{2}-\d{2}#\d+)", line).group(1)  # LEDGER_RE already matched
        if rid not in taken or line_date < taken[rid]:
            taken[rid] = line_date
    return taken


def cmd_effectiveness(day):
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

    def label(rid):
        return f' "{summ[rid]}"' if rid in summ else ""

    for rid, tdate in sorted(_taken_recs().items()):
        after = [dd for dd in seen.get(rid, []) if dd > tdate]
        if after:
            status, last = "recurred-after-fix", after[-1]
        else:
            days_since = (day - date.fromisoformat(tdate)).days
            status = "too-soon" if days_since < TOO_SOON_DAYS else "holding"
            last = seen.get(rid, ["none"])[-1] if seen.get(rid) else "none"
        print(f"EFFECTIVENESS rec:{rid} taken:{tdate} status:{status} "
              f"last_seen:{last} seen_count:{len(seen.get(rid, []))}{label(rid)}")
    cutoff = (day - timedelta(days=CHRONIC_WINDOW)).isoformat()
    for rid in sorted(seen):
        in_window = [dd for dd in seen[rid] if dd >= cutoff]
        if len(in_window) >= CHRONIC_MIN_DAYS:
            print(f"CHRONIC rec:{rid} seen_count:{len(in_window)} "
                  f"dates:{in_window[0]}..{in_window[-1]}{label(rid)}")


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

    `errors_per_hour` / `tool_calls_per_hour` are the primary axes (already rate-
    normalized, so a busy day and a quiet day are comparable - a raw count conflates
    volume with quality). A day with < MIN_TREND_COVERAGE_HOURS of coverage (no sessions,
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
    print(f"{'date':<12} {'sess':>4} {'cov_h':>6} {'tools/hr':>9} {'err/hr':>7} "
          f"{'friction':>9} {'gates':>6} {'max_gate_wait':>13} {'human_wait%':>12}")
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
              f"{cell(r.get('tool_calls_per_hour'), 9)} {cell(r.get('errors_per_hour'), 7)} "
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
        for key, label_ in (("errors_per_hour", "errors/hr (primary)"),
                            ("tool_calls_per_hour", "tool_calls/hr"),
                            ("top_friction", "friction_score (noisier axis)")):
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
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_effectiveness(latest)
        eff_lines = buf.getvalue().splitlines()
        holding = sum(1 for l in eff_lines if "status:holding" in l)
        recurred = sum(1 for l in eff_lines if "status:recurred-after-fix" in l)
        too_soon = sum(1 for l in eff_lines if "status:too-soon" in l)
        chronic = sum(1 for l in eff_lines if l.startswith("CHRONIC"))
        print(f"effectiveness as of {latest.isoformat()}: holding={holding} "
              f"recurred-after-fix={recurred} too-soon={too_soon} CHRONIC={chronic}")
        if chronic:
            print("  a CHRONIC rec recurring across weeks despite landing is the strongest "
                  "single signal something isn't actually improving - see that day's report's "
                  "Fix effectiveness section for which one(s)")


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
             "cache_write_tokens": 50, "duration_secs": 40.0}]}))
        cmd_metrics(date(2026, 7, 8))
        cmd_metrics(date(2026, 7, 8))
        lines = (REPORTS / "metrics.jsonl").read_text().splitlines()
        assert len(lines) == 1, lines
        m = json.loads(lines[0])
        assert m["top_friction"] == 12.5
        assert (m["tokens"], m["cache_write_tokens"], m["max_duration_secs"]) == (300, 50, 40.0), m
        # a pre-migration scan.json (no token fields) must not KeyError -> .get defaults
        (work / "scan.json").write_text(json.dumps({"sessions": [
            {"tools": {}, "errors": 0, "interrupts": 0, "retries": 0,
             "denials": 0, "friction_score": 0}]}))
        cmd_metrics(date(2026, 7, 8))
        m2 = json.loads((REPORTS / "metrics.jsonl").read_text().splitlines()[0])
        assert m2["tokens"] == 0 and m2["max_duration_secs"] == 0, m2
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
            "Evidence: ...\n" + COMPLETE_MARKER + "\n")
        cmd_recs(date(2026, 7, 11))
        rrow2_line = next(l for l in (REPORTS / "recs.jsonl").read_text().splitlines()
                          if json.loads(l)["report_date"] == "2026-07-11")
        rid2 = {r["id"]: r for r in json.loads(rrow2_line)["recs"]}["2026-07-11#1"]
        assert rid2["summary"].startswith("[claude-md]"), rid2
        assert "below for the sharpening" not in rid2["summary"], rid2
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
        for iso, epr, tph, fric in [
            ("2026-07-20", 10.0, 100.0, 30.0), ("2026-07-21", 12.0, 110.0, 32.0),
            ("2026-07-22", 8.0, 90.0, 28.0), ("2026-07-23", 10.0, 100.0, 30.0),
            ("2026-07-24", 2.0, 50.0, 10.0), ("2026-07-25", 3.0, 55.0, 12.0),
            ("2026-07-26", 1.0, 45.0, 8.0), ("2026-07-27", 2.0, 50.0, 10.0),
        ]:
            _upsert_jsonl(REPORTS / "metrics.jsonl", {
                "date": iso, "sessions": 5, "coverage_hours": 5.0,
                "errors_per_hour": epr, "tool_calls_per_hour": tph, "top_friction": fric,
                "gate_calls": 1, "max_gate_wait_secs": 1.0, "work_secs": 10.0,
                "human_wait_secs": 10.0, "blocked_secs": 0.0, "model_latency_secs": 0.0,
                "idle_secs": 0.0,
            }, key="date")
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
        assert "trend errors/hr (primary): first-half avg 10.0 -> second-half avg 2.0 (down)" in out_t, out_t
        assert "trend tool_calls/hr: first-half avg 100.0 -> second-half avg 50.0 (down)" in out_t, out_t
        assert ("trend friction_score (noisier axis): first-half avg 30.0 -> "
                "second-half avg 10.0 (down)") in out_t, out_t
        row_2026_07_19 = next(l for l in out_t.splitlines() if l.startswith("2026-07-19"))
        assert "low-coverage" in row_2026_07_19, row_2026_07_19
        assert "effectiveness as of 2026-07-27: holding=1" in out_t, out_t
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
    finally:
        PROJECTS = real_projects
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
        top = int(args[args.index("--top") + 1]) if "--top" in args else 8
        cmd_extract(day, min(top, 8))
    elif cmd == "metrics":
        cmd_metrics(day)
    elif cmd == "recs":
        cmd_recs(day)
    elif cmd == "effectiveness":
        cmd_effectiveness(day)
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
