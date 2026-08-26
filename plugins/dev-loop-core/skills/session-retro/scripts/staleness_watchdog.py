#!/usr/bin/env python3
"""Same-day staleness watchdog for session-retro.

The daily report only surfaces the FOLLOWING morning, so a session that dispatches an
async job (a backgrounded Bash, or an Agent) whose completion notification never
arrives just sits there - defef5a9-3b39-4cb4-ba79-bda6611372cc (2026-08-19/20) lost
19,478s (65% of an 8.5h unattended overnight run) to exactly this, undetected until a
human happened to check in. This script is a cheap, frequent (~15-30 min via launchd
StartInterval, see scripts/watchdog.sh) sweep that catches that live, same-day,
instead of waiting for tomorrow's report.

Commands:
  scan          stat-filter recently-active session transcripts for staleness, tail-parse
                only the stale candidates, print one line per NEWLY-stuck session (dedup'd
                against work/watchdog-state.json so a still-stuck session doesn't re-notify
                every run), update the state file.
  selftest      run built-in assertions on synthetic transcripts

Stdlib only, reuses scan_sessions.py's transcript parsing (iter_lines/blocks/text_of/
BG_NOTIFY_RE) rather than re-implementing it. Transcript content is untrusted data; this
script only reads and classifies it, never executes anything found inside it.

ponytail: `scan()` calls iter_lines() (a full read_text) per STALE candidate rather than
a byte-offset tail-seek. The mtime stat-filter already narrows candidates to sessions with
no write in STALE_SECS+, which in practice is a handful of files at any moment - a full
parse of a few MB there is negligible next to running this every 15-30 min. Revisit with a
real tail-seek only if `scan` is ever observed to run long enough to matter.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan_sessions as ss  # reuse iter_lines/blocks/text_of/BG_NOTIFY_RE - one parser, not two

STATE_PATH = ss.REPORTS / "work" / "watchdog-state.json"
# A session with no write in this long might just be stuck - big enough margin above the
# longest observed legitimate single async wait (~30 min, a whole-branch review agent
# dispatch in defef5a9 itself), well below the 19,478s/5.4h this watchdog exists to catch
# hours earlier instead of the next morning.
STALE_SECS = int(os.environ.get("RETRO_WATCHDOG_STALE_SECS", 45 * 60))
# Don't bother tail-parsing a session nobody's touched in ages - not "still stuck", just old.
RECENT_WINDOW_SECS = int(os.environ.get("RETRO_WATCHDOG_RECENT_SECS", 18 * 3600))


def candidates(now=None):
    """(path, age_secs) for every transcript written to between STALE_SECS and
    RECENT_WINDOW_SECS ago - cheap: stat only, no file content read."""
    now = now if now is not None else time.time()
    out = []
    for p in ss.PROJECTS.glob("*/*.jsonl"):
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if STALE_SECS <= age <= RECENT_WINDOW_SECS:
            out.append((p, age))
    return sorted(out, key=lambda x: -x[1])


# A dispatch into an EXTERNAL queue (codex/agy `--background`) returns a job id immediately, so the
# Bash call itself completes and the harness never owes a notification - the run_in_background test
# below cannot see it. Session ce8656e6 (2026-08-26) lost 731 of 795 minutes to exactly this: four
# land gates queued this way, every gate done in under 3 min, and is_owed_turn() classified all three
# stalls as "ordinary end-of-turn, nothing left in flight" (replayed against the real transcript).
# The companion is usually invoked through a shell var (`node "$CODEX" task --background`), so
# matching only the literal filename misses most real dispatches - and most real read-backs.
_CODEX = r"(?:codex-companion\.mjs|\$\{?CODEX\}?|\bagy\b)"
EXTERNAL_DISPATCH_RE = re.compile(_CODEX + r"[^\n]*--background")
# ...and it is resolved the moment the session goes and LOOKS: a status/result call, or a read of the
# job record or the verdict it lands in. Any of those means the wait is being managed, not forgotten.
EXTERNAL_READ_RE = re.compile(
    _CODEX + r"[^\n]*\b(?:status|result|cancel)\b"
    r"|/jobs/[\w.-]+\.json"
    r"|land-verdicts"
    r"|watch-bg-job\.sh"
)


def _command_text(block):
    inp = block.get("input") or {}
    return inp.get("command") or "" if isinstance(inp, dict) else ""


def last_meaningful_event(path):
    """Scan the WHOLE file once: the last user/assistant row, every background-job
    dispatch (an Agent tool_use, or a Bash tool_use with run_in_background=True) and every
    id that later got a <task-notification> completion anywhere in the file - the dispatch
    and its notification can be far apart, so this can't be tail-windowed."""
    dispatched_bg = set()
    notified = set()
    external_pending = 0
    last = None
    for d in ss.iter_lines(path):
        t = d.get("type")
        if t == "assistant":
            for b in ss.blocks(d):
                if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                    continue
                name = b.get("name", "?")
                inp = b.get("input") or {}
                is_bg = name == "Agent" or (name == "Bash" and inp.get("run_in_background") is True)
                if is_bg and b.get("id"):
                    dispatched_bg.add(b["id"])
                if name == "Bash":
                    cmd = _command_text(b)
                    if EXTERNAL_READ_RE.search(cmd):
                        external_pending = 0  # the session went and looked - nothing forgotten
                    elif EXTERNAL_DISPATCH_RE.search(cmd):
                        external_pending += 1
        elif t == "user":
            raw = (d.get("message") or {}).get("content")
            raw_text = raw if isinstance(raw, str) else " ".join(
                ss.text_of(b) for b in ss.blocks(d) if isinstance(b, dict))
            for m in ss.BG_NOTIFY_RE.finditer(raw_text):
                notified.add(m.group(1))
        if t in ("user", "assistant"):
            last = d
    return last, dispatched_bg, notified, external_pending


