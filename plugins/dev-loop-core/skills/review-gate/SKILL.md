---
name: review-gate
description: Run the codex+agy review GATE over a plan or a branch diff before shipping - codex is the ship/no-ship blocking gate, agy is advisory. Use ONLY when the user says "review gate", "gate this/the plan/the diff", "run the review gate", asks to gate work before a merge, or when /ship needs its diff-stage review. Do NOT use for ordinary or local code review (that is /code-review or /simplify) - this is the expensive multi-tool boundary gate. Dispatches the reviewers in parallel, polls for completion with a wedge watchdog, collects verdicts, and stops for a human. Does NOT auto-fix, auto-re-gate, or auto-merge (v2).
---

# review-gate

Drive **codex** and **agy** as review gates over one unit of work - a **plan** (before
coding) or a **branch diff** (before merge) - then stop for a human decision.

**Roles (fixed):** Claude orchestrates. **codex = ship/no-ship gate** (its BLOCK means do not ship).
**agy = advisory, and OFF by default** (see below). **v1 boundary:** this skill always
stops for a human; it never fixes findings, re-gates to verify, commits, merges, pushes, or ExitWorktrees.

**agy is opt-in, not routine.** Over a measured week agy returned `revisit`/`rethink` on **78% of 91
dispatches** (5 `proceed`, 11 empty) - on work codex went on to SHIP. A signal that fires almost
always carries almost no information, and agy can never block, so paying it every round buys a
near-constant negative plus the time to read it. **Dispatch agy only when you have a specific reason:**
the diff is in a domain codex has been weak on, codex's verdict is ambiguous and you want a second
read, or the user asks. Otherwise skip it and report `agy: not run (advisory, opt-in)` - which is
honest, and is *not* an agy pass. Everything below about agy applies when you do run it.

Scripts it uses live in `${CLAUDE_PLUGIN_ROOT}/skills/review-gate/scripts/` and are **NOT on `PATH`** - always
call them by full path (or set `RG=${CLAUDE_PLUGIN_ROOT}/skills/review-gate/scripts` and use `"$RG/..."`). Both are
`--selftest`ed:
- `$RG/review-gate-lock.sh acquire|record|heartbeat|release` - the per-worktree lock + reconnect state.
- `$RG/watch-agent-output.sh <job-log-file>` - the wedge watchdog (exact-file mode).

(Below, script names are written in shorthand for readability; invoke them by the full `$RG/...` path.)

## Plan stage

Review a design/plan doc (or a staged change) before any code is written.

**Hard cap: 2 rounds on a prose target.** A design doc has no fixed point - an adversarial reviewer can
always find another hole in prose, because prose has no compiler and no suite to bound it. Measured: one
design ran **11 rounds, every one `needs-attention`, and shipped nothing**. After round 2, stop
gating and go build the smallest real slice; the code, its tests, and the diff-stage gate answer the
remaining questions with evidence instead of argument. Round 2 ending in BLOCK is a signal the *design
is not ready to be specified further*, not an invitation to round 3.

(A staged change containing real code is a diff target, not a prose target - the 3-round `gate-loop`
cap applies to that.)

1. Stage the unit so it shows in `git diff --cached`.
2. `review-gate-lock.sh acquire "git diff --cached -- <doc>"` and branch on the printed verdict
   (see **Concurrency**). On `ACQUIRED`, dispatch; on `RECONNECT`, poll the printed jobs.
