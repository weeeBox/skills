---
name: gate-loop
description: The autonomous in-session verify→fix→re-gate loop that turns a worktree's work into a green, codex-gated `session/<slug>` branch - then STOPS before integration (the lander merges, not this skill). Runs the full suite, agy-advises the working tree, commits, runs a coded test-tamper guard, and gates codex (the required blocker) on the branch diff, looping on BLOCK up to 3 rounds by resuming the same warm codex agent. Use when the user says "gate-loop", "/gate-loop", "loop to green", "run the gate loop", or wants a worktree driven to a gated-green branch autonomously before landing. Do NOT use for a one-shot human-gated review (that is /ship or review-gate) - this is the capped, self-driving multi-round loop. Never claims green on a cap-out, a tamper hit, or a wedge.
---

> **Configure first (opinionated pipeline).** This skill assumes a repo configured via `.dev-loop.conf` at its root (copy `$CLAUDE_PLUGIN_ROOT/dev-loop.conf.example` and edit). Source it so `$TEST_COMMAND`, `$BASE_BRANCH`, and the vendored-lander wiring are set. Requires the `dev-loop-core` plugin (the `review-gate` skill it drives). See the repo README.

# gate-loop

The Stage-1 autonomous loop of the gated-land pipeline (this is an opinionated solo-dev pipeline; see the repo README). It is a
**skill, not a script** on purpose: the session Claude IS the fixer, so it orchestrates the loop the
way `review-gate`/`ship` do, driving the already-built pieces rather than re-implementing them.

**What it produces:** a green `session/<slug>` branch that codex has SHIP'd, with
the test-tamper guard clean. **Where it stops:** before integration. Merging that branch is the lander's job
(`${CLAUDE_PLUGIN_ROOT}/engine/lander.sh`, Stage 2) or a human via `/ship` - this skill never merges, pushes, resets the
target, or `ExitWorktree`s.

**Roles (fixed, same as review-gate):** codex = the ship/no-ship gate - a BLOCK means loop or stop.
agy = advisory only. The **test-tamper guard is a hard
brake** that overrides *any* SHIP from codex: no self-approval on a diff that touched the
verification substrate, period.

## When this fires

Inside a worktree on a `session/<slug>` branch, work is believed done and the user wants it driven to
a gated-green branch autonomously: "gate-loop", "/gate-loop", "loop to green", or before handing off
to the lander. Not for a one-shot review - that is `/ship` (human-gated, one codex pass, no loop).

## Reused pieces (do NOT re-hand-roll)

- your full-suite command (`$TEST_COMMAND` from `.dev-loop.conf`) - the full suite (socket-port-0 isolated; safe to run concurrently).
- **review-gate skill** owns codex/agy dispatch, the wedge watchdog, and the per-worktree
  lock. Drive codex/agy **through the `review-gate` skill** (from the `dev-loop-core` plugin, a hard dependency), do not reinvent the mechanics.
- `${CLAUDE_PLUGIN_ROOT}/skills/gate-loop/scripts/tamper-check.sh <base> [HEAD]` - the coded test-tamper guard
  (exit 3 = tamper). `--selftest`ed.
- **`$VLOG` - the append-only audit trail** (tab-separated `ts<TAB>event<TAB>head<TAB>detail`), resolved
  ONCE at Step 0 and used for every write below. It lives in the **primary checkout**, never the worktree:

  ```bash
  VLOG="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")/.claude/state/verify.log"
  ```

  **Never write it as a bare `.claude/state/verify.log`.** This skill *requires* running in a worktree
  (Step 0), and `.claude/state/` is gitignored, so a cwd-relative append creates a SEPARATE log inside
  the worktree. The `land` skill and `lander.sh` both read the primary checkout's copy - so a
  `gateloop-pass` written cwd-relative is structurally invisible to land's Step-1b SKIP-GATE check,
  and every land pays a second codex round on code the branch gate already SHIP'd. Measured on one
  repo 2026-08-07: 8 `gateloop-pass` rows, 87% of prepares at `OVERLAP=0`, and `land-gate-skipped`
  had fired exactly **once** in the repo's entire history.

## Step 0 - preflight

- Confirm you are **in a worktree** on a `session/<slug>` branch (`git rev-parse --show-toplevel`,
  `git branch --show-current`). If on the primary checkout or `main`, STOP - this loop must not run on
  the integration target.
- Resolve `<base>` (usually `main`; ask only if genuinely ambiguous) and confirm
  `git diff <base>...HEAD` (plus any uncommitted work) is non-empty. Empty → STOP, "nothing to gate".
- **Resolve `$VLOG` now** (see Reused pieces) and use it for every audit write in this skill. Sanity-check
  it points OUTSIDE the worktree - if `$VLOG` starts with `$(git rev-parse --show-toplevel)`, you
  resolved it wrong and `land` will not see your `gateloop-pass`:

  ```bash
  VLOG="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")/.claude/state/verify.log"
  case "$VLOG" in "$(git rev-parse --show-toplevel)"/*) echo "VLOG resolved INTO the worktree - abort"; exit 1 ;; esac
  mkdir -p "$(dirname "$VLOG")"
  ```

