---
name: gate-loop
description: The autonomous in-session verify→fix→re-gate loop that turns a worktree's work into a green, codex+deepseek-gated `session/<slug>` branch - then STOPS before integration (the lander merges, not this skill). Runs the full suite, agy-advises the working tree, commits, runs a coded test-tamper guard, and gates codex AND deepseek (both required blockers) on the branch diff, looping on BLOCK up to 3 rounds by resuming the same warm codex agent. Use when the user says "gate-loop", "/gate-loop", "loop to green", "run the gate loop", or wants a worktree driven to a gated-green branch autonomously before landing. Do NOT use for a one-shot human-gated review (that is /ship or review-gate) - this is the capped, self-driving multi-round loop. Never claims green on a cap-out, a tamper hit, or a wedge.
---

> **Configure first (opinionated pipeline).** This skill assumes a repo configured via `.dev-loop.conf` at its root (copy `$CLAUDE_PLUGIN_ROOT/dev-loop.conf.example` and edit). Source it so `$TEST_COMMAND`, `$BASE_BRANCH`, and the vendored-lander wiring are set. Requires the `dev-loop-core` plugin (the `review-gate` skill it drives). See the repo README.

# gate-loop

The Stage-1 autonomous loop of the gated-land pipeline (this is an opinionated solo-dev pipeline; see the repo README). It is a
**skill, not a script** on purpose: the session Claude IS the fixer, so it orchestrates the loop the
way `review-gate`/`ship` do, driving the already-built pieces rather than re-implementing them.

**What it produces:** a green `session/<slug>` branch that codex and deepseek have both SHIP'd, with
the test-tamper guard clean. **Where it stops:** before integration. Merging that branch is the lander's job
(`${CLAUDE_PLUGIN_ROOT}/engine/lander.sh`, Stage 2) or a human via `/ship` - this skill never merges, pushes, resets the
target, or `ExitWorktree`s.

