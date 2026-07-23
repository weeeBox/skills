---
name: review-gate
description: Run the codex+deepseek+agy review GATE over a plan or a branch diff before shipping - codex AND deepseek are the ship/no-ship blocking gates (deepseek when configured), agy is advisory. Use ONLY when the user says "review gate", "gate this/the plan/the diff", "run the review gate", asks to gate work before a merge, or when /ship needs its diff-stage review. Do NOT use for ordinary or local code review (that is /code-review or /simplify) - this is the expensive multi-tool boundary gate. Dispatches the reviewers in parallel, polls for completion with a wedge watchdog, collects verdicts, and stops for a human. Does NOT auto-fix, auto-re-gate, or auto-merge (v2).
---

# review-gate

Drive **codex**, **deepseek**, and **agy** as review gates over one unit of work - a **plan** (before
coding) or a **branch diff** (before merge) - then stop for a human decision.

**Roles (fixed):** Claude orchestrates. **codex = ship/no-ship gate** (its BLOCK means do not ship).
**deepseek = second ship/no-ship gate, when configured** (an independent model's blocking verdict; its
BLOCK/SHIP-WITH-CHANGES/ERROR/OVERSIZE means do not ship). **agy = advisory** (surfaced and cross-checked,
never blocks alone). **Combination rule: a pass requires codex `SHIP` AND deepseek `SHIP`** (or deepseek
not-configured, see below); `SHIP-WITH-CHANGES` from either blocker is not-a-pass; agy never gates alone.
**deepseek opt-in:** deepseek runs via the installed `deepseek` plugin and needs `DEEPSEEK_API_KEY`. Check
readiness first (`deepseek_companion.py setup --json` -> `"ready"`); if NOT ready, report "deepseek not
configured - skipped" as an honest gap (NOT a pass, NOT a hard block) and gate on codex+agy only. Where
deepseek IS ready it is a required blocker. **v1 boundary:** this skill always stops for a human; it never
fixes findings, re-gates to verify, commits, merges, pushes, or ExitWorktrees.

Scripts it uses live in `${CLAUDE_PLUGIN_ROOT}/skills/review-gate/scripts/` and are **NOT on `PATH`** - always
call them by full path (or set `RG=${CLAUDE_PLUGIN_ROOT}/skills/review-gate/scripts` and use `"$RG/..."`). Both are
`--selftest`ed:
- `$RG/review-gate-lock.sh acquire|record|heartbeat|release` - the per-worktree lock + reconnect state.
- `$RG/watch-agent-output.sh <job-log-file>` - the wedge watchdog (exact-file mode).

(Below, script names are written in shorthand for readability; invoke them by the full `$RG/...` path.)

## Plan stage

Review a design/plan doc (or a staged change) before any code is written.

1. Stage the unit so it shows in `git diff --cached`.
2. `review-gate-lock.sh acquire "git diff --cached -- <doc>"` and branch on the printed verdict
   (see **Concurrency**). On `ACQUIRED`, dispatch; on `RECONNECT`, poll the printed jobs.