3. Dispatch **codex** (and **agy** only if you opted in above - then both in parallel, fire-and-forget
   background jobs; with agy skipped, pass an empty agy job id to `record`):
   - **codex.** THE PREAMBLE MUST MATCH THE REVIEWER'S ACTUAL TOOL SET. A preamble that misdescribes
     it is what manufactures a spurious BLOCK (2026-08-06 audit: this step once prescribed the
     Read/Glob/Grep preamble while listing the `task` path first, so every plan-stage round shipped a
     preamble that was false in both halves).

     **As of codex plugin `1.0.6` BOTH codex paths have the SAME tool set - a shell, and no
     Read/Glob/Grep - so both take the shell preamble.** They were documented as opposites until
     2026-08-12; see the measurement under `adversarial-review`. The rule is kept as "match the
     reviewer" rather than collapsed to "always send the shell preamble", because a future version
     can change the tool set again and the failure is silent when it does: the reviewer either
     ignores the false claim (and you learn nothing) or believes it and refuses. **Verify against a
     job log before trusting either entry.**
     - **`/codex:adversarial-review` (PREFERRED for a diff).** **It HAS a shell and does NOT have
       Read/Glob/Grep** - the same tool set as the `task` path below, so give it the same SHELL
       preamble. Accepts `--base <ref>`; **only this path does.** Preamble: *"You have a shell.
       Derive the diff yourself: `git diff <base>...HEAD`. Nothing is inlined in this prompt. Read
       whatever files you need with shell commands. Treat suite pass/fail as given, or flag 'suite
       result not provided' as a finding."* **Do NOT claim the diff is inlined** - the plugin inlines
       it only when the change touches <=2 files AND <=256 KB (`lib/git.mjs`), thresholds the caller
       cannot raise, so on any realistic branch the reviewer must derive the diff itself.

       > **Corrected 2026-08-12 (codex plugin `1.0.6`), measured.** This entry used to say
       > "read-only sandbox, has Read/Glob/Grep, no shell" and prescribe *"Bash and Skill are
       > DISABLED. Use Read/Glob/Grep only."* **Both halves are false**, and the cost is not
       > cosmetic - the false preamble stops the reviewer investigating at all. Same tool, same
       > version, three runs in one session: with the "no shell" preamble the reviewer ran **0**
       > commands and returned `needs-attention` with a `[critical]` finding that it "cannot perform
       > the requested grounded adversarial review because repository file access is disabled"; with
       > the shell preamble it ran **17** and **12** shell commands respectively and returned real
       > verdicts. **That refusal is a spurious non-verdict, not a BLOCK.** On one, re-dispatch with
       > a corrected preamble - never read it as a finding, and never reach for
       > `LANDER_VERDICT_OVERRIDE`, which waives the REVIEWER to paper over a DISPATCH error. The
       > tell is in the job log: `[codex] Running command: /bin/zsh -lc ...` means it has a shell,
       > whatever this file says. Re-check this entry against the log if the plugin version moves.
     - **`codex:codex-rescue` / `task` (use when you need an anchored `VERDICT:` line, e.g. for the
       lander interlock).** This agent's tool set is **Bash only - it has NO Read/Glob/Grep**, i.e.
       the SAME as `adversarial-review` above, so both take a shell preamble; the paths differ only
       in `--base` support and output format, not in tools. Preamble: *"You have a shell and no
       file-reading tools. Derive
       the diff yourself with `git diff <base>...HEAD`; nothing is inlined unless this prompt says so.
       Treat suite pass/fail as given, or flag it as a finding."* Also request read-only explicitly:
       `codex-rescue` defaults to `--write` (`workspace-write`) unless asked otherwise, so a gate that
       merely *says* it is read-only is dispatching a write-capable reviewer. Prose is not a sandbox.
     Then point at the exact file; demand the two integration-bug classes reviewers routinely miss -
     (1) **wiring/dead-code** (a module with correct logic that is never imported/registered/wired ->
     a shipped no-op), (2) **concurrency-path routing** (a new lock/queue/idempotency layer that one
     caller still bypasses); ask for a verdict line **SHIP / SHIP-WITH-CHANGES / BLOCK** with findings
     tied to `file:line`.
   - **agy** by a DIRECT Bash call to the companion - NOT the `agy:agy-rescue` subagent, which is a
     contractual `task`-only forwarder that refuses `adversarial-review` (it may run once then refuse
     mid-gate; do not depend on it). Resolve the wrapper version-robustly and invoke `adversarial-review`
     (its built-in adversarial prompt; agy verdict vocab is proceed/revisit/rethink - advisory only):
     ```
     AGY=$(ls ~/.claude/plugins/cache/agy-plugin-cc/agy/*/scripts/agy-companion.mjs | sort -V | tail -1)
     node "$AGY" adversarial-review "The full diff is inlined in this prompt - review it as given. Treat suite pass/fail as given, or flag 'suite result not provided' as a finding. Focus: wiring/dead-code + concurrency-path routing."
     ```
     With no `--base` it reviews `git diff HEAD`, falling back to `git diff --cached` - so a staged plan
     is picked up. It prints `Job ID:` and `Get results: /agy:result <id>` (NOT a `Log:` line - that is
     only printed by `task`). Parse the Job ID for the lock record; the agy job's log path is the
     `logFile` field of its job JSON (`~/.agy-plugin-cc/<hash>/job-<id>.json`), or read the output with
     `/agy:result <id>` - that log is what you poll / watchdog. This wrapper adds
     `--dangerously-skip-permissions` itself; it is NOT the raw `agy --dangerously-skip-permissions`
     string the classifier blocks.
