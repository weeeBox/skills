---
name: land
description: Stage-2 local merge queue - integrate a green session/<slug> branch into the target (main) through the serialized lander. Runs scripts/lander.sh prepare (lock, throwaway integration worktree, merge --no-ff, full suite, static risk-classify), gates codex AND deepseek on the exact integration commit, then commits only on codex-SHIP + deepseek-SHIP + risk=LOW (everything else stops for a human), with CAS fail-closed if the target moved. Use when the user says "land", "/land", "land this branch", or wants a gated-green session branch integrated. Do NOT use to produce the green branch (that is the gate-loop skill) or for a one-shot human review (that is /ship). Never auto-merges risk=HIGH, never auto-pushes, never auto-resolves a conflict.
---

> **Configure first (opinionated pipeline).** This skill assumes a repo configured via `.dev-loop.conf` at its root (copy `$CLAUDE_PLUGIN_ROOT/dev-loop.conf.example` and edit). Source it so `$TEST_COMMAND`, `$BASE_BRANCH`, and the vendored-lander wiring are set. Requires the `dev-loop-core` plugin (the `review-gate` skill it drives). See the repo README.

# land

Stage 2 of the gated-land pipeline (this is an opinionated solo-dev pipeline; see the repo README). It drives
`${CLAUDE_PLUGIN_ROOT}/engine/lander.sh` - the ONE serialized merge queue - and wraps the codex+deepseek re-gate that a bash
script can't drive itself. Input: a green `session/<slug>` branch (produced by the `gate-loop` skill). Output:
that branch integrated into `main`, or a clean STOP with the target untouched.

**Roles:** codex AND deepseek = ship/no-ship gates on the **integration commit** (the merged tree, not
the branch in isolation - that is how two-green-alone-broken-together is caught); a non-SHIP from
*either* aborts the land. deepseek is required infra (`DEEPSEEK_API_KEY` lives in the **main-checkout
`.env`**, not the worktree; setup: the README); a genuine
not-configured / `ERROR` / `OVERSIZE` is a human stop, never a silent pass. agy is **N/A here** - it cannot review a committed diff (the design's known tooling gap);
do not present its empty result as a pass.

**Boundaries (v1):** never auto-merges `risk=HIGH`, never auto-pushes (opt-in `LANDER_PUSH=1`), never
auto-resolves a merge conflict, never runs on every turn. This is the expensive land boundary.

## Step 0 - preflight

- Identify the **candidate** `session/<slug>` branch (the current branch if you are in its worktree,
  else ask) and the **target** (default `main`; ask only if ambiguous).
- The candidate should already be `gate-loop`-green. This skill re-verifies from scratch anyway (the
  lander re-runs the suite on the *merged* commit), so a stale green is caught, not trusted.

## Step 1 - prepare (mechanical, in a throwaway worktree)

```bash
"$CLAUDE_PLUGIN_ROOT/engine/lander.sh" prepare <candidate> <target>
```

The script (off a base captured at that instant - `prepare` is deliberately **lock-free**; the lock is
only taken in `commit`, and the CAS there, expected-old == this base, is what makes the release-between-
phases safe): spins a throwaway integration worktree, `git merge --no-ff` the candidate there, runs the
**full suite** on the merged commit, and statically risk-classifies the merged diff via
the vendored `${CLAUDE_PLUGIN_ROOT}/engine/risk_classify.py` (allowlist + denylist + size caps, from `git diff --numstat`).

- **Non-zero exit → STOP, target untouched.** Surface the reason verbatim: `3`=merge conflict (never
  auto-resolve - hand to a human), `4`=suite red on the integration commit (the semantic-conflict
  catch), `5`=dirty/stale, `2`=usage. Do not proceed.
- **Exit 0 →** capture the printed `BASE`, `INTEGRATION`, `WORKTREE`, `RISK`. The throwaway worktree is
  left in place holding the merged commit, for the gate and the commit.

## Step 2 - codex + deepseek gate on the integration commit

Drive codex through the `review-gate` skill's **diff-stage** mechanics (dispatch + wedge watchdog +
lock - do not hand-roll them), but scope it to the **integration commit**, not the candidate branch in
isolation: run the review **from the throwaway `$WORKTREE`** (its detached HEAD *is* the merged commit)
with `--base <BASE>`, so codex sees `BASE...INTEGRATION` = the candidate's work applied onto the
*current* target. That combined diff is what surfaces a semantic conflict a per-branch review misses.

- **deepseek** (second blocker): dispatch it through **review-gate's diff-stage recipe** - which owns
  the companion path (with its `2>/dev/null` + `[ -n "$DS" ]` guards), the `$VF` verdict file, and the
  main-checkout `.env` load (a worktree has no `./.env`); do NOT re-hand-roll them. Run it on the
  **integration commit**, exactly like codex: from a subshell `cd "$WORKTREE"` (first assert
  `git -C "$WORKTREE" rev-parse HEAD` == `$INTEGRATION`) with `--base <BASE>`, so deepseek reviews
  `BASE...INTEGRATION` = the merged range. Verdict = the recipe's last `VERDICT:` line (SHIP /
  SHIP-WITH-CHANGES / BLOCK / OVERSIZE / ERROR). A genuine not-ready / `ERROR` / `OVERSIZE` is a
  **human stop** (fail closed), never a skip - deepseek is required at the land boundary.