3. Dispatch **codex** and **agy** in parallel (both fire-and-forget background jobs):
   - **codex** via the `codex:codex-rescue` agent (or `/codex:adversarial-review`). Prompt: point at
     the exact file; demand the two integration-bug classes reviewers routinely miss - (1) **wiring/
     dead-code** (a module with correct logic that is never imported/registered/wired -> a shipped
     no-op), (2) **concurrency-path routing** (a new lock/queue/idempotency layer that one caller
     still bypasses); ask for a verdict line **SHIP / SHIP-WITH-CHANGES / BLOCK** with findings tied
     to `file:line`.
   - **agy** by a DIRECT Bash call to the companion - NOT the `agy:agy-rescue` subagent, which is a
     contractual `task`-only forwarder that refuses `adversarial-review` (it may run once then refuse
     mid-gate; do not depend on it). Resolve the wrapper version-robustly and invoke `adversarial-review`
     (its built-in adversarial prompt; agy verdict vocab is proceed/revisit/rethink - advisory only):
     ```
     AGY=$(ls ~/.claude/plugins/cache/agy-plugin-cc/agy/*/scripts/agy-companion.mjs | sort -V | tail -1)
     node "$AGY" adversarial-review "focus: wiring/dead-code + concurrency-path routing"
     ```
     With no `--base` it reviews `git diff HEAD`, falling back to `git diff --cached` - so a staged plan
     is picked up. It prints `Job ID:` and `Get results: /agy:result <id>` (NOT a `Log:` line - that is
     only printed by `task`). Parse the Job ID for the lock record; the agy job's log path is the
     `logFile` field of its job JSON (`~/.agy-plugin-cc/<hash>/job-<id>.json`), or read the output with
     `/agy:result <id>` - that log is what you poll / watchdog. This wrapper adds
     `--dangerously-skip-permissions` itself; it is NOT the raw `agy --dangerously-skip-permissions`
     string the classifier blocks.
   - **deepseek** (second blocker, when configured) by a DIRECT Bash call to the installed plugin's
     companion, dispatched as a Claude `run_in_background: true` job (the companion is synchronous - the
     bg job completing IS completion; no separate job-JSON poll needed). Resolve it version-robustly, LOAD
     `.env` FIRST (the key usually lives there, and `setup` reads the env), THEN check readiness; skip-with-
     note if not ready:
     ```
     DS=$(ls ~/.claude/plugins/cache/deepseek/deepseek/*/scripts/deepseek_companion.py 2>/dev/null | sort -V | tail -1)
     # DEEPSEEK_API_KEY lives in the MAIN checkout's .env; a worktree has no ./.env, so resolve the
     # main root from the git common dir (works from both the main checkout and any worktree):
     set -a; _RGE="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")/.env"; [ -f "$_RGE" ] && . "$_RGE"; set +a   # must precede setup
     [ -n "$DS" ] && python3 "$DS" setup --json          # gate on "ready": true
     ```
     If `ready` is false (or `$DS` empty): report "deepseek not configured - skipped" (honest gap, not a
     pass, not a block) and gate on codex+agy only - **UNLESS `RG_REQUIRE_DEEPSEEK=1` is set** (the
     autonomous consumers gate-loop/land/ship set it), in which case a not-configured deepseek is itself
     a fail-closed human STOP (BLOCK-equivalent), never a codex-only pass. If ready, dispatch (same shell so the key is loaded)
     via `run_in_background: true`, writing the verdict to a per-round scratchpad file:
     ```
     python3 "$DS" adversarial-review "$RGSCOPE" > "$VF" 2>&1   # $VF = <scratchpad>/deepseek-verdict-r<round>.txt
     ```
     `$RGSCOPE` is a fixed scope-discipline focus string passed to EVERY deepseek dispatch (plan AND
     diff). The reviewer is a Bash-DISABLED `claude -p --tools Read,Grep,Glob` subagent, so without it the
     reviewer opens with a doomed `git diff`/`git log` and guesses `src/<subdir>/` paths that 404 (07-16..19
     retros P1/P2). Passing it through the companion's existing free-form focus arg keeps the guidance in
     THIS skill (durable) instead of the vendored, reinstall-ephemeral prompt template:
     ```
     RGSCOPE="SCOPE DISCIPLINE: Bash, git, and Skill are DISABLED - never run git/ls/cat/find/rg and never invoke Skill (incl. review-gate) to orchestrate. The full diff is inlined below and its file paths are AUTHORITATIVE - resolve every path against them, never guess a sibling's src/<subdir>/ from memory (e.g. src/engine/ when the real path is src/app/); on a 404, Glob the basename ONCE (never re-fire the same path at a new offset). If the diff touches only docs/designs/** or docs/plans/**, this is a DESIGN-only gate - files it names may not exist yet, so Glob to confirm before Reading a named source/artifact; never Read a design-referenced file blind. Your target worktree can be reaped mid-review: on the FIRST 'Directory does not exist', an rg/posix_spawn ENOENT, or a File-does-not-exist on a path that read fine seconds earlier, STOP all filesystem probing (the tree is gone, no retry fixes it) and complete the verdict from the inlined diff plus whatever you already read. List a directory with Glob dir/**, never ls, and never Read a directory."
     ```
     The verdict is the **last `VERDICT:` line** of `$VF` (the companion fails closed: any error/missing
     key/oversize -> `VERDICT: ERROR`, never a silent pass). deepseek reviews as a read-only agent that
     can grep beyond the diff.
