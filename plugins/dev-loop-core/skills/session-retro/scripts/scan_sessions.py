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
        "retries": 0, "first_ts": None, "last_ts": None, "events": 0,
    }
    prev_call = None
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
        if t == "assistant":
            s["assistant_turns"] += 1
            for b in blocks(d):
                if b.get("type") == "tool_use":
                    name = b.get("name", "?")
                    s["tools"][name] = s["tools"].get(name, 0) + 1
                    call = (name, json.dumps(b.get("input", {}), sort_keys=True))
                    if call == prev_call:
                        s["retries"] += 1
                    prev_call = call
        elif t == "user":
            has_tool_result = False
            for b in blocks(d):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    has_tool_result = True
                    txt = text_of(b)
                    if b.get("is_error"):
                        s["errors"] += 1
                        if len(s["error_samples"]) < 10:
                            s["error_samples"].append(txt.strip().split("\n")[0][:200])
                        if DENIAL_RE.search(txt):
                            s["denials"] += 1
                txt = text_of(b) if isinstance(b, dict) else str(b)
                if "[Request interrupted" in txt:
                    s["interrupts"] += 1
            if not has_tool_result:
                s["user_turns"] += 1
    return s


def merge_sub(parent, sub):
    for k in ("errors", "interrupts", "denials", "retries", "assistant_turns"):
        parent[k] += sub[k]
    for name, n in sub["tools"].items():
        parent["tools"][name] = parent["tools"].get(name, 0) + n
    parent["error_samples"] = (parent["error_samples"] + sub["error_samples"])[:10]


def friction(s):
    # A single-turn negative/sandbox test is not friction: no thrash, and the
    # error IS the asserted outcome. Suppress so probes don't outrank real friction.
    if s["retries"] == 0 and s["interrupts"] == 0 and s["errors"] > 0 \
       and s["error_samples"] \
       and all("operation not permitted" in e for e in s["error_samples"]):
        return 0.0
    # ponytail: naive weighted density; tune weights when reports misrank
    weighted = 3 * s["errors"] + 5 * s["interrupts"] + 2 * s["retries"] + s["denials"]
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
    print(f"{'score':>6} {'err':>4} {'int':>4} {'rty':>4} {'turns':>6} {'tools':>6}  session")
    for s in result["sessions"]:
        print(f"{s['friction_score']:>6} {s['errors']:>4} {s['interrupts']:>4} "
              f"{s['retries']:>4} {s['user_turns']+s['assistant_turns']:>6} "
              f"{sum(s['tools'].values()):>6}  {s['project']}/{s['session'][:8]}")
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
    picked = [s for s in result["sessions"] if s["friction_score"] > 0][:top]
    for s in picked:
        out = [f"# Extract {s['project']}/{s['session']} ({day})",
               f"friction={s['friction_score']} errors={s['errors']} "
               f"interrupts={s['interrupts']} retries={s['retries']} "
               f"subagents={s['subagents']}", ""]
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
    }
    path = REPORTS / "metrics.jsonl"
    rows = []
    if path.exists():
        for l in path.read_text().splitlines():
            try:
                r = json.loads(l)
            except json.JSONDecodeError:
                continue
            if r.get("date") != line["date"]:
                rows.append(r)
    rows.append(line)
    rows.sort(key=lambda r: r.get("date", ""))
    # pid-suffixed tmp: a manual backfill racing the scheduled run must not
    # interleave writes into one shared tmp file
    tmp = path.with_name(f"metrics.jsonl.tmp.{os.getpid()}")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    os.replace(tmp, path)  # atomic: manual/backfill invocations must not truncate
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
    # metrics upsert is idempotent per date (atomic replace-by-date)
    global REPORTS
    real_reports = REPORTS
    with tempfile.TemporaryDirectory() as td:
        REPORTS = Path(td)
        work = REPORTS / "work" / "2026-07-08"
        work.mkdir(parents=True)
        (work / "scan.json").write_text(json.dumps({"sessions": [
            {"tools": {"Bash": 2}, "errors": 1, "interrupts": 0, "retries": 1,
             "denials": 0, "friction_score": 12.5}]}))
        cmd_metrics(date(2026, 7, 8))
        cmd_metrics(date(2026, 7, 8))
        lines = (REPORTS / "metrics.jsonl").read_text().splitlines()
        assert len(lines) == 1, lines
        assert json.loads(lines[0])["top_friction"] == 12.5
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