def is_owed_turn(path):
    """True iff the session's LAST row leaves it owing a turn that only an autonomous
    continuation - not a human - can supply. Two shapes, both seen in practice:
      (a) last row is an assistant turn that dispatched a background job whose id never
          got a completion notification anywhere in the file (defef5a9's actual bug).
      (b) last row is a completion notification itself, with no assistant turn after it
      (c) an external async job (codex/agy `--background`) was queued and never read back - the
          harness owes no notification for these, so shape (a) is blind to them

    ponytail: from a static transcript file alone, a genuinely-wedged LIVE session and an
    old session the user simply stopped returning to (closed the terminal, started a new
    conversation elsewhere) look IDENTICAL - both end on an unresolved dispatch forever.
    Verified live on this machine (2026-08-20): 2 of 3 first-run flags were sessions last
    written to the evening before, already reported on in that day's retro - false
    positives by this definition, not live wedges. No externally-visible signal (this
    script has no hook into "is a `claude` process still attached to this file") tells
    them apart. Accepted: dedup in scan() means each such session notifies ONCE ever (its
    mtime never changes again), not repeatedly - a one-time false alarm is a cheap price
    for catching a real multi-hour wedge same-day instead of the next morning. Upgrade
    path if the false-positive rate is ever a real problem: correlate against live
    `claude`/`codex-companion` process state instead of file content alone.
          (the job finished but nothing ever picked it up).
    A plain trailing human message, or a dispatch that already got its notification and
    an assistant reply, is ordinary idle time - NOT flagged; that's most of a quiet day."""
    last, dispatched_bg, notified, external_pending = last_meaningful_event(path)
    if last is None:
        return False, "no user/assistant events"
    if last.get("type") == "assistant":
        unresolved = dispatched_bg - notified
        if unresolved:
            return True, f"{len(unresolved)} background dispatch(es) with no completion notification"
        if external_pending:
            return True, (f"{external_pending} external async dispatch(es) (codex/agy --background) "
                          "queued and never read back")
        return False, "ordinary end-of-turn, nothing left in flight"
    if last.get("type") == "user":
        raw = (last.get("message") or {}).get("content")
        raw_text = raw if isinstance(raw, str) else ""
        if ss.BG_NOTIFY_RE.search(raw_text):
            return True, "a completion notification arrived with no assistant follow-up"
        return False, "a plain user/tool-result turn"
    return False, "unrecognized last event type"