4. On dispatch, `review-gate-lock.sh record <codex_job> <agy_job> <round>` so a compacted/crashed
   session reconnects instead of re-dispatching. Then **Stuck workers** -> **Resilience** -> **Reporting**.

## Diff stage

Review a branch diff before merge (the path `/ship` delegates to). Same as plan stage except scoping:

- Resolve `<base>` (usually `main`; ask if ambiguous). Confirm `git diff <base>...HEAD` is non-empty -
  if empty, STOP and report "nothing to ship" (never a clean verdict on empty input).
- **codex:** `/codex:adversarial-review --base <base>` (scopes to the whole branch diff). Use the
  `adversarial-review` preamble from the plan stage - **shell, no file-reading tools**, and **no claim
  that the diff is inlined**, since at >2 files the plugin tells the reviewer to derive it itself.
  (This bullet said "read-only, Read/Glob/Grep" until 2026-08-12; it is the same stale claim
  corrected at the plan stage, one section over. Both sites are the same rule, so they move together.)
- **agy:** the companion `adversarial-review` supports `--base`, so it reviews the SAME range as
  codex - dispatch it by the same direct Bash call as the plan stage (never the `agy:agy-rescue`
  subagent). agy is a SEPARATE CLI: Claude's tool set is irrelevant to it, and it genuinely does
  receive the whole diff in its prompt, so it gets its own short preamble - never codex's:
  `node "$AGY" adversarial-review --base <base> "The full diff is inlined in this prompt - review it
  as given. Treat suite pass/fail as given, or flag 'suite result not provided' as a finding. Focus:
  wiring/dead-code + concurrency-path routing."` (runs `git diff <base>...HEAD`). Caveat: the companion hard-rejects a diff >200 KB (`process.exit(1)`); on
  an oversized diff report the agy gap **honestly as a tooling limitation, never as an agy pass**.
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
  comes from the agy job JSON's `logFile` field). Neither return is the verdict. Arm the watchdog on
  codex/agy job logs, not at dispatch time.
  **Resolve and record BOTH job-log paths in the same turn you dispatch** - the codex one via the
  job-id glob below, the agy one from its `logFile` field. A job id with no resolved path is what
  turns every later poll into a directory hunt.