**Roles (fixed, same as review-gate):** codex AND deepseek = ship/no-ship gates - a BLOCK from
*either* means loop or stop. deepseek is a **required** second blocker (`DEEPSEEK_API_KEY` set + plugin
ready - the key lives in the **main-checkout `.env`**, so load it from the repo root, not the
worktree's absent `./.env`; setup: the README). If deepseek is genuinely
not configured, review-gate reports an honest
skip and the loop treats that skip as **not-a-pass**: STOP for a human to fix the config (fail closed),
never a silent codex-only green. agy = advisory only. The **test-tamper guard is a hard
brake** that overrides *any* SHIP (codex or deepseek): no self-approval on a diff that touched the
verification substrate, period.

## When this fires

Inside a worktree on a `session/<slug>` branch, work is believed done and the user wants it driven to
a gated-green branch autonomously: "gate-loop", "/gate-loop", "loop to green", or before handing off
to the lander. Not for a one-shot review - that is `/ship` (human-gated, one codex pass, no loop).

## Reused pieces (do NOT re-hand-roll)

- your full-suite command (`$TEST_COMMAND` from `.dev-loop.conf`) - the full suite (socket-port-0 isolated; safe to run concurrently).
- **review-gate skill** owns codex/deepseek/agy dispatch, the wedge watchdog, and the per-worktree
  lock. Drive codex/deepseek/agy **through the `review-gate` skill** (from the `dev-loop-core` plugin, a hard dependency), do not reinvent the mechanics.
- `${CLAUDE_PLUGIN_ROOT}/skills/gate-loop/scripts/tamper-check.sh <base> [HEAD]` - the coded test-tamper guard
  (exit 3 = tamper). `--selftest`ed.
- `.claude/state/verify.log` - the append-only audit trail (tab-separated `ts<TAB>event<TAB>head<TAB>detail`).

## Step 0 - preflight

- Confirm you are **in a worktree** on a `session/<slug>` branch (`git rev-parse --show-toplevel`,
  `git branch --show-current`). If on the primary checkout or `main`, STOP - this loop must not run on
  the integration target.
- Resolve `<base>` (usually `main`; ask only if genuinely ambiguous) and confirm
  `git diff <base>...HEAD` (plus any uncommitted work) is non-empty. Empty → STOP, "nothing to gate".

## The loop (max 3 gate rounds)

At loop entry, append one `gateloop-start` row to the audit log so the cap is counted from data, not
memory: `printf '%s\tgateloop-start\t%s\t-\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(git rev-parse --short HEAD)" >> .claude/state/verify.log`.

Repeat the round below. A **round** = one gate attempt (codex + deepseek). **Cap from data, not memory:**
at the top of each round, if `${CLAUDE_PLUGIN_ROOT}/skills/gate-loop/scripts/round-count.sh .claude/state/verify.log`
returns `>=3`, take the Cap-out path (do NOT start a 4th round). The count is positional (rows after the
last `gateloop-start`), so it survives context compaction and clock drift.

Drive review-gate's deepseek dispatch with `RG_REQUIRE_DEEPSEEK=1` exported — for this autonomous loop a
not-configured deepseek is a fail-closed human stop, never a codex-only pass.

1. **Full suite.** your full-suite command (`$TEST_COMMAND` from `.dev-loop.conf`).
   - Red → this is a fix pass: find the **root cause** and fix it, re-run affected + full suite until
     green. Making a real test pass is fine; **deleting or mocking it to go green is what Step 4
     catches** - never do it.
2. **agy advisory on the UNCOMMITTED working tree, BEFORE commit.** agy cannot review a committed
   branch, so it must see the working-tree/staged diff here or be marked N/A (clean tree → N/A, say so;
   don't present an empty agy result as a pass). Advisory only - surface findings, weigh them, do not
   block on agy alone.
3. **Commit** the work on `session/<slug>` (per-task commits as usual).
4. **Test-tamper guard (hard brake, every round over the whole branch diff):**
   `${CLAUDE_PLUGIN_ROOT}/skills/gate-loop/scripts/tamper-check.sh <base>`.
   - Exit 3 → **STOP. Do not self-approve, even if the suite is green and codex would SHIP.** Log
     `gateloop-tamper` to verify.log and hand the branch diff to a human, or dispatch a **dedicated
     codex pass whose sole job is to judge that test/config/deps diff** - the loop cannot clear it.
   - Exit 0 → continue. **Only exit 0 continues; ANY non-zero (2 usage, 3 tamper, or anything else) is a
     fail-closed STOP** - never "exit != 3 means proceed".
5. **codex + deepseek gate on the branch diff.** Drive BOTH through review-gate's **diff-stage**
   mechanics (`/codex:adversarial-review --base <base>` plus review-gate's deepseek dispatch on the
   same `<base>...HEAD` range), arming the wedge watchdog in the same breath. **Round 2+: resume the
   SAME codex agent** (`codex:rescue --resume`) so it re-judges with warm context, feeding it the prior
   verdict; do not cold-dispatch codex each round. deepseek is **stateless** - it re-runs fresh each
   round (there is no warm resume). A genuine not-configured deepseek is **not-a-pass** - fail closed
   and STOP for a human, never a silent codex-only green.
   - **codex SHIP AND deepseek SHIP** + tamper-clean + suite green → **DONE.**
     Log `gateloop-pass` to verify.log. Report the green `session/<slug>` branch and **STOP before
     integration** (Stage 1 hands off to the lander / `/ship`, it does not merge).
   - **BLOCK from either blocker** (or `SHIP-WITH-CHANGES` from either, which is not-a-pass) →
     **severity-gate the re-gate** so a nitpick loop can't blow the round budget. Classify this round's
     findings: a repo may wire `$REGATE_DECISION_CMD` in `.dev-loop.conf` to decide from the diff's risk
     class + each finding's severity (it prints `regate` or `batch`); **if unset, default to a full
     re-gate** - today's behavior, fail-safe.
     - **full re-gate** (any blocking-severity finding - bug/security/correctness/race/fail-open/
       provenance/money - or a risk-sensitive diff) → fix the **root cause of the whole defect class**
       (enumerate siblings, add one class regression), re-run the affected suite, and start the next
       round. If a fix must touch a test/config/deps file, that is a Step-4 tamper stop, not an
       autonomous fix.
     - **batch** (all findings are style/design nits on a low-risk diff) → apply **all** the nit fixes
       in ONE commit, then run ONE **confirmation** gate (codex + deepseek on the batched commit).
       Converged if it SHIPs or returns only nits; a **new blocking finding sends it back to full
       re-gate** - batching is never an escape hatch.
     - **Guardrails:** a BLOCK is never merged over (codex AND deepseek must still SHIP the final
       commit); a risk-sensitive diff never batches; batch+confirm counts toward the 3-round cap.
   - **deepseek `ERROR`/`OVERSIZE`** (missing key at call time, API failure, diff too large) → a human
     stop, never a loop and never a pass: log `gateloop-capout` and hand off.

## Cap-out (round 3 still BLOCK)

- **Never claim green.** Log `gateloop-capout` to verify.log with the head and the standing findings,
  then STOP and hand off to a human. Three self-driven rounds without a codex-AND-deepseek SHIP means
  the loop cannot resolve it alone - that is a stop condition, not a "good enough."
- Same for a codex/deepseek/agy **wedge** the watchdog can't recover, or a deepseek `ERROR`/`OVERSIZE`
  no fix clears (see review-gate's Resilience): log it, STOP, never infer a pass from a dead worker or
  a fail-closed deepseek verdict.

## verify.log rows

Append one tab-separated row per terminal event to your audit log (`.claude/state/verify.log`):

```
printf '%s\t%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" <event> "$(git rev-parse --short HEAD)" "<detail>" >> .claude/state/verify.log
```

Events: `gateloop-pass` (detail = base + rounds used), `gateloop-block` (per BLOCK round, detail =
one-line findings), `gateloop-capout`, `gateloop-tamper` (detail = offending paths).

## Ceiling

- **In-band:** the guard and gate run in the same trust domain they protect; a determined agent could
  disable them (caught next turn). Not solved here - accepted for v1.
- **agy is advisory and working-tree-only** - it never sees the committed branch, so a clean-tree
  round marks it N/A. **codex and deepseek are the required blocking gates** on the branch
  diff; a genuine not-configured deepseek is a human stop (fail closed - it is required infra), never
  a silent codex-only pass.
- The tamper guard's correctness lives in `${CLAUDE_PLUGIN_ROOT}/skills/gate-loop/scripts/tamper-check.sh --selftest`; the loop orchestration
  is validated by the Stage-1 live dry-run in the design's Verification section (deliberately-failing
  test → converges ≤3 or caps out; a `tests/` edit trips the tamper stop).