4. On dispatch, `review-gate-lock.sh record <codex_job> <agy_job> <round>` so a compacted/crashed
   session reconnects instead of re-dispatching. Record the deepseek dispatch too: its verdict lives at
   the deterministic `$VF` path, so on `RECONNECT` re-read `$VF` (a resolved `VERDICT:` line = done; absent
   = re-dispatch). Then **Stuck workers** -> **Resilience** -> **Reporting**.

## Diff stage

Review a branch diff before merge (the path `/ship` delegates to). Same as plan stage except scoping:

- Resolve `<base>` (usually `main`; ask if ambiguous). Confirm `git diff <base>...HEAD` is non-empty -
  if empty, STOP and report "nothing to ship" (never a clean verdict on empty input).
- **codex:** `/codex:adversarial-review --base <base>` (scopes to the whole branch diff).
- **agy:** the companion `adversarial-review` NOW supports `--base`, so it reviews the SAME range as
  codex - dispatch it by the same direct Bash call as the plan stage (never the `agy:agy-rescue`
  subagent), just add the base: `node "$AGY" adversarial-review --base <base>` (runs
  `git diff <base>...HEAD`). Caveat: the companion hard-rejects a diff >200 KB (`process.exit(1)`); on
  an oversized diff report the agy gap **honestly as a tooling limitation, never as an agy pass**.
- **deepseek:** same readiness check and background dispatch as the plan stage, just add the base (and
  the same `$RGSCOPE` focus string):
  `python3 "$DS" adversarial-review --base <base> "$RGSCOPE"` (reviews `git diff <base>...HEAD`, the SAME
  range as codex). Its own char guard (`DEEPSEEK_MAX_DIFF_CHARS`, ~1M) turns an oversized diff into
  `VERDICT: OVERSIZE` = a human stop, never a pass.
- **Invariant - the deepseek reviewer is Bash-DISABLED (`--tools Read,Grep,Glob`), so it CANNOT run
  `git diff`/`git log`.** It depends on the companion inlining the full diff into its prompt
  (`deepseek_companion.py` `do_review` -> `tmpl.format(diff=diff)`; the `ADVERSARIAL`/`PLAIN` templates
  carry a `{diff}` block). Never "improve" the companion or a future reviewer to expect it to shell out
  for the diff - a keyword-hit `Grep` list is NOT the diff (2026-07-16 retro: `5fe81dd0` asserted "9
  markdown files" from a `deepseek`-keyword grep, having never seen the diff).
- **Reviewer-side fallback (any Bash-disabled reviewer, when a diff is somehow absent):** establish
  scope BEFORE reading source - `Glob` the files the branch's plan/commit doc claims to touch (a branch
  that changes only `docs/plans/*.md` needs the PLAN reviewed, not the pre-existing code it references),
  and to locate a symbol grep the bare name (`\bNAME\b`), not `def NAME` (imports, re-exports, and
  `X = Y` aliases have no local `def`). Evidence: `a5b90104` spent ~8 min tracing out-of-scope source.
- Lock scope command: `review-gate-lock.sh acquire "git diff <base>...HEAD"`.

### Generated / build-output cleanup

A dirty generated tree (build output, a tool-generated dir) pollutes the diff the reviewers see. By
default `git checkout -- <generated-dir>` to drop disposable tool noise before scoping - **but only
where the repo treats it as disposable.** A repo whose own conventions track that dir as real work must
instead STOP and ask the human; never run a destructive git command on it. A consuming skill (e.g.
`ship`) may override this step for a repo where the generated dir is not disposable.

## Stuck workers

Completion is **poll-driven**, the watchdog is only a **wedge** backstop.

