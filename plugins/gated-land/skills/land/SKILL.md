---
name: land
description: Stage-2 local merge queue - integrate a green session/<slug> branch into the target (main) through the serialized lander. Runs scripts/lander.sh prepare (lock, throwaway integration worktree, merge --no-ff, full suite, static risk-classify), gates codex on the exact integration commit, then commits only on codex-SHIP + risk=LOW (everything else stops for a human), with CAS fail-closed if the target moved. Use when the user says "land", "/land", "land this branch", or wants a gated-green session branch integrated. Do NOT use to produce the green branch (that is the gate-loop skill) or for a one-shot human review (that is /ship). Never auto-merges risk=HIGH, never auto-pushes, never auto-resolves a conflict.
---

> **Configure first (opinionated pipeline).** This skill assumes a repo configured via `.dev-loop.conf` at its root (copy `$CLAUDE_PLUGIN_ROOT/dev-loop.conf.example` and edit). Source it so `$TEST_COMMAND`, `$BASE_BRANCH`, and the vendored-lander wiring are set. Requires the `dev-loop-core` plugin (the `review-gate` skill it drives). See the repo README.

# land

Stage 2 of the gated-land pipeline (this is an opinionated solo-dev pipeline; see the repo README). It drives
`${CLAUDE_PLUGIN_ROOT}/engine/lander.sh` - the ONE serialized merge queue - and wraps the codex re-gate that a bash
script can't drive itself. Input: a green `session/<slug>` branch (produced by the `gate-loop` skill). Output:
that branch integrated into `main`, or a clean STOP with the target untouched.

