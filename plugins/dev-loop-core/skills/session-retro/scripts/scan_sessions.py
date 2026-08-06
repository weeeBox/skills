#!/usr/bin/env python3
"""Deterministic pre-scan for the session-retro skill.

Commands:
  scan --date YYYY-MM-DD      stats for all sessions with events on that day
                              (writes work/<date>/scan.json + table to stdout)
  extract --date D --top N    pruned evidence extracts for top-N friction sessions
                              (writes work/<date>/<session-id>.md + previous-report.md)
  missing-dates               dates since last complete report, up to yesterday
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
    r"\b(?:one |a )?correction (?:to|i owe|i must)\b|"
    # "my published number was wrong", "my ad-hoc-entry test was wrong" (<=4 tokens, so the
    # rejected unbounded-gap variant stays rejected and the negative assert still holds).
    r"\bmy [a-z0-9_./-]{1,30}(?: [a-z0-9_./-]{1,30}){0,3} (?:was|were|is|are) wrong\b|"
    r"\bthat(?:'s| is) (?:the [^.]{0,40} )?(?:mistake|error|one)?,? ?on me\b|"
    r"\bi (?:overstated|understated|conflated|misattributed|mislabel(?:l?ed)|"
    r"wrongly assumed|incorrectly assumed)\b|"
    r"\bthan i (?:assumed|thought)\b|"
    r"\b(?:is|was|are|were) confounded\b|"
    r"\b(?:names?|lists?|cites?) (?:it|them|that) wrongly\b|"
    r"\b(?:is|was) now stale\b")
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
GATE_RE = re.compile(
    r"(?i)"
    r"adversarial-review|codex-companion|agy-companion|"                    # reviewer runtimes
    r"codex\s+exec|/codex:|codex:(?:adversarial|rescue|task)|codex-rescue|" # codex CLI/skill/subagent
    r"/agy:|agy:rescue|agy-rescue|\bagy\s|"                                 # agy CLI/skill/subagent
    r"lander\.sh|review-gate|gate-loop|"                                    # gate/land scripts
    r'"skill":\s*"[^"]*(?:review-gate|gate-loop|:land|:ship)')              # gate skills via Skill


def is_gate_call(name, input_json):
    """True iff a tool call is a codex/agy gate dispatch/poll: a job-dispatcher tool (GATE_TOOLS)
    whose input carries a gate signature. The name gate stops gate-MENTIONING reads/questions from
    counting as gate time.
    ponytail: a Bash that merely greps a codex/gate file still counts (gate-adjacent tooling) -
    acceptable; the big inflation was AskUserQuestion/Read, now excluded by the name gate."""
    return name in GATE_TOOLS and bool(GATE_RE.search(input_json))


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
    }
    prev_call = None
    prev_ts = None          # datetime of the previous in-day event (transcript order)
    prev_label = None       # what the previous event was, for gap attribution
    pending = {}            # tool_use id -> (name, ts) awaiting its tool_result
    all_gaps = []           # (secs, at_hms, after_label) - top-5 kept at the end
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
        # inter-event gap in TRANSCRIPT order (not sorted), attributed to what the
        # previous event was. NEUTRAL wall-clock: may be human think-time, model
        # latency, async wait, or tool latency - not waste on its own.
        if prev_ts is not None:
            all_gaps.append(((ts - prev_ts).total_seconds(),
                             prev_ts.strftime("%H:%M:%S"), prev_label))
        t = d.get("type")
        label = t
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
            if RETRACTION_RE.search(atext):
                s["self_retractions"] += 1
            for b in blocks(d):
                if b.get("type") == "tool_use":
                    name = b.get("name", "?")
                    s["tools"][name] = s["tools"].get(name, 0) + 1
                    label = "tool_use:" + name
                    bid = b.get("id")
                    if bid:
                        pending[bid] = (name, ts)  # local to this file: never crosses scans
                    call = (name, json.dumps(b.get("input", {}), sort_keys=True))
                    if call == prev_call:
                        s["retries"] += 1
                    prev_call = call
                    if is_gate_call(name, call[1]):
                        s["gate_calls"] += 1
                        label = "gate:" + name   # so the FOLLOWING gap is attributed to the gate
        elif t == "user":
            has_tool_result = False
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
        prev_ts, prev_label = ts, label
    flush_run()
    s["gaps"] = [{"secs": round(g, 1), "at": at, "after": lab}
                 for g, at, lab in sorted(all_gaps, reverse=True)[:5]]
    # wall-clock spent waiting on a codex/agy gate = the gaps that FOLLOW a gate call. Reuses the
    # gap machinery (a gate that blocks 25 min shows as a big gap labeled "gate:*") instead of a
    # stateful dispatch->verdict correlator.
    gate_gaps = [g for g, _at, lab in all_gaps if lab and lab.startswith("gate:")]
    s["gate_wait_secs"] = round(sum(gate_gaps), 1)
    s["max_gate_wait_secs"] = round(max(gate_gaps), 1) if gate_gaps else 0.0
    s["repeated_error_runs"] = runs
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
            if now.timestamp() - st.st_mtime < ACTIVE_GRACE_SECS:
                active.append(f"{proj.name}/{f.stem}")
            s = scan_file(f, start, end)
            if s["events"] == 0:
                continue
            subdir = f.parent / f.stem / "subagents"
            subs = list(subdir.glob("*.jsonl")) if subdir.is_dir() else []
            for sub in subs:
                merge_sub(s, scan_file(sub, start, end))
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
                     subagents=len(subs), friction_score=friction(s))
            sessions.append(s)
    sessions.sort(key=lambda x: x["friction_score"], reverse=True)
    return {"date": day.isoformat(), "generated": now.isoformat(),
            "sessions": sessions, "still_active": active}


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
        print(f"still-active files scanned: {len(result['still_active'])}")
    print(f"scan.json: {workdir / 'scan.json'}")
    return result


def prune_event(d, start, end, out):
    ts = parse_ts(d.get("timestamp", ""))
    if ts is None or not (start <= ts < end):
        return
    t = d.get("type")
    stamp = ts.strftime("%H:%M:%S")
    if t == "user":
        for b in blocks(d):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                txt = text_of(b).strip()
                cap = 500 if b.get("is_error") else 300
                tag = "TOOL_ERROR" if b.get("is_error") else "tool_result"
                out.append(f"[{stamp}] {tag}: {txt[:cap]}")
            else:
                txt = (text_of(b) if isinstance(b, dict) else str(b)).strip()
                if txt:
                    out.append(f"[{stamp}] USER: {txt[:1500]}")
    elif t == "assistant":
        for b in blocks(d):
            if b.get("type") == "tool_use":
                args = json.dumps(b.get("input", {}))[:300]
                out.append(f"[{stamp}] tool_use {b.get('name', '?')}: {args}")
            elif b.get("type") == "text" and b.get("text", "").strip():
                out.append(f"[{stamp}] ASSISTANT: {b['text'].strip()[:800]}")


def sub_errors(s, start, end, out, size):
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
                    line = (f"[{sub.stem} {ts.strftime('%H:%M:%S')}] "
                            f"TOOL_ERROR: {text_of(b).strip()[:300]}")
                    out.append(line)
                    size += len(line) + 1
    return size


def cmd_extract(day, top):
    import hashlib
    workdir = REPORTS / "work" / day.isoformat()
    scan_path = workdir / "scan.json"
    result = json.loads(scan_path.read_text()) if scan_path.exists() else cmd_scan(day)
    start, end = day_bounds(day)
    cap = min(top, 8)
    sessions = result["sessions"]
    fric = [s for s in sessions if s["friction_score"] > 0]
    slow = [s for s in sorted(sessions, key=lambda x: x.get("duration_secs", 0), reverse=True)
            if s.get("duration_secs", 0) > 0]
    picked, seen, slow_added = [], set(), []

    def _add(s):
        k = (s["project"], s["session"])
        if k in seen:
            return False
        seen.add(k)
        picked.append(s)
        return True

    # Reserve up to 2 of the `cap` slots for the slowest sessions friction would NOT
    # surface (friction==0 but long wall-clock) - so "slow but quiet" sessions stop
    # being invisible. The rest go to friction; leftover slots backfill with slow.
    # Note: when friction sessions exceed cap, this drops the 2 weakest to make room.
    for s in [x for x in slow if x["friction_score"] == 0][:2]:
        if len(picked) < cap and _add(s):
            slow_added.append(s)
    for s in fric:
        if len(picked) >= cap:
            break
        _add(s)
    for s in slow:
        if len(picked) >= cap:
            break
        _add(s)
    picked.sort(key=lambda x: (x["friction_score"], x.get("duration_secs", 0)), reverse=True)
    if slow_added:
        print("slow-reserved extracts (friction=0, long wall-clock): "
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
            "repeated_error_runs=" + (", ".join(f"{r['count']}x {r['snippet'][:60]!r}"
                                                for r in rer) or "none"),
            f"gate_calls={s.get('gate_calls', 0)} gate_wait_secs={s.get('gate_wait_secs', 0)} "
            f"max_gate_wait_secs={s.get('max_gate_wait_secs', 0)}  (codex/agy review-gate "
            "latency: many calls + high wait = slow/repeated gate rounds - a NAMEABLE cost)",
            "",
        ]
        size = sum(len(x) + 1 for x in out)
        # sessions with subagents keep 15KB of the cap reserved for their error
        # evidence (their stats are folded into the score, so the report needs it)
        main_cap = MAX_EXTRACT_BYTES - (15_000 if s["subagents"] else 0)
        for d in iter_lines(Path(s["path"])):
            n0 = len(out)
            prune_event(d, start, end, out)
            size += sum(len(x) + 1 for x in out[n0:])
            if size > main_cap:
                out.append("[main trajectory truncated at cap]")
                break
        if s["subagents"]:
            size = sub_errors(s, start, end, out, size)
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
    }
    _upsert_jsonl(REPORTS / "metrics.jsonl", line, key="date")
    print(f"metrics upserted for {line['date']}")


# full-schema match (anchored both ends): summary AND (reason) are mandatory -
# a prefix-only match would let schema-violating tails through as "valid"
LEDGER_RE = re.compile(
    r"^- \[(\d{4}-\d{2}-\d{2})\] (?:taken|rejected|deferred) "
    r"rec:(\d{4}-\d{2}-\d{2})#\d+ - .+ \(.+\)$")


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
    printable ASCII, drop angle brackets, cap length."""
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[^\x20-\x7e]", "", s)
    s = s.replace("<", "").replace(">", "")
    return s[:120]