- agy: **N/A** (committed diff, no base option) - say so honestly, do not imply agy reviewed.
- Collect codex's AND deepseek's verdicts (SHIP / SHIP-WITH-CHANGES / BLOCK). Do not fix findings here.

## Step 3 - decide (the gate + risk table)

- **Before an auto-land `commit`, write the integ-keyed verdict record** so `commit` can re-verify against
  the exact integration commit. It derives the path from `<INTEGRATION>` (a stale record for a different
  SHA is ignored). `$CODEX_RESULT_JSON` = the codex job's result JSON you already read; `$DEEPSEEK_VF` =
  review-gate's `$VF`:
  ```bash
  printf '%s' "$INTEGRATION" | grep -qE '^[0-9a-f]{40,64}$' || { echo "INTEGRATION is not a git OID — abort"; exit 1; }
  case "$CODEX_RESULT_JSON$DEEPSEEK_VF" in *"
  "*) echo "reviewer artifact path contains a newline — abort"; exit 1 ;; esac
  RECDIR="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")/.claude/state/land-verdicts"; mkdir -p "$RECDIR"
  _t="$(mktemp "$RECDIR/.${INTEGRATION}.rec.XXXXXX")" || { echo "mktemp failed"; exit 1; }
  printf 'CODEX_RESULT=%s\nDEEPSEEK_VERDICT=%s\n' "$CODEX_RESULT_JSON" "$DEEPSEEK_VF" > "$_t" && mv -f "$_t" "$RECDIR/$INTEGRATION.rec"
  ```
  `commit` re-runs the risk classifier (refuses non-LOW, exit 7) and re-reads those artifacts, refusing
  (exit 8) unless each artifact's last anchored `VERDICT:` line is exactly `SHIP`. `LANDER_HUMAN_OVERRIDE=1`
  is the only bypass of the risk/verdict checks (never of integration validation) - an explicit,
  `land-override`-logged, in-band escape hatch.

| codex | deepseek | risk | action |
|-------|----------|------|--------|
| SHIP  | SHIP     | LOW  | write the record (above), then **auto-land:** `${CLAUDE_PLUGIN_ROOT}/engine/lander.sh commit <candidate> <target> <BASE> <INTEGRATION> <WORKTREE>` |
| SHIP  | SHIP     | HIGH | **STOP for a human** even though green - present the diff + risk; a human runs the same `commit` with `LANDER_HUMAN_OVERRIDE=1` |
| SHIP-WITH-CHANGES | any | any | **not-a-pass:** `abort`, surface the requested changes, STOP for a human (never auto-land a conditional SHIP) |
| any   | SHIP-WITH-CHANGES | any | **not-a-pass:** `abort`, surface the changes, STOP for a human |
| BLOCK | any  | any | **abort:** `${CLAUDE_PLUGIN_ROOT}/engine/lander.sh abort <WORKTREE> "codex BLOCK"`, surface findings, STOP |
| any   | BLOCK / OVERSIZE / ERROR | any | **abort:** `${CLAUDE_PLUGIN_ROOT}/engine/lander.sh abort <WORKTREE> "deepseek <verdict>"`, surface, STOP |

- At Stage 2 the classifier is deliberately conservative - an unclassified diff fails safe to `HIGH`,
  so **almost everything routes to the human**. That is the intended posture; the `LOW` auto-path
  exists but rarely fires until Stage 4 widens it on `verify.log` evidence. Do not "help" it fire by
  hand-classifying - the whole point is that risk is static and code-path-based, not agent-judged.
- **`commit` is CAS-protected:** it re-checks that the target still equals `BASE` and fails closed
  (exit 5, target untouched) if another land moved it since `prepare` - report that as "stale base,
  re-run", not a failure of the work.

## Step 4 - report

Every terminal path already appends to `.claude/state/verify.log` (`land-ok`/`land-conflict`/
`land-redsuite`/`land-stale`/`land-abort`). Report to the human: the prepare outcome, codex's AND
deepseek's verdicts verbatim, the risk class, and what happened (landed / stopped-for-human / aborted). Never claim a land
that the CAS didn't confirm.

## Ceiling

- **Semantic-conflict residual:** serialize + post-merge suite + codex-on-integration mitigate it, not
  eliminate (suite coverage gaps, flakes, skipped live tests). Stated, not hidden.
- **agy blind at the land boundary** - codex and deepseek gate the merged commit (agy cannot review a
  committed diff).
- **Lock is mkdir-atomic** (no `flock` on macOS/bash 3.2), reclaimed after `LANDER_LOCK_STALE`s; the
  CAS on commit is the real cross-phase integrity guarantee, not the lock.
- Mechanics are covered by `${CLAUDE_PLUGIN_ROOT}/engine/lander.sh --selftest` (merge/conflict/red-suite/stale-base/commit).
  This orchestration skill itself is validated by a live dry-run (design Stage-2 Verification).
- **This code is pending its codex+agy gate** (design: "Gate the lander CODE with codex+agy here").
  Until that passes, run `/land` with a human watching every step.