**Roles:** codex = the ship/no-ship gate on the **integration commit** (the merged tree, not
the branch in isolation - that is how two-green-alone-broken-together is caught); a non-SHIP
aborts the land. agy is **N/A here** - it cannot review a committed diff (the design's known tooling gap);
do not present its empty result as a pass.

**Boundaries (v1):** never auto-merges `risk=HIGH`, never auto-pushes (opt-in `LANDER_PUSH=1`), never
auto-resolves a merge conflict, never runs on every turn. This is the expensive land boundary.

## Step 0 - preflight

- Identify the **candidate** `session/<slug>` branch (the current branch if you are in its worktree,
  else ask) and the **target** (default `main`; ask only if ambiguous).
- The candidate should already be `gate-loop`-green. This skill re-verifies from scratch anyway (the
  lander re-runs the suite on the *merged* commit), so a stale green is caught, not trusted.
- **REFUSE to land from inside the candidate's own worktree.** Check it before anything else:

  ```bash
  git rev-parse --show-toplevel      # must NOT be the candidate branch's worktree
  git worktree list                  # shows which worktree holds <candidate>
  ```

  If this session's toplevel is the worktree checked out on the candidate branch, stop and say:
  *"land from a different worktree - a successful land deletes this one."* A successful land removes
  the candidate worktree, and a session pinned to it wedges instantly and totally: every later Bash
  call is refused as resolving to the shared checkout, **including the `git push` that finishes the
  land**. Recovery is `ExitWorktree` with `action: "keep"` - one call, no session restart, and
  neither `cd`, an absolute path, nor `git -C` gets out of it. On 2026-08-10 a land succeeded and
  then burned four dead calls rediscovering this before handing the unpushed push to the user.

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
- **Exit 0 →** capture the printed `BASE`, `INTEGRATION`, `WORKTREE`, `RISK`, `SUITE`, `OVERLAP`,
  `OVERLAP_FILES`. The throwaway worktree is left in place holding the merged commit, for the gate and
  the commit.
- `SUITE` is `RAN` or `SKIPPED_DOCS_ONLY`. The skip fires only when **every** path in the
  integration diff ends `.md`/`.txt`/`.rst` - one code file, one unlisted extension, or an empty
  diff and the suite runs. State the value in the Step 4 report and in the reviewer prompt, so a
  green land on prose is never mistaken for a green land on a tested diff.

## Step 1b - is there anything new to review? (the merge-interaction check)

`prepare` prints `OVERLAP` = the count of files the candidate touched **that the target also moved
since the fork**, and `OVERLAP_FILES` = those paths. This is the only surface a land-stage review can
see that the **branch-stage** gate did not: everything else in `BASE...INTEGRATION` is either the
candidate diff the branch gate already SHIP'd, or target commits that already landed through this same
gate.

**Skip Step 2's codex round only when ALL of these hold** (any doubt → run it):

1. `OVERLAP` is exactly `0` - not `unknown`. **`unknown` means the engine could not compute the
   surface (bad ref, unrelated histories) and is a GATE signal, never a pass.**
2. The branch gate SHIP'd **this exact candidate tip**: a `gateloop-pass` row in
   `.claude/state/verify.log` whose head equals `git rev-parse --short <candidate>`. No row, or a row
   at a different head → the candidate was never gated → **gate it here**.
3. The suite was green on the integration commit (guaranteed - `prepare` exits 4 otherwise).

```bash
CTIP="$(git rev-parse --short "$CANDIDATE")"
if [ "$OVERLAP" = 0 ] && awk -F'\t' -v h="$CTIP" '$2=="gateloop-pass" && $3==h {f=1} END{exit !f}' \
     "$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")/.claude/state/verify.log"
then echo "SKIP-GATE"; else echo "GATE"; fi
```

- **SKIP-GATE →** log it, then go to Step 3 treating codex as **SHIP**, writing the verdict record with
  the **branch-stage** codex artifact (it reviewed the same code, so `commit`'s `VERDICT: SHIP`
  re-verification still has a real reviewer artifact behind it). Say plainly in your report that the
  land-stage round was skipped and why - never present it as a fresh review.
  ```bash
  VLOG="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")/.claude/state/verify.log"
  printf '%s\tland-gate-skipped\t%s\tno merge interaction; branch SHIP at %s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${INTEGRATION:0:8}" "$CTIP" >> "$VLOG"
  ```
- **GATE →** Step 2 as normal. When `OVERLAP` is non-zero, **name `OVERLAP_FILES` in the codex prompt as the focus**: those files are where the two lines of work can interact, and that interaction is the question this gate exists to answer.

Why this is safe, and where it is not: the branch gate reviewed the candidate, this gate exists for the
*combination*, and `OVERLAP=0` means no file carries both. It does **not** prove semantic independence
across files (branch adds a caller, target changes the callee's contract in a file the branch never
touched) - the **full suite on the merged commit still runs either way** and is what catches that.
`OVERLAP` only decides whether a *second LLM review of already-reviewed code* is bought.

## Step 2 - codex gate on the integration commit

Scope the review to the **integration commit**, not the candidate branch in isolation: point codex at
the throwaway `$WORKTREE` (its detached HEAD *is* the merged commit) so it sees `BASE...INTEGRATION` =
the candidate's work applied onto the *current* target. That combined diff is what surfaces a semantic
conflict a per-branch review misses.

**Dispatch this gate with `codex task`, NOT `adversarial-review`.** This is the one place in the
pipeline where the reviewer's OUTPUT FORMAT is load-bearing: `lander.sh commit` re-reads the artifact
and requires a **line-anchored** `VERDICT: SHIP` (`_verdict_ok`). `codex task` writes `rawOutput` as
plain text, so an anchored line survives. `adversarial-review` serialises its result as ONE LINE of
JSON with the verdict inside a `summary` string - no line is anchored, so a genuine SHIP is refused
with exit 8, and the only way past that is `LANDER_VERDICT_OVERRIDE=1`: waiving the REVIEWER to work
around a FORMAT mismatch, which is exactly the reflex the risk/verdict flag split exists to prevent.

> Teaching the interlock to read prose was tried and reverted (2026-08-07). Three codex rounds found
> three distinct fail-opens in it - a shared token rule that relaxed the plain-text path, a whole-line
> token search that read `VERDICT: DO NOT SHIP` as approval, and a boundary check that a zero-width
> space walked through. A fail-closed release interlock should not parse free-form prose; dispatch a
> reviewer whose output is already machine-checkable instead.

```bash
CODEX="$(ls ~/.claude/plugins/cache/openai-codex/codex/*/scripts/codex-companion.mjs | sort -V | tail -1)"
# Write the prompt to a FILE. A prompt containing backticks or $(...) is command-substituted by the
# shell when passed inline, which silently deletes words from the review request.
# $PROMPT_FILE MUST BE ABSOLUTE: the companion resolves it with path.resolve(cwd, ...), i.e. against
# --cwd, so a path relative to your own directory silently resolves inside $WORKTREE and fails.
node "$CODEX" task --background --cwd "$WORKTREE" --prompt-file "$PROMPT_FILE"
```

- `--cwd "$WORKTREE"` is what scopes the review to the merged commit. There is no `--base` on this
  path, so the PROMPT must tell codex to derive the diff itself: `git diff <BASE>...HEAD`.
- **Preamble must match the task path:** this agent has **Bash only - no Read/Glob/Grep**. Never send
  it the read-only `adversarial-review` preamble ("use Read/Glob/Grep"); it will answer that its
  environment exposes no such tools, which reads as a BLOCK but is a dispatch error.
- Do **not** pass `--write`. The companion's `task` is read-only by default; a gate must stay that way.
- **Require the anchored line explicitly**, as its own line: ask for
  `VERDICT: SHIP` / `VERDICT: SHIP-WITH-CHANGES` / `VERDICT: BLOCK` and nothing else on that line.
  Trailing prose on the verdict line is refused by the interlock, by design.
- Poll the job JSON's `result` for completion and arm the wedge watchdog, exactly as `review-gate`
  does - do not hand-roll those mechanics.

- agy: **N/A** (committed diff, no base option) - say so honestly, do not imply agy reviewed.
- Collect codex's verdict (SHIP / SHIP-WITH-CHANGES / BLOCK). Do not fix findings here.

## Step 3 - decide (the gate + risk table)

- **Before an auto-land `commit`, write the integ-keyed verdict record** so `commit` can re-verify against
  the exact integration commit. It derives the path from `<INTEGRATION>` (a stale record for a different
  SHA is ignored). `$CODEX_RESULT_JSON` = the codex job's result JSON you already read:
  ```bash
  printf '%s' "$INTEGRATION" | grep -qE '^[0-9a-f]{40,64}$' || { echo "INTEGRATION is not a git OID — abort"; exit 1; }
  case "$CODEX_RESULT_JSON" in *"
  "*) echo "reviewer artifact path contains a newline — abort"; exit 1 ;; esac
  RECDIR="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")/.claude/state/land-verdicts"; mkdir -p "$RECDIR"
  _t="$(mktemp "$RECDIR/.${INTEGRATION}.rec.XXXXXX")" || { echo "mktemp failed"; exit 1; }
  printf 'CODEX_RESULT=%s\n' "$CODEX_RESULT_JSON" > "$_t" && mv -f "$_t" "$RECDIR/$INTEGRATION.rec"
  ```
  **Check the artifact PARSES before you run `commit`**, so a format mismatch surfaces here as a gate
  problem to re-run, not later as an exit-8 that looks like something to override. Ask the interlock
  itself - do NOT hand-mirror its grep, which drifts on whitespace and CR handling:
  ```bash
  "$CLAUDE_PLUGIN_ROOT/engine/lander.sh" verdict "$CODEX_RESULT_JSON" || {
    echo "artifact is not a clean anchored SHIP — re-gate via 'codex task' (Step 2)." >&2
    echo "Do NOT reach for LANDER_VERDICT_OVERRIDE: that waives the reviewer, not the format." >&2
    exit 1
  }
  ```
  Non-zero here means either a real non-SHIP verdict or an unparseable artifact, and both are stop
  conditions - it must `exit`, not just warn, or the very next line runs `commit` anyway.
  `commit` re-runs the risk classifier (refuses non-LOW, exit 7) and re-reads that artifact, refusing
  (exit 8) unless its last anchored `VERDICT:` line is exactly `SHIP`.
- **The two waivers are SEPARATE (2026-08-06) - neither bypasses integration validation.**
  `LANDER_HUMAN_OVERRIDE=1` (alias `LANDER_RISK_OVERRIDE=1`) waives the RISK CLASS ONLY, logged
  `land-risk-override`. Waiving a missing or non-`SHIP` verdict needs `LANDER_VERDICT_OVERRIDE=1`,
  logged `land-verdict-override`. They were one flag until 2026-08-06, and that conflation is why
  **49 of 59 lands carried 10,498 of 10,890 landed src lines past BOTH checks** while only 392 src
  lines (2.5%) cleared the interlock as designed. Because the classifier fails safe to `HIGH` (see
  below), the risk waiver is the ROUTINE keystroke - so it must never be the one that also waives
  the reviewer. **Setting `LANDER_VERDICT_OVERRIDE=1` means landing over an unmet gate: state that
  in your report, and record it, rather than presenting the land as gated.**

| codex | risk | action |
|-------|------|--------|
| SHIP  | LOW  | write the record (above), then **auto-land:** `${CLAUDE_PLUGIN_ROOT}/engine/lander.sh commit <candidate> <target> <BASE> <INTEGRATION> <WORKTREE>` |
| SHIP  | HIGH | **STOP for a human** even though green - present the diff + risk; a human runs the same `commit` with `LANDER_HUMAN_OVERRIDE=1`. That waives the risk class only; the recorded `SHIP` still has to be there, which is the point - a HIGH-risk land is exactly where you want the reviewer's verdict enforced. |
| SHIP-WITH-CHANGES | any | **not-a-pass:** `abort`, surface the requested changes, STOP for a human (never auto-land a conditional SHIP) |
| BLOCK | any  | **abort:** `${CLAUDE_PLUGIN_ROOT}/engine/lander.sh abort <WORKTREE> "codex BLOCK"`, surface findings, STOP |

- At Stage 2 the classifier is deliberately conservative - an unclassified diff fails safe to `HIGH`,
  so **almost everything routes to the human**. That is the intended posture; the `LOW` auto-path
  exists but rarely fires until Stage 4 widens it on `verify.log` evidence. Do not "help" it fire by
  hand-classifying - the whole point is that risk is static and code-path-based, not agent-judged.
- **`commit` is CAS-protected:** it re-checks that the target still equals `BASE` and fails closed
  (exit 5, target untouched) if another land moved it since `prepare` - report that as "stale base,
  re-run", not a failure of the work.

## Step 4 - report

Every terminal path already appends to `.claude/state/verify.log` (`land-ok`/`land-conflict`/
`land-redsuite`/`land-stale`/`land-abort`). Report to the human: the prepare outcome, codex's
verdict verbatim, the risk class, and what happened (landed / stopped-for-human / aborted). Never claim a land
that the CAS didn't confirm.

## Ceiling

- **Semantic-conflict residual:** serialize + post-merge suite + codex-on-integration mitigate it, not
  eliminate (suite coverage gaps, flakes, skipped live tests). Stated, not hidden.
- **agy blind at the land boundary** - codex gates the merged commit (agy cannot review a
  committed diff).
- **Lock is mkdir-atomic** (no `flock` on macOS/bash 3.2), reclaimed after `LANDER_LOCK_STALE`s; the
  CAS on commit is the real cross-phase integrity guarantee, not the lock.
- Mechanics are covered by `${CLAUDE_PLUGIN_ROOT}/engine/lander.sh --selftest` (merge/conflict/red-suite/stale-base/commit).
  This orchestration skill itself is validated by a live dry-run (design Stage-2 Verification).
- **This code is pending its codex+agy gate** (design: "Gate the lander CODE with codex+agy here").
  Until that passes, run `/land` with a human watching every step.