- The dispatches are fire-and-forget background jobs that return BEFORE the review finishes - codex via
  the rescue subagent's completion notification, agy via the companion's printed `Job ID:` (its log path
  comes from the agy job JSON's `logFile` field), deepseek via its `run_in_background` bg-job completion
  notification (verdict in `$VF`). None of these returns is the verdict. Arm the watchdog on codex/agy job
  logs, not at dispatch time.
- **Poll for completion - the mechanic DIFFERS by tool:**
  - **codex:** poll the job JSON's `result` (`~/.claude/plugins/data/codex-openai-codex/state/<workspace>/jobs/<id>.json`
    or `/codex:result`). The moment `result.rawOutput` is populated, read it and stop. `result` is an
    already-parsed dict: `json.load(...)['result'].get('rawOutput','')` - never `json.loads` it, never a
    cwd-relative path.
  - **agy:** the agy job JSON (`~/.agy-plugin-cc/<hash>/job-<id>.json`) has NO `result` field - only
    `status`/`pid`/`logFile`. agy's verdict lands in the LOG file (`logFile`), readable via `/agy:result
    <id>` (which just cats the log). Poll that log for the verdict text (agy's `proceed`/`revisit`/`rethink`,
    or a SHIP/BLOCK line if you asked for one) plus pid liveness; done = verdict text in the log.
  - **deepseek:** the companion is SYNCHRONOUS, so its Claude `run_in_background` Bash job completing IS
    completion - the harness notifies you; no job-JSON poll. Done = the bg job's output file `$VF` has a
    resolved last `VERDICT:` line (`SHIP` / `SHIP-WITH-CHANGES` / `BLOCK` / `OVERSIZE` / `ERROR`). Liveness
    is the bg job's own lifecycle. (The companion also records `~/.deepseek-plugin-cc/<hash>/jobs/<id>.json`
    for `/deepseek:status`, but the gate reads `$VF`, not that.)
- **Liveness is pid-based, not just mtime.** Both job JSONs record the worker `pid` and a streaming
  `logFile`. `kill -0 <pid>` alive + log growing = working; **pid DEAD with no verdict yet (empty codex
  `result`, or no verdict text in the agy log) = crashed -> fail closed immediately** (do not wait the
  timeout). Never trust `status`/`phase` - they read "running" after death. Run `watch-agent-output.sh <exact-job-log-file>` as the wedge
  backstop **in a `run_in_background: true` Bash job - NEVER a foreground call** (a foreground watchdog
  inherits the Bash tool's 2-min timeout and SIGTERMs at the ceiling, killing the poll: 2026-07-16 retro
  `d4e1b696`/`c9ab2420` both `Exit code 143`). Read its result once on the completion notification; if it
  returns while there is still no verdict, salvage the log, `/codex:cancel` / `/agy:cancel`, one retry, then fail closed.

## Re-gate on a new round

After a fix, **resume the same codex agent** (SendMessage to its agentId, or `codex exec resume`) for
warm context. But a resume can silently drop or refuse the message (observed: "nothing to forward") -
for a gate that could read as "no issues". So on ANY empty/no-verdict resume, treat it as not-a-pass
and fall back to a FRESH dispatch with the prior verdict inlined; never retry the resume (double-drops
are real). One drop ends resume for the session. In v1 the skill does NOT auto-run this loop - it stops;
the human re-invokes after fixing.

## Coexistence with the codex stop hook

The codex plugin ships a `stop-review-gate` hook that auto-runs a per-turn ALLOW/BLOCK on any
edit-producing turn - a cheap continuous safety net. This skill is different: an EXPLICIT, multi-tool
(codex+deepseek+agy) boundary gate you invoke at a plan or pre-merge point. A green stop-hook is not a
substitute for this gate, and this skill must not be invoked on every turn (that doubles review spend).
Hook = per-turn net; skill = boundary gate.

## Execution model

A gate is a **multi-tool-call Claude orchestration, not one shell process.** Each Bash call is its own
short-lived shell, so `$$` dies immediately and `trap ... EXIT` fires at the end of that single call
(not the gate); shell vars do not survive across turns. Durable state therefore lives in a FILE
(re-resolved by path), and crash-safety is by mtime, not PID. This is why the lock is a script with a
persisted state file, not inline bash.

## Durable gate state

`review-gate-lock.sh` owns a per-worktree lock dir under `$(git rev-parse --absolute-git-dir)` (verified
per-worktree: main -> `.git/...`, worktree -> `.git/worktrees/<name>/...`). Its `state` file carries the
session `owner`, the `diff_hash`, the codex/agy job ids, and a timestamp. It is lock + reconnect record +
orphan-cleanup marker in one. A `heartbeat` (call it each poll cycle) refreshes the mtime so "stale"
means genuinely dead. Cleanup is `review-gate-lock.sh release` at the clean end AND every early-exit path.

## Concurrency

**One gate per worktree; never two in one checkout.** Two invariants back this:
- **Hard (same session):** a single Claude session runs sequentially - it cannot have two
  concurrently-live gates with the same owner, which is what makes `owner==mine` reconnect/reclaim safe.
- **Soft (cross session):** the worktree-guard hook is a NUDGE (it *asks* before a primary-checkout
  mutation, has bypasses, and does nothing once you are in a worktree) - it makes two-gates-one-checkout
  uncommon, not impossible.

So the lock is a **best-effort advisory backstop**, not a hard mutex; it fails closed (BLOCKED) on any
ambiguous different-owner lock. Drive it by the `acquire` verdict:

```
RG=${CLAUDE_PLUGIN_ROOT}/skills/review-gate/scripts        # bundled scripts are NOT on PATH - call by full path
verdict=$("$RG/review-gate-lock.sh" acquire "<SCOPE_CMD>") || { echo "lock error -> FAIL CLOSED"; exit 1; }
read -r tag j_codex j_agy round <<< "$verdict"  # `read` splits on IFS in BOTH bash and zsh; `set -- $verdict` does NOT split in zsh (the user's shell)
case "$tag" in
  ACQUIRED)   dispatch codex+deepseek+agy; then "$RG/review-gate-lock.sh" record <codex_job> <agy_job> <round> (deepseek verdict lives at $VF) ;;
  RECONNECT)  poll $j_codex / $j_agy - do NOT re-dispatch ;;
  RECLAIM)    cancel $j_codex $j_agy (may be empty); acquire again (lockdir already removed -> ACQUIRED); same fail-closed on the re-acquire ;;
  BLOCKED)    STOP now: another session is gating this checkout; use a separate worktree. Do NOT run `release` (you do not own the lock) and do NOT fall through to Reporting's release ;;
  *)          echo "unknown verdict '$verdict' -> FAIL CLOSED"; exit 1 ;;
