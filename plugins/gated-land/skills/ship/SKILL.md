---
name: ship
description: Tier 2 (v1, non-autonomous) ship gate for this repo - run the full test suite, then run the codex+deepseek (blocking) / agy (advisory) diff-stage review gate once, then stop for a human merge decision. Does not auto-fix, auto-re-gate, or auto-merge (that is v2, not yet built). Use when the user says "ship it", "ship this", invokes /ship, or asks to run the ship gate before merging a worktree's work to main. Isolates the expensive LLM review to this one explicit boundary - never run it on every turn.
---

> **Configure first (opinionated pipeline).** This skill assumes a repo configured via `.dev-loop.conf` at its root (copy `$CLAUDE_PLUGIN_ROOT/dev-loop.conf.example` and edit). Source it so `$TEST_COMMAND`, `$BASE_BRANCH`, and the vendored-lander wiring are set. Requires the `dev-loop-core` plugin (the `review-gate` skill it drives). See the repo README.

Tier 2 of the verification harness (an opinionated solo-dev ship gate; see the repo README). Tier 1 (`verify.sh`) is the cheap, no-LLM, every-turn gate; this skill is the expensive, explicit, once-per-shippable-unit gate. **v1 only**: it stops for a human decision. It never commits, merges, pushes, or exits a worktree - the autonomous fix→re-gate loop is the `gate-loop` skill (Stage 1: drives a worktree to a gated-green branch but still stops before integration), and merging is the lander (`${CLAUDE_PLUGIN_ROOT}/engine/lander.sh`, Stage 2); both are gated on this v1 proving reliable first.

## When this fires

Work in a worktree is believed done (Tier 1 has been green for a while) and the user wants to ship it: "ship it", "ship this", `/ship`, or "is this ready to merge".

## Step 0 - clean-worktree preflight

Step 1 (tests) and Step 2 (review) must certify the exact same state - Step 1 runs against whatever is on disk, Step 2 only ever reviews a *committed* branch diff, and those two silently diverge if anything is left uncommitted. Before running the suite, check `git status --porcelain --untracked-files=all` (the `graphify-out/` case is handled separately in Step 2, so ignore paths under it here). If anything else is dirty or untracked, **STOP** and ask the human to commit, stash, or discard it first - do not run the suite or the review against a tree that the review step won't actually see.

## Step 1 - full suite

Tier 1's Stop hook only ever runs the seeded `tests/smoke.txt` subset. Before any review spend, run the full suite:

```bash
"$TEST_COMMAND"   # from your repo-root .dev-loop.conf
```

- **Any failure -> STOP here.** Report the failing files (the script already tails each failure's last output line) and do not proceed to Step 2. Do not spend review tokens on a red tree.
- All green -> continue.

## Step 2 - diff-stage review gate (run ONCE)

Follow the `review-gate` skill's **diff stage** dispatch mechanics exactly (it is not duplicated here - read it fresh, do not work from memory of it), but only its dispatch/wait mechanics, not its fix-and-re-gate loop - that loop is the `gate-loop` skill, not this skill. **One exception**: do NOT follow review-gate's "`git checkout -- graphify-out/` if dirty" step in this repo. That step assumes graphify churn is disposable tool noise, but this repo's own `CLAUDE.md` treats it as legitimate tracked work (`graphify update .` after code changes is a documented, expected step) - discarding it can destroy real, uncommitted work. If `graphify-out/` is dirty when this skill runs, STOP before dispatching either review and ask the human whether to regenerate/stage/stash/discard it first. Do not run any destructive git command on it yourself.

- **Scope the review to the branch, not the ambient working-tree diff.** `/ship` fires at a merge boundary where this project's convention is per-task commits, so the working tree is typically already clean - a review that just looks at "the worktree diff" silently reviews nothing in that normal case. Resolve the base ref (`main`, or ask the human if it is ambiguous) and confirm `git diff <base>...HEAD` is non-empty before dispatching. If it is empty, **STOP** and tell the human there is nothing to ship rather than reporting a false-clean gate.
  - **Codex** (blocking): launch `/codex:adversarial-review --base <base>` (it supports scoping to a branch diff against a base ref directly).
  - **deepseek** (second blocking gate): dispatch it through **review-gate's diff-stage recipe** with `--base <base>` - review-gate owns the companion path (and its `2>/dev/null` + `[ -n "$DS" ]` guards), the `$VF` verdict file, and the main-checkout `.env` load (a worktree has no `./.env`); do NOT duplicate them here. Verdict = the recipe's last `VERDICT:` line. A genuine not-ready / `ERROR` / `OVERSIZE` is a **human stop** (fail closed), never a silent skip - deepseek is a required blocker here (setup: the README).
  - **agy** (advisory): `/agy:adversarial-review` has no branch/base option - it only ever reviews the current uncommitted `git diff`/staged diff, so at ship time (clean tree) it will report "nothing to review." That is a known tooling gap, not a real advisory pass - do not treat it as agy having reviewed and found nothing. Report the gap plainly in Step 3 rather than presenting agy's empty result as a clean verdict.
- Arm the stuck-worker watchdog in the same breath as dispatch (see review-gate's "Stuck workers" section) - never wait passively.
- Collect whatever verdict comes back - clean or with findings. **Do not fix anything, do not resume the thread, do not re-run the review in this step or this skill invocation.** Reacting to findings is Step 3's job, and Step 3 hands that decision to the human, not to this skill.

## Step 3 - stop for a human decision (always, whether clean or not)

Whether codex came back clean or with findings, this skill stops here every time - a red review is not a reason to keep working, it is the reason to stop and hand off:

- Report: full-suite result (Step 1), **codex AND deepseek verdicts verbatim** (findings included, if any), and either agy's findings and whether the blockers corroborated them, or - if agy hit the empty-diff tooling gap from Step 2 - say so plainly instead of implying agy reviewed anything.
- **Stop.** Do **not**, under any circumstance, in this skill:
  - fix any finding codex or agy raised
  - resume or re-dispatch either review to check a fix
  - commit on the human's behalf beyond what TDD/task work already committed during implementation
  - merge the worktree branch into `main`
  - push
  - call `ExitWorktree`
  - loop back to re-run Step 2 automatically
- If **both codex and deepseek** came back SHIP: ask the human whether to merge.
- If **either** came back with findings / non-SHIP (or deepseek hit a fail-closed `ERROR`/`OVERSIZE`): report both verdicts and ask the human how to proceed (e.g. fix the findings yourself and invoke `/ship` again once done, or explicitly override). The human decides the next step - this skill does not.

  All of the above automation lives elsewhere: the fix→re-gate loop is the `gate-loop` skill and integration is the lander (`${CLAUDE_PLUGIN_ROOT}/engine/lander.sh`, Stage 2, not yet built). This v1 `/ship` stays the explicit human-gated path and is deliberately not wired into either.

## Ceiling

This skill has no automated test (it is a pure orchestration playbook over two already-tested pieces: your `$TEST_COMMAND` and the `review-gate` skill). Its correctness is validated by a live dry-run instead - see the plan's Task 2.

**Resolved (was assumed to be a ceiling, turned out not to be):** an earlier draft of this note assumed `Skill(skill: "ship")` could not resolve until a fresh session, by analogy with the settings.json hooks hot-reload constraint. That assumption was wrong for skills - live-verified in the same session that merged this file to `main`: immediately after the merge, `Skill(skill: "ship")` resolved and loaded this file with no session restart. Project skills are evidently not scanned only at session start the way hooks are; do not assume a new skill needs a fresh session to become invokable by name.