## Step 0b - the class sweep (ONCE, before round 1)

**Do this before the first codex dispatch, not after the first BLOCK.** Measured on a week of gate
data, the single largest consumer of rounds is *whack-a-mole inside one defect class*: the reviewer
names one instance, the fix patches that instance, and the next round finds a sibling. One branch spent
**8 rounds** on a single leak class (a saved value escaping through a new path each round: candidate
override, then the discovery response, then mixed queries, then a duplicate fact, then a value-scrub,
then an address-derived id). Another found the *same* finding in three separate rounds.

So, before dispatching:

1. **Name the invariant in one sentence** - what must be true after this diff that was not before
   ("no saved provider value reaches model context", "every retry path honours the deadline").
2. **Enumerate every site that could violate it** - `grep` the whole class, do not reason from the
   files you happened to edit: all return/raise sites, all callers, all branches of the adapter, all
   admitting verbs, every consumer in the map. Quote the command you ran.
3. **Fix or explicitly clear each site**, and add **one class-level regression** (not one per site).
4. **Put the enumeration in the commit message / gate prompt.** It tells the reviewer the class was
   swept, and it makes an omission visible to *you* first.

If you cannot enumerate the class, say so in the prompt and ask codex to enumerate it - that is a far
cheaper round than discovering the siblings one per round. A round that fixes exactly the one instance
named in the prior verdict, with no sweep, is the failure mode this step exists to prevent.

## The loop (max 3 gate rounds)

At loop entry, append one `gateloop-start` row to the audit log so the cap is counted from data, not
memory: `printf '%s\tgateloop-start\t%s\t-\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(git rev-parse --short HEAD)" >> "$VLOG"`.

Repeat the round below. A **round** = one gate attempt (codex). **Cap from data, not memory:**
at the top of each round, if `${CLAUDE_PLUGIN_ROOT}/skills/gate-loop/scripts/round-count.sh "$VLOG"`
returns `>=3`, take the Cap-out path (do NOT start a 4th round). The count is positional (rows after the
last `gateloop-start`), so it survives context compaction and clock drift.