esac
```
`release` is owner-guarded (it only removes YOUR lock), but a BLOCKED session must still exit immediately rather than continue to Reporting - it never acquired anything.

Residual (documented v1 limitation): two DIFFERENT sessions in one checkout hitting the sub-second
bootstrap window can both reach ACQUIRED. The operational rule "one gate per worktree" covers it; a
lease+liveness mutex is v2.

## Resilience

A gate must **FAIL CLOSED**: any ambiguity = not verified = do NOT ship, surface to the human. Never
read an error, timeout, empty result, or dropped message as ALLOW.
- **Both blockers must pass** - a ship requires **codex `SHIP` AND deepseek `SHIP`** (or deepseek
  not-configured-and-skipped). `SHIP-WITH-CHANGES` from either is not-a-pass. agy is advisory only.
- **Explicit verdict required** - the codex result must contain a SHIP/SHIP-WITH-CHANGES/BLOCK token; a
  completed-but-empty or token-less result is a non-pass -> BLOCK-equivalent.
- **deepseek fail-closed states** - `ERROR` (missing key / API failure / unparseable) and `OVERSIZE`
  (diff too large) are **human stops, never a pass**. A deepseek `run_in_background` job that dies with no
  resolved `VERDICT:` line in `$VF` = crashed -> BLOCK-equivalent. Only `deepseek not-configured` (setup
  not ready) is an honest skip that does not block **at THIS general-purpose gate** (a human is present
  to decide); every other non-`SHIP` deepseek outcome blocks.
- **Two-tier caveat (skip is NOT universal):** the autonomous consumers that declare deepseek a
  **required** blocker - `gate-loop`, `land`, `ship` (see their SKILLs; see the README)
  - do NOT inherit this skip-and-proceed. For them a genuinely not-configured deepseek is itself a
  **fail-closed human stop**, never a codex-only pass, because a degraded gate in an unattended pipeline
  is unacceptable. The consuming skill's stricter stance controls, since it is what reads the verdict.
- **Null job = BLOCK-equivalent** - a rescue agent that dies on a terminal API error, or a job with no
  result, is never a skip.
- **Wedge vs done** - completion is codex's populated `result` OR agy's verdict text in its log file;
  watchdog-fired while that is still absent = wedged -> salvage, cancel, one retry, then fail closed.
- **Empty input = STOP** - nothing staged / `git diff <base>...HEAD` empty -> "nothing to ship".
- **Oversized diff** - run `$RG/diff-size.sh <base> HEAD` (co-located with the lock/watchdog scripts)
  BEFORE dispatch instead of eyeballing three different thresholds: `BYTES=ERROR` (bad ref / failed diff)
  is a human stop; `AGY=OVERSIZE` / `DEEPSEEK=OVERSIZE` is that tool's honest oversize gap (a human stop
  for a blocker, a reported gap for agy); `CODEX=WARN` means split/chunk the change. Never accept a
  truncated-diff pass -> BLOCK-equivalent.
- **Dropped resume** - see **Re-gate**: fall back to fresh, never double-retry.

## Reporting (stop for a human)

Always end here, clean or not:
- codex verdict **verbatim** (findings included).
- **deepseek verdict verbatim** (the `$VF` output), OR "deepseek not configured - skipped" if setup was
  not ready. Its last `VERDICT:` line is the blocking result.
- agy findings and whether the blockers corroborated them - OR, if agy hit the empty-diff gap, say so
  plainly instead of implying a clean agy pass.
- **Pass = codex `SHIP` AND deepseek `SHIP` (or deepseek skipped).** If BOTH blockers are clean: ask
  whether to proceed/merge. If EITHER is BLOCK / SHIP-WITH-CHANGES / (deepseek) ERROR/OVERSIZE: report
  which blocker(s) held and ask how to proceed (fix then re-invoke, or an explicit logged override).
- **Gate-round log (observation-only; NEVER blocks the gate).** After the verdicts are known, emit one
  `gate_log.py verdict` per reviewer so a retrospective can compute reviewer precision + blocker
  agreement + plan/diff value (these were previously unmeasurable). The shared round id comes from the
  lock (minted at `acquire`); `$STAGE` is `plan` or `diff`; each `$<NAME>_FINDINGS` is a per-reviewer
  JSON list of
  `{"id":..,"category":"bug|security|correctness|race|fail-open|provenance|money|style|design-nit|false-positive"}`
  from your triage of that reviewer's findings (per-reviewer, so precision is attributable; this
  category is what the severity-gated re-gate rule consumes). Fail-open (`|| true`): logging must never
  fail a gate. `gate_log.py` has no exec bit, so invoke it via `python3` (it is stdlib-only).

  ```bash
  RID="$("$RG/review-gate-lock.sh" round-id)"; WT="$(git rev-parse --show-toplevel)"
  DH="$(sed -n 's/^diff_hash=//p' "$(git rev-parse --absolute-git-dir)/review-gate.lock.d/state")"
  # $CODEX_VERDICT/$DEEPSEEK_VERDICT/$AGY_VERDICT = the SHIP/BLOCK/... you parsed above; the
  # $<NAME>_FINDINGS vars are your per-reviewer triaged findings JSON (default []). One pipe-delimited
  # row per reviewer via a heredoc - NOT colon-packing into one string (that mis-splits job ids/paths).
  while IFS='|' read -r name verdict job findings; do
    [ -n "$verdict" ] || continue
    python3 "$WT/scripts/gate_log.py" verdict --round-id "$RID" --reviewer "$name" \
      --verdict "$verdict" --repo "$(basename "$WT")" --worktree "$WT" \
      --base-sha "$base" --diff-hash "$DH" --job-id "$job" --stage "$STAGE" \
      --findings-json "${findings:-[]}" >/dev/null 2>&1 || true
  done <<EOF
codex|$CODEX_VERDICT|${CODEX_JOB:-codex}|${CODEX_FINDINGS:-[]}
deepseek|$DEEPSEEK_VERDICT|deepseek|${DEEPSEEK_FINDINGS:-[]}
agy|$AGY_VERDICT|${AGY_JOB:-agy}|${AGY_FINDINGS:-[]}
EOF
  ```
- `review-gate-lock.sh release`.
- Do NOT: fix a finding, resume-to-verify, commit, merge, push, ExitWorktree, or auto-loop. That is v2.