def _load_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(f"{STATE_PATH.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(state, indent=0, sort_keys=True))
    os.replace(tmp, STATE_PATH)


def scan():
    """Print one line per newly-stuck session (not already notified for this exact
    last-write instant) and persist the dedup state. A session still stuck on a later run
    with no new writes (same mtime) is silently skipped - one notification per stuck
    instance, not one per poll."""
    now = time.time()
    state = _load_state()
    new_state = {}
    flagged = []
    for path, age in candidates(now):
        key = str(path)
        mtime = path.stat().st_mtime
        owed, reason = is_owed_turn(path)
        if not owed:
            continue
        new_state[key] = mtime  # keep only currently-candidate, currently-owed entries
        if state.get(key) == mtime:
            continue  # already notified for this exact stuck instance
        flagged.append(f"{path.parent.name}/{path.stem} stale {int(age/60)}m: {reason}")
    _save_state(new_state)
    for line in flagged:
        print(line)
    return flagged


def selftest():
    import tempfile

    def ev(t, sec, **kw):
        return dict(type=t, timestamp=f"2026-07-08T12:{sec // 60:02d}:{sec % 60:02d}.000Z", **kw)

    def write(lines):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for l in lines:
            f.write(json.dumps(l) + "\n")
        f.close()
        return Path(f.name)

    # (a) an unresolved background dispatch -> owed
    p1 = write([
        ev("user", 0, message={"content": [{"type": "text", "text": "go"}]}),
        ev("assistant", 1, message={"content": [
            {"type": "tool_use", "name": "Agent", "id": "a1",
             "input": {"prompt": "--background\n\ninvestigate"}}]}),
    ])
    owed, why = is_owed_turn(p1)
    assert owed and "1 background dispatch" in why, (owed, why)
    p1.unlink()

    # (b) same dispatch, but its notification DID arrive and got a reply -> not owed
    notif = ("<task-notification>\n<task-id>j1</task-id>\n"
              "<tool-use-id>a1</tool-use-id>\n<status>completed</status>\n</task-notification>")
    p2 = write([
        ev("user", 0, message={"content": [{"type": "text", "text": "go"}]}),
        ev("assistant", 1, message={"content": [
            {"type": "tool_use", "name": "Agent", "id": "a1",
             "input": {"prompt": "--background\n\ninvestigate"}}]}),
        ev("user", 100, message={"content": notif}),
        ev("assistant", 101, message={"content": [{"type": "text", "text": "done"}]}),
    ])
    owed2, why2 = is_owed_turn(p2)
    assert not owed2, (owed2, why2)
    p2.unlink()

    # (c) notification arrived but NOTHING answered it -> owed (shape b)
    p3 = write([
        ev("user", 0, message={"content": [{"type": "text", "text": "go"}]}),
        ev("assistant", 1, message={"content": [
            {"type": "tool_use", "name": "Agent", "id": "a1",
             "input": {"prompt": "--background\n\ninvestigate"}}]}),
        ev("user", 100, message={"content": notif}),
    ])
    owed3, why3 = is_owed_turn(p3)
    assert owed3 and "no assistant follow-up" in why3, (owed3, why3)
    p3.unlink()

    # (d) a plain trailing human message with nothing dispatched -> not owed (normal idle)
    p4 = write([
        ev("user", 0, message={"content": [{"type": "text", "text": "go"}]}),
        ev("assistant", 1, message={"content": [{"type": "text", "text": "sure, one sec"}]}),
        ev("user", 2, message={"content": [{"type": "text", "text": "any updates?"}]}),
    ])
    owed4, why4 = is_owed_turn(p4)
    assert not owed4, (owed4, why4)
    p4.unlink()

    # a synchronous (non-backgrounded) Bash dispatch must NOT count as a background job
    p5 = write([
        ev("assistant", 0, message={"content": [
            {"type": "tool_use", "name": "Bash", "id": "b1",
             "input": {"command": "ls", "run_in_background": False}}]}),
    ])
    owed5, why5 = is_owed_turn(p5)
    assert not owed5, (owed5, why5)
    p5.unlink()

    # (e) an external async dispatch (codex --background) queued and never read back -> owed.
    # This is the ce8656e6 shape: a FOREGROUND Bash call, so (a) is blind to it.
    gate = ('CODEX=$(ls ~/.claude/plugins/cache/openai-codex/codex/*/scripts/codex-companion.mjs '
            '| sort -V | tail -1) && node "$CODEX" task --background --cwd /wt --prompt-file /p.md')
    p6 = write([
        ev("assistant", 0, message={"content": [
            {"type": "tool_use", "name": "Bash", "id": "b1", "input": {"command": gate}}]}),
        ev("assistant", 1, message={"content": [{"type": "text", "text": "Dispatched. Waiting."}]}),
    ])
    owed6, why6 = is_owed_turn(p6)
    assert owed6 and "external async dispatch" in why6, (owed6, why6)
    p6.unlink()

    # (f) same dispatch, but the session went and read the job back -> not owed
    p7 = write([
        ev("assistant", 0, message={"content": [
            {"type": "tool_use", "name": "Bash", "id": "b1", "input": {"command": gate}}]}),
        ev("assistant", 1, message={"content": [
            {"type": "tool_use", "name": "Bash", "id": "b2",
             "input": {"command": 'node "$CODEX" status --all --json'}}]}),
    ])
    owed7, why7 = is_owed_turn(p7)
    assert not owed7, (owed7, why7)
    p7.unlink()

    # (g) a foreground dispatch WITHOUT --background (the fixed land-gate shape) -> not owed;
    # the harness owes a real notification for it, so shape (a) covers it and (e) must not fire.
    p8 = write([
        ev("assistant", 0, message={"content": [
            {"type": "tool_use", "name": "Bash", "id": "b1",
             "input": {"command": 'timeout 1800 node "$CODEX" task --cwd /wt --prompt-file /p.md',
                       "run_in_background": True}}]}),
        ev("user", 100, message={"content": ("<task-notification>\n<task-id>j9</task-id>\n"
                                             "<tool-use-id>b1</tool-use-id>\n</task-notification>")}),
        ev("assistant", 101, message={"content": [{"type": "text", "text": "verdict in"}]}),
    ])
    owed8, why8 = is_owed_turn(p8)
    assert not owed8, (owed8, why8)
    p8.unlink()

    # --- candidates(): mtime stat filter ---
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "-fake-project"
        proj.mkdir()
        f_fresh = proj / "fresh.jsonl"; f_fresh.write_text("{}\n")
        f_stale = proj / "stale.jsonl"; f_stale.write_text("{}\n")
        f_ancient = proj / "ancient.jsonl"; f_ancient.write_text("{}\n")
        now = time.time()
        os.utime(f_fresh, (now - 60, now - 60))                       # 1 min ago: too fresh
        os.utime(f_stale, (now - 3600, now - 3600))                   # 1h ago: candidate
        os.utime(f_ancient, (now - 100 * 3600, now - 100 * 3600))     # 100h ago: aged out
        orig_projects = ss.PROJECTS
        ss.PROJECTS = Path(td)
        try:
            found = {p.name for p, _age in candidates(now)}
        finally:
            ss.PROJECTS = orig_projects
        assert found == {"stale.jsonl"}, found

    # --- scan(): dedup state (one notification per stuck instance, not per poll) ---
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "-fake-project"
        proj.mkdir()
        stuck = proj / "stuck.jsonl"
        stuck.write_text("\n".join(json.dumps(l) for l in [
            ev("user", 0, message={"content": [{"type": "text", "text": "go"}]}),
            ev("assistant", 1, message={"content": [
                {"type": "tool_use", "name": "Agent", "id": "a1",
                 "input": {"prompt": "--background\n\ninvestigate"}}]}),
        ]) + "\n")
        now2 = time.time()
        os.utime(stuck, (now2 - 3600, now2 - 3600))
        orig_projects, orig_reports = ss.PROJECTS, ss.REPORTS
        ss.PROJECTS = Path(td)
        ss.REPORTS = Path(td) / "reports"
        global STATE_PATH
        orig_state_path = STATE_PATH
        STATE_PATH = ss.REPORTS / "work" / "watchdog-state.json"
        try:
            first = scan()
            assert len(first) == 1 and "stuck" in first[0], first
            second = scan()   # same mtime, still stuck -> suppressed, not re-flagged
            assert second == [], second
            # a DIFFERENT (still-stale) mtime simulates a new event that itself later went
            # stale again - a distinct stuck instance, so it must re-flag
            os.utime(stuck, (now2 - 4000, now2 - 4000))
            third = scan()
            assert len(third) == 1, third
        finally:
            ss.PROJECTS, ss.REPORTS = orig_projects, orig_reports
            STATE_PATH = orig_state_path

    print("selftest OK")


def main():
    args = sys.argv[1:]
    if not args or args[0] == "scan":
        scan()
    elif args[0] == "selftest":
        selftest()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