1. **agy advisory - OPT-IN, skipped by default** (see review-gate's role note: 78% of 91 measured
   dispatches returned `revisit`/`rethink` on work codex then SHIP'd, and agy can never block). Run it
   only for a specific reason - a domain codex has been weak on, an ambiguous codex verdict, or on
   request. When skipped, report `agy: not run (advisory, opt-in)`; that is honest and is **not** a
   pass. When you DO run it: agy cannot review a committed branch, so it must see the
   working-tree/staged diff here, BEFORE commit, or be marked N/A (clean tree → N/A, say so; don't
   present an empty agy result as a pass). Advisory only - surface findings, weigh them, never block or
   spend a codex round on agy alone.
2. **Commit** the work on `session/<slug>` (per-task commits as usual).
3. **Test-tamper guard (hard brake, every round over the whole branch diff):**
   `${CLAUDE_PLUGIN_ROOT}/skills/gate-loop/scripts/tamper-check.sh <base>`.
   - Exit 3 → **STOP. Do not self-approve, even if the suite is green and codex would SHIP.** Log
     `gateloop-tamper` to verify.log and hand the branch diff to a human, or dispatch a **dedicated
     codex pass whose sole job is to judge that test/config/deps diff** - the loop cannot clear it.
     **This dispatch gets the SAME wedge watchdog as Step 4's gate, no exceptions and no "I'll wait
     for the natural completion notification":** arm `watch-agent-output.sh <exact-job-log-file>` in
     a `run_in_background: true` Bash job in the same turn you dispatch, and read its result once on
     the completion notification. On STUCK-OR-DONE with empty output, cancel and re-dispatch once in
     the same turn (`rec:2026-08-12#8`) - never let a second window sit unwatched. Measured cost of
     skipping this: session `defef5a9` (2026-08-19/20, unattended overnight) dispatched this exact
     tamper-judge pass unwatched and lost 19,478s - 65% of an 8.5h session - to a dead job whose
     result never arrived; the eventual watched re-dispatch that unstuck it took under 2 minutes.
   - Exit 0 → continue. **Only exit 0 continues; ANY non-zero (2 usage, 3 tamper, or anything else) is a
     fail-closed STOP** - never "exit != 3 means proceed".
   This runs BEFORE the concurrent dispatch below and must clear first: a tamper hit means no codex
   verdict can be accepted at all, so there is nothing to overlap it with.
4. **Full suite AND the codex gate - dispatch BOTH in the same turn, both `run_in_background: true`,
   then wait.** They read the same committed tree, so neither blocks the other; the only ordering
   constraint is that both come after the commit at Step 2. Waiting for the suite before dispatching
   the gate is the largest recoverable serialization in this loop - measured 2026-08-09,
   `parallelism=1.00x` across 24 background jobs in one session, a 4-minute dead gap between the suite
   finishing and the codex dispatch going out, and 45% of that session's wall clock blocked.
   - **Suite** = your full-suite command (`$TEST_COMMAND` from `.dev-loop.conf`).
   - **codex gate on the branch diff** - drive it through review-gate's **diff-stage**
     mechanics (`/codex:adversarial-review --base <base>`), arming the wedge watchdog in the same breath.
     **Round 2+: resume the SAME codex agent** (`codex:rescue --resume`) so it re-judges with warm
     context, feeding it the prior verdict; do not cold-dispatch codex each round.
   - **A RED suite still rejects the round, exactly as before - this is a concurrency change, not a
     policy change.** Discard this round's codex verdict (a SHIP on a red tree is never a pass), find
     the **root cause** and fix it, re-run affected + full suite until green, then start the next
     round. Making a real test pass is fine; **deleting or mocking it to go green is what Step 3
     catches** - never do it.
   - **The price of overlapping is one wasted codex job per RED round.** That is the intended trade:
     codex spend is plan quota, the suite is ~3.5 min of wall clock on every round, and green rounds
     are the majority. If a repo's suite is habitually red on entry to the loop, fix that rather than
     re-serializing these two.
5. **Verdict** (both results in hand).
   - **codex SHIP** + tamper-clean + suite green → **DONE.**
     Log `gateloop-pass` to verify.log. Report the green `session/<slug>` branch and **STOP before
     integration** (Stage 1 hands off to the lander / `/ship`, it does not merge).
   - **BLOCK** (or `SHIP-WITH-CHANGES`, which is not-a-pass) →
     **severity-gate the re-gate** so a nitpick loop can't blow the round budget. Classify this round's
     findings: a repo may wire `$REGATE_DECISION_CMD` in `.dev-loop.conf` to decide from the diff's risk
     class + each finding's severity (it prints `regate` or `batch`); **if unset, default to a full
     re-gate** - today's behavior, fail-safe.
     - **full re-gate** (any blocking-severity finding - bug/security/correctness/race/fail-open/
       provenance/money - or a risk-sensitive diff) → fix the **root cause of the whole defect class**
       (enumerate siblings, add one class regression), re-run the affected suite, and start the next
       round. If a fix must touch a test/config/deps file, that is a Step-3 tamper stop, not an
       autonomous fix.
     - **batch** (all findings are style/design nits on a low-risk diff) → apply **all** the nit fixes
       in ONE commit, then run ONE **confirmation** gate (codex on the batched commit).
       Converged if it SHIPs or returns only nits; a **new blocking finding sends it back to full
       re-gate** - batching is never an escape hatch.
     - **Guardrails:** a BLOCK is never merged over (codex must still SHIP the final
       commit); a risk-sensitive diff never batches; batch+confirm counts toward the 3-round cap.

## Cap-out (round 3 still BLOCK)

- **Never claim green.** Log `gateloop-capout` to verify.log with the head and the standing findings,
  then STOP and hand off to a human. Three self-driven rounds without a codex SHIP means
  the loop cannot resolve it alone - that is a stop condition, not a "good enough."
- Same for a codex/agy **wedge** the watchdog can't recover: log it, STOP, never infer a pass from a
  dead worker.

## verify.log rows

Append one tab-separated row per terminal event to `$VLOG` (resolved at Step 0 - the PRIMARY
checkout's log, never the worktree's):

```
printf '%s\t%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" <event> "$(git rev-parse --short HEAD)" "<detail>" >> "$VLOG"
```

Events: `gateloop-pass` (detail = base + rounds used), `gateloop-block` (per BLOCK round, detail =
one-line findings), `gateloop-capout`, `gateloop-tamper` (detail = offending paths).

`gateloop-pass` is the one row another skill CONSUMES: `land`'s Step 1b skips its codex round only
when `OVERLAP=0` **and** a `gateloop-pass` row exists whose head equals
`git rev-parse --short <candidate>`. So it must be written to `$VLOG` at the FINAL candidate tip - a
row at an earlier head, or in the worktree's log, silently costs a full extra codex round at land.

## Ceiling

- **In-band:** the guard and gate run in the same trust domain they protect; a determined agent could
  disable them (caught next turn). Not solved here - accepted for v1.
- **agy is advisory and working-tree-only** - it never sees the committed branch, so a clean-tree
  round marks it N/A. **codex is the required blocking gate** on the branch diff.
- The tamper guard's correctness lives in `${CLAUDE_PLUGIN_ROOT}/skills/gate-loop/scripts/tamper-check.sh --selftest`; the loop orchestration
  is validated by the Stage-1 live dry-run in the design's Verification section (deliberately-failing
  test → converges ≤3 or caps out; a `tests/` edit trips the tamper stop).