def cmd_recs(day):
    """Upsert the rec-ids in <day>'s stamped report into recs.jsonl - the cross-day recurrence
    signal (a rec-id on multiple report dates = a pattern that recurred). Deterministic: only
    well-formed `[rec: <origin-date>#<n>]` tags whose origin is a real calendar date <= the
    report date; summaries charset-neutralized. Feeds only future retro bookkeeping."""
    report = REPORTS / f"{day.isoformat()}.md"
    if not report.exists():
        return
    recs = {}
    for line in report.read_text(encoding="utf-8", errors="replace").splitlines():
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
    assert not is_gate_call("Bash", '{"command": "git status --short"}')
    # the tightening: a QUESTION / READ that only MENTIONS a gate must NOT count as gate time
    assert not is_gate_call("AskUserQuestion",
                            '{"questions": [{"question": "override the codex review-gate no-ship?"}]}')
    assert not is_gate_call("Read", '{"file_path": "codex-adversarial-review-notes.md"}')
    assert not is_gate_call("Edit", '{"file_path": "ship_it.py"}')   # bare 'ship' in a name != gate
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
        # ledger filter: valid passes; future rec id, time-travel, malformed dropped
        (REPORTS / "actions-log.md").write_text("\n".join([
            "- [2026-07-09] taken rec:2026-07-08#1 - valid (ok)",
            "- [2026-07-09] rejected rec:2026-07-09#1 - future rec id (pre-seeded)",
            "- [2026-07-07] taken rec:2026-07-08#2 - acted before report (early)",
            "- [2026-07-09] rejected rec:2026-07-08#3 - schema-violating tail no reason",
            "ignore me: not a ledger line rec:2026-07-01#1",
        ]))
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_ledger(date(2026, 7, 9))
        kept = buf.getvalue().strip().splitlines()
        assert kept == ["- [2026-07-09] taken rec:2026-07-08#1 - valid (ok)"], kept
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
    REPORTS = real_reports
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
    elif cmd == "selftest":
        selftest()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