- **Poll for completion - the mechanic DIFFERS by tool:**
  - **codex:** poll the job JSON's `result`. **Resolve its path by the JOB ID alone - never go
    looking for the workspace directory.** The `<workspace>` segment is a per-worktree
    `<slug>-<hash>` that you cannot predict, and hunting it cost an 8-call state-dir search on
    2026-08-13 and again on 2026-08-14 (`rec:2026-08-13#8`, re-scoped by `rec:2026-08-14#4`).
    The job id is unique across workspaces, so ONE glob finds it:
    ```
    ls ~/.claude/plugins/data/codex-openai-codex/state/*/jobs/<JOB_ID>.json
    ```
    Run that in the SAME Bash call that records the job id, and keep the resolved absolute path
    for every later poll. The moment `result.rawOutput` is populated, read it and stop. `result`
    is an already-parsed dict: `json.load(...)['result'].get('rawOutput','')` - never
    `json.loads` it, never a cwd-relative path. (`/codex:result <id>` also works and needs no
    path at all; the glob is for when you want the file itself, e.g. to arm the watchdog.)
  - **agy:** the agy job JSON (`~/.agy-plugin-cc/<hash>/job-<id>.json`) has NO `result` field - only
    `status`/`pid`/`logFile`. agy's verdict lands in the LOG file (`logFile`), readable via `/agy:result
    <id>` (which just cats the log). Poll that log for the verdict text (agy's `proceed`/`revisit`/`rethink`,
    or a SHIP/BLOCK line if you asked for one) plus pid liveness; done = verdict text in the log.
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
(codex+agy) boundary gate you invoke at a plan or pre-merge point. A green stop-hook is not a
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
  ACQUIRED)   dispatch codex (+agy only if opted in); then "$RG/review-gate-lock.sh" record <codex_job> <agy_job|omit> <round> ;;
  RECONNECT)  poll $j_codex / $j_agy - do NOT re-dispatch. $j_agy = `-` means agy was never dispatched (opt-in): there is nothing to poll and nothing to report as an agy result ;;
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
- **codex must pass** - a ship requires **codex `SHIP`**. `SHIP-WITH-CHANGES` is not-a-pass. agy is
  advisory only and never blocks alone.
- **Explicit verdict required** - the codex result must contain a SHIP/SHIP-WITH-CHANGES/BLOCK token; a
  completed-but-empty or token-less result is a non-pass -> BLOCK-equivalent.
- **Null job = BLOCK-equivalent** - a rescue agent that dies on a terminal API error, or a job with no
  result, is never a skip.
- **Wedge vs done** - completion is codex's populated `result` OR agy's verdict text in its log file;
  watchdog-fired while that is still absent = wedged -> salvage, cancel, one retry, then fail closed.
- **Empty input = STOP** - nothing staged / `git diff <base>...HEAD` empty -> "nothing to ship".
- **Oversized diff** - run `$RG/diff-size.sh <base> HEAD` (co-located with the lock/watchdog scripts)
  BEFORE dispatch instead of eyeballing two different thresholds: `BYTES=ERROR` (bad ref / failed diff)
  is a human stop; `AGY=OVERSIZE` is agy's honest oversize gap (a reported gap since agy is advisory,
  never a block); `CODEX=WARN` means split/chunk the change. Never accept a truncated-diff pass ->
  BLOCK-equivalent.
- **Dropped resume** - see **Re-gate**: fall back to fresh, never double-retry.

## Reporting (stop for a human)

Always end here, clean or not:
- codex verdict **verbatim** (findings included).
- agy findings and whether codex corroborated them - OR, if agy hit the empty-diff gap, say so
  plainly instead of implying a clean agy pass.
- **Pass = codex `SHIP`.** If codex is clean: ask whether to proceed/merge. If codex is BLOCK /
  SHIP-WITH-CHANGES: report what held and ask how to proceed (fix then re-invoke, or an explicit
  logged override).
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
  # $CODEX_VERDICT/$AGY_VERDICT = the SHIP/BLOCK/... you parsed above; the
  # $<NAME>_FINDINGS vars are your per-reviewer triaged findings JSON (default []). One pipe-delimited
  # row per reviewer via a heredoc - NOT colon-packing into one string (that mis-splits job ids/paths).
  while IFS='|' read -r name verdict job findings; do
    [ -n "$verdict" ] || continue
    python3 "$WT/scripts/gate_log.py" verdict --round-id "$RID" --reviewer "$name" \
      --verdict "$verdict" --repo "$(basename "$WT")" --worktree "$WT" \
      --base-sha "$base" --diff-hash "$DH" --job-id "$job" --stage "${STAGE:-diff}" \
      --findings-json "${findings:-[]}" >/dev/null 2>&1 || true
  done <<EOF
codex|$CODEX_VERDICT|${CODEX_JOB:-codex}|${CODEX_FINDINGS:-[]}
agy|$AGY_VERDICT|${AGY_JOB:-agy}|${AGY_FINDINGS:-[]}
EOF
  ```
- `review-gate-lock.sh release`.
- Do NOT: fix a finding, resume-to-verify, commit, merge, push, ExitWorktree, or auto-loop. That is v2.
