#!/usr/bin/env bash
# lander.sh — a serialized local merge queue (the `gated-land` plugin's `land` engine).
#
# Deterministic mechanics only; the codex re-gate is driven by the `/land` SKILL between
# `prepare` and `commit` (the skill is the thing that can drive codex via review-gate). So this
# runs in two phases with the gate in the middle:
#
#   prepare <candidate> [target]  -> lock, capture base, throwaway integration worktree,
#                                    merge --no-ff, full suite, static risk-classify.
#                                    Prints BASE/INTEGRATION/WORKTREE/RISK for the skill,
#                                    LEAVES the throwaway worktree holding the merged commit.
#   commit  <cand> <target> <base> <integration> <wt>  -> ff-only/CAS ref update (fails closed
#                                    if the target moved since prepare), push (opt-in), cleanup.
#   abort   <wt> [reason]          -> discard the throwaway worktree (target never touched).
#
# The lock is NOT held across the gate (a wedged gate would block every lander forever); the
# CAS on commit — expected-old == the base captured at prepare — is the real cross-phase
# integrity guarantee (design step 6). ponytail: CAS-on-commit over lock-across-gate; a stale
# base just fails the land and surfaces, target is never corrupted.
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "lander: not in a git repo" >&2; exit 2; }
COMMON="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
PRIMARY="$(dirname "$COMMON")"                       # the primary checkout (git-common-dir's parent)
LOCK="$COMMON/lander.lock.d"                         # mkdir-atomic lock dir (portable; no flock on macOS/bash3.2)
LOG="$PRIMARY/.claude/state/verify.log"
SUITE_CMD="${LANDER_SUITE_CMD:-make test}"          # overridable (set it in .dev-loop.conf); --selftest injects a fast suite
RISK_CLASSIFY="${LANDER_RISK_CLASSIFY:-$(cd "$(dirname "$0")" && pwd)/risk_classify.py}"   # co-located generic classifier by default
PY="${LANDER_PY:-$PRIMARY/.venv/bin/python}"; [ -x "$PY" ] || PY=python3

_mtime() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0; }  # BSD||GNU

# Bounded-wait atomic lock (mkdir is atomic everywhere; no flock needed). Reclaims a lock left
# by a crashed committer once it is older than LANDER_LOCK_STALE. Held only around the brief
# ref-update — never across the codex gate.
_lock_acquire() {
  local waited=0 timeout="${LANDER_LOCK_TIMEOUT:-120}"
  while :; do
    if mkdir "$LOCK" 2>/dev/null; then printf '%s\n' "$$" > "$LOCK/pid"; return 0; fi
    if [ "$(( $(date +%s) - $(_mtime "$LOCK") ))" -ge "${LANDER_LOCK_STALE:-300}" ]; then
      mv "$LOCK" "$LOCK.dead.$$" 2>/dev/null && rm -rf "$LOCK.dead.$$"   # atomic steal: one winner
      continue
    fi
    [ "$waited" -ge "$timeout" ] && return 1
    sleep 1; waited=$((waited + 1))
  done
}
_lock_release() {
  # Only remove OUR lock. If we went stale and another committer stole it (mv+mkdir), the pid
  # file now holds THEIR pid - deleting it would drop a live owner's lock (codex/agy finding).
  [ "$(cat "$LOCK/pid" 2>/dev/null)" = "$$" ] && rm -rf "$LOCK"
}

log() { # ts \t event \t head \t detail
  local head; head="$(git rev-parse --short HEAD 2>/dev/null || echo none)"
  mkdir -p "$(dirname "$LOG")"
  printf '%s\t%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$head" "${2:-}" >> "$LOG"
}
die() { echo "lander: $1" >&2; log "land-error" "$1"; exit "${2:-1}"; }

# --- risk: static, from the ACTUAL diff, via the deterministic land-risk classifier ---
# Feed `git diff --numstat --no-renames` (line counts, so the size caps apply; --no-renames splits a
# rename into delete+add so each path is policy-checkable). risk_classify.py's --gate mode encodes the
# verdict in the EXIT CODE: 0=LOW, 1=NOT_LOW, 2=could-not-classify. pipefail (set at top) propagates a
# failed `git diff`. FAIL CLOSED: anything other than a clean exit-0 -> HIGH (a discarded error must
# never read as LOW->auto-land). The bundled classifier is dependency-free; set LANDER_RISK_PYTHONPATH
# only if you point LANDER_RISK_CLASSIFY at a custom classifier that imports your own modules.
risk_of() { # base..integration -> prints HIGH|LOW
  local base="$1" integ="$2" rc; local -a extra=()
  # LANDER_RISK_EXTRA_FLAGS (advertised in dev-loop.conf): tighten-only tokens forwarded to the
  # classifier. `set -f` keeps a glob value (e.g. --deny 'infra/**') LITERAL instead of expanding it
  # against the cwd; a MALFORMED value makes eval fail -> we fail CLOSED to HIGH rather than silently
  # continuing with the default classifier (which could return LOW).
  if [ -n "${LANDER_RISK_EXTRA_FLAGS:-}" ]; then
    set -f
    # Validate in an INNER subshell first: an eval PARSE error (e.g. an unbalanced quote) terminates
    # its subshell, and because risk_of itself runs inside a `$(...)` command substitution, an
    # un-contained parse error would kill risk_of before the guard could fail closed. Containing it in
    # `( )` lets the `if !` see the nonzero exit and fail closed to HIGH.
    if ! ( eval "extra=(${LANDER_RISK_EXTRA_FLAGS})" ) >/dev/null 2>&1; then set +f; echo HIGH; return; fi
    eval "extra=(${LANDER_RISK_EXTRA_FLAGS})"   # validated above -> parses cleanly here
    set +f
  fi
  git diff --numstat --no-renames "$base..$integ" 2>/dev/null \
    | PYTHONPATH="${LANDER_RISK_PYTHONPATH:+$LANDER_RISK_PYTHONPATH${PYTHONPATH:+:$PYTHONPATH}}" "$PY" "$RISK_CLASSIFY" --gate "${extra[@]+"${extra[@]}"}" >/dev/null 2>&1
  rc=$?
  [ "$rc" -eq 0 ] && echo LOW || echo HIGH
}

overlap_of() { # base candidate -> prints the merge INTERACTION SURFACE, one path per line
  # Files the candidate touched INTERSECT files the target moved since the fork. Empty means the
  # integration commit introduces no review surface beyond the candidate diff the BRANCH gate
  # already SHIP'd, so the land-stage codex round is re-reviewing already-gated code. Non-empty
  # names exactly the files where the two lines of work can interact semantically.
  #
  # FAIL-CLOSED: any git error prints `unknown`, and the /land skill MUST gate on `unknown`
  # exactly as it gates on a non-empty overlap. Never infer "no overlap" from a failure.
  local base="$1" candidate="$2" fork a b
  fork="$(git merge-base "$base" "$candidate" 2>/dev/null)" || { echo unknown; return; }
  [ -n "$fork" ] || { echo unknown; return; }
  a="$(git diff --name-only "$fork" "$candidate" 2>/dev/null)" || { echo unknown; return; }
  b="$(git diff --name-only "$fork" "$base"      2>/dev/null)" || { echo unknown; return; }
  # `comm` needs sorted input; sed drops the empty line printf emits for an empty side.
  comm -12 <(printf '%s\n' "$a" | sed '/^$/d' | sort -u) \
           <(printf '%s\n' "$b" | sed '/^$/d' | sort -u)
}

reap_abandoned_worktrees() { # remove integration worktrees a dead prepare left behind
  # `prepare` deliberately RELEASES its EXIT trap (`trap - EXIT`) to hand the integration worktree to
  # the gate, so only `commit` or `abort` ever clean it up. A session that dies - or simply ends -
  # between prepare and either one leaks that worktree permanently. Measured 2026-08-07 on one repo:
  # 10 leaked, 7 of them from a single branch with 7 prepares and 0 lands.
  #
  # Reaped by AGE ALONE, against the lander's own `lander-` naming. Deliberately NOT by "no process
  # is inside it": the gate wait is precisely the window where nothing is running, so a liveness
  # probe would reap live work. A whole prepare -> gate -> commit cycle is minutes, so anything older
  # than LANDER_WT_STALE (default 6h) is abandoned by construction.
  #
  # Best-effort throughout: this runs on prepare's critical path and must never be able to fail a
  # land. Concurrent prepares may race to reap the same path - the loser's `remove` just fails.
  local stale="${LANDER_WT_STALE:-21600}" now m age w
  now="$(date +%s)"
  git -C "$PRIMARY" worktree list --porcelain 2>/dev/null | awk '/^worktree /{print $2}' |
  while IFS= read -r w; do
    case "$w" in */lander-*) ;; *) continue ;; esac   # only our own throwaways
    [ -d "$w" ] || continue                            # vanished dir: `prune` below handles the ref
    m="$(stat -f %m "$w" 2>/dev/null || stat -c %Y "$w" 2>/dev/null || echo "$now")"
    age=$(( now - m ))
    [ "$age" -gt "$stale" ] || continue
    if git -C "$PRIMARY" worktree remove --force "$w" 2>/dev/null; then
      log "land-reaped" "abandoned integration worktree $(basename "$w") (idle ${age}s)"
    fi
  done
  git -C "$PRIMARY" worktree prune 2>/dev/null || true
}

target_clean() { # refuse to build on / land onto a dirty target checkout
  # LANDER_STATUS_EXCLUDES: space-separated extra pathspecs to ignore (e.g. a tracked generated dir).
  #
  # $1 = "tracked-only" -> also ignore UNTRACKED files (`-uno`). ONLY `prepare` may pass it, and only
  # because prepare never touches the target's working tree: it creates a throwaway worktree at $base
  # OUTSIDE the repo, merges there, and runs the suite there. A stray untracked file in the target
  # therefore cannot change prepare's result - it only blocks a land for a file nobody is landing.
  # Measured on one repo 2026-08-07: 44 of 46 "target checkout dirty" refusals came from prepare, 2
  # from commit; one of the 44 was a single untracked markdown file.
  #
  # `commit` keeps the STRICT check (untracked included) on purpose: it merges into the target's
  # CHECKED-OUT branch, where an incoming path can collide with an untracked file of the same name
  # and git would refuse mid-merge. Tracked modifications still block BOTH phases - those mean
  # someone is mid-edit, and failing at prepare is kinder than after a full suite run.
  local u=""; [ "${1:-}" = "tracked-only" ] && u="-uno"
  local ex=""; for p in .claude/state ${LANDER_STATUS_EXCLUDES:-}; do ex="$ex :(exclude)$p"; done
  [ -z "$(git -C "$PRIMARY" status --porcelain $u -- . $ex 2>/dev/null)" ]
}

cmd_prepare() {
  local candidate="${1:?usage: prepare <candidate-branch> [target]}" target="${2:-main}"
  git rev-parse --verify -q "$candidate" >/dev/null || die "no such candidate branch: $candidate" 2
  git rev-parse --verify -q "$target"    >/dev/null || die "no such target branch: $target" 2
  target_clean tracked-only || die "target checkout dirty ($PRIMARY) — commit/stash/clean it first"
  reap_abandoned_worktrees   # self-healing: clear leaks from prepares that never reached commit/abort

  local base wt
  base="$(git rev-parse --verify "refs/heads/$target^{commit}")"
  # OUT of the working tree (a worktree inside it reads as untracked -> trips target_clean).
  # Sanitize to a safe charset: a branch name with a quote/space/glob would otherwise break the
  # single-quoted EXIT-trap cleanup string (codex finding).
  wt="${TMPDIR:-/tmp}/lander-$(printf '%s' "$candidate" | tr -c 'A-Za-z0-9._-' '_')-$$"
  # trap cleans the throwaway worktree if we die before handing it off to the gate
  trap 'git -C "$PRIMARY" worktree remove --force "$wt" 2>/dev/null; git -C "$PRIMARY" worktree prune 2>/dev/null' EXIT
  git -C "$PRIMARY" worktree add -q --detach "$wt" "$base" || die "could not create integration worktree"

  # Diagnostics go to SIBLING paths, never inside $wt: both failure branches below reach `die`,
  # which exits and fires the EXIT trap that `worktree remove --force`s $wt - a log written inside
  # it is destroyed on exactly the path it exists to diagnose. Plain `>` (not `tee`): this
  # function's stdout is the machine-readable KEY=VALUE block the caller parses.
  local merge_log="$wt.merge.log" suite_log="$wt.suite.log"

  if ! git -C "$wt" merge --no-ff --no-edit "$candidate" >"$merge_log" 2>&1; then
    git -C "$wt" merge --abort 2>/dev/null
    log "land-conflict" "$candidate onto $target@$base"
    die "MERGE CONFLICT integrating $candidate onto $target — aborted, target untouched (log: $merge_log)" 3
  fi
  local integ; integ="$(git -C "$wt" rev-parse HEAD)"

  # full suite on the EXACT integration commit (catches two-branches-broken-together)
  if ! ( cd "$wt" && eval "$SUITE_CMD" ) >"$suite_log" 2>&1; then
    log "land-redsuite" "$candidate integ=$(git -C "$wt" rev-parse --short HEAD)"
    die "SUITE RED on the integration commit — discarded, target untouched (log: $suite_log)" 4
  fi

  local risk; risk="$(risk_of "$base" "$integ")"

  # Merge interaction surface (see overlap_of). OVERLAP=0 means the branch gate already reviewed
  # every line in this integration commit; OVERLAP=unknown means we could not tell -> treat as gate.
  local ovf ovn
  ovf="$(overlap_of "$base" "$candidate")"
  if [ "$ovf" = unknown ]; then ovn=unknown
  else ovn="$(printf '%s' "$ovf" | grep -c . )"; ovn="${ovn:-0}"; fi

  trap - EXIT                       # hand the worktree off to the gate; commit/abort will clean it
  log "land-prepared" "$candidate risk=$risk overlap=$ovn integ=${integ:0:8}"
  # OVERLAP_FILES is single-quoted: the caller parses this block with `eval`, so an unquoted
  # multi-file value would execute the second path as a command. Single quotes are stripped from
  # paths for the same reason (they would close the quote).
  printf "CANDIDATE=%s\nTARGET=%s\nBASE=%s\nINTEGRATION=%s\nWORKTREE=%s\nRISK=%s\nOVERLAP=%s\nOVERLAP_FILES='%s'\n" \
    "$candidate" "$target" "$base" "$integ" "$wt" "$risk" "$ovn" \
    "$(printf '%s' "$ovf" | tr -d "'" | tr '\n' ' ')"
}

# Re-read codex's OWN artifact; pass ONLY on a clean codex SHIP. Verdict = the LAST line beginning
# 'VERDICT:' (CR stripped for CRLF artifacts), EXACT-match one verdict token, so 'VERDICT: BLOCK'
# + later prose containing SHIP/SPACESHIP fails closed. That shell rule is UNCHANGED and is the only
# definition of a valid token. Missing file / unparseable JSON / absent verdict line -> non-zero
# (fail closed).
#
# TWO REVIEWER OUTPUT SHAPES, one verdict rule. `codex task` writes rawOutput as PLAIN TEXT, so the
# verdict is already a line. `codex adversarial-review` writes rawOutput as a SINGLE LINE OF JSON
# ({"verdict":..,"summary":..,"findings":..}), so no line begins 'VERDICT:' and this returned 1 on a
# genuine SHIP - a false NEGATIVE that pushed the operator toward LANDER_VERDICT_OVERRIDE=1, i.e.
# waiving the reviewer to work around a FORMAT bug. That is precisely the reflex the 2026-08-06
# risk/verdict flag split exists to prevent, so the parser learns the second shape instead.
#
# The envelope branch parses `summary` in PYTHON and hands the shell a NORMALISED bare verdict line,
# so prose tolerance never touches the plain-text path. An earlier cut of this fix instead widened
# the SHARED shell rule to allow a delimiter after the token; because it is shared, that silently
# relaxed plain text too, and 'VERDICT: SHIP BLOCK' / 'VERDICT: SHIP: BLOCK' / 'VERDICT: SHIP,
# pending fixes' went from rejected to ACCEPTED (codex, [high]) - a fail-OPEN introduced by a fix
# for a fail-closed nuisance. Prose tolerance belongs only where prose is expected.
#
# It does NOT trust the envelope's own `verdict` field: a codex turn that dies mid-review (model
# capacity, wedge) still serialises {"verdict":"approve", summary:<the model's opening plan
# sentence>}, so `approve` there can mean "crashed before reviewing anything". The model-emitted
# VERDICT token is the only signal that requires the review to have actually reached a conclusion -
# observed twice on 2026-08-08, both crashes rendering as "Verdict: approve".
_verdict_ok() { # <codex_result_json>
  local cres="$1" craw cverd
  [ -f "$cres" ] || return 1
  craw="$("$PY" - "$cres" <<'PYEOF' 2>/dev/null
import json, os, re, sys
d = json.load(open(os.path.expanduser(sys.argv[1]), encoding="utf-8", errors="replace"))
raw = d["result"].get("rawOutput", "") or ""

# PLAIN TEXT: emit verbatim. The shell rule below then applies EXACT-match, exactly as it always
# has. An earlier revision of this fix widened that shell rule to tolerate a delimiter, and because
# the rule is SHARED, it silently relaxed this path too: 'VERDICT: SHIP BLOCK', 'VERDICT: SHIP:
# BLOCK' and 'VERDICT: SHIP, pending fixes' all went from rejected to ACCEPTED (codex, [high]) -
# a fail-OPEN in the interlock, introduced by a change meant to fix a fail-closed nuisance. Prose
# tolerance belongs ONLY where prose is expected, so it lives below, after the envelope decodes.
if re.search(r"^VERDICT:", raw, re.M):
    print(raw)
    raise SystemExit(0)

# STRUCTURED ENVELOPE (codex adversarial-review): rawOutput is one line of JSON and the verdict
# sits in `summary` as prose. Parse it HERE and hand the shell a NORMALISED bare verdict line, so
# the shell's exact-match rule stays the single definition of a valid token and never has to learn
# about prose. The envelope's own `verdict` field is deliberately ignored: a codex turn that dies
# mid-review still serialises {"verdict":"approve", summary:<opening plan sentence>}.
try:
    inner = json.loads(raw)
except Exception:
    raise SystemExit(0)                      # unparseable -> emit nothing -> fail closed
summary = inner.get("summary") if isinstance(inner, dict) else None
if not isinstance(summary, str):
    raise SystemExit(0)

last = None
for line in summary.replace("\r", "").split("\n"):
    if line.startswith("VERDICT:"):          # line-anchored, same as the shell rule
        last = line                          # last one wins, same as the shell rule
if last is None:
    raise SystemExit(0)

# Longest alternative first (Python alternation is leftmost-FIRST, not longest), and a negative
# lookahead so SHIP cannot match inside SHIP-WITH-CHANGES or SHIPPING, nor at the tail of SPACESHIP.
found = re.findall(r"\b(?:SHIP-WITH-CHANGES|BLOCK|SHIP)(?![\w-])", last)
# EXACTLY one token on the line. Two means the conclusion contradicts itself ('SHIP ... BLOCK'),
# and an interlock must not pick a winner from an ambiguous verdict - it refuses.
if len(found) == 1:
    print("VERDICT: %s" % found[0])
PYEOF
)" || return 1
  cverd="$(printf '%s\n' "$craw" | tr -d '\r' | grep -E '^VERDICT:' | tail -1 | sed 's/^VERDICT:[[:space:]]*//' | grep -oE '^(SHIP-WITH-CHANGES|BLOCK|SHIP)$')"
  [ "$cverd" = SHIP ] || return 1
  return 0
}

cmd_commit() {
  local candidate="${1:?}" target="${2:?}" base="${3:?}" integ="${4:?}" wt="${5:?}"
  _lock_acquire || die "could not acquire lander lock within timeout"
  # Release the lock AND clean the throwaway worktree however we exit. Expand paths NOW (double
  # quotes): this EXIT trap fires at top level after the function returns, where the locals are
  # already out of scope (set -u would abort the trap and skip cleanup otherwise).
  trap "_lock_release; git -C '$PRIMARY' worktree remove --force '$wt' 2>/dev/null; git -C '$PRIMARY' worktree prune 2>/dev/null" EXIT

  target_clean || die "target checkout dirty at commit time — refusing to land"

  # integration validation (ALWAYS, even under override): integ must be a real commit that IS the tree
  # we are about to land (the throwaway worktree's HEAD). Blocks a record aimed at a fabricated SHA.
  git rev-parse --verify -q "$integ^{commit}" >/dev/null 2>&1 || die "integ is not a valid commit: $integ" 8
  [ "$(git -C "$wt" rev-parse HEAD 2>/dev/null)" = "$integ" ] || die "integration worktree HEAD != $integ — refusing" 8

  # --- coded interlocks (waived only via explicit, separately-logged escape hatches) ---
  # TWO INDEPENDENT waivers. LANDER_HUMAN_OVERRIDE (and its alias LANDER_RISK_OVERRIDE) waives the
  # RISK CLASS ONLY. Waiving the recorded codex verdict now requires LANDER_VERDICT_OVERRIDE=1 as a
  # separate, deliberate act. Measured 2026-08-06: one combined flag carried 49 of 59 lands and
  # 10,498 of 10,890 landed src lines past BOTH checks, so every routine risk waiver silently
  # carried a verdict waiver with it, and only 392 src lines (2.5%) ever cleared the gate as
  # designed - every one of those a flake fix, a test-only branch, or a small UI follow-up.
  # The risk classifier is deliberately conservative (unclassified fails safe to HIGH), so the risk
  # waiver is the ROUTINE keystroke - which is exactly why it must not also waive the verdict.
  local rec="$PRIMARY/.claude/state/land-verdicts/$integ.rec"
  local risk_waived="${LANDER_RISK_OVERRIDE:-${LANDER_HUMAN_OVERRIDE:-0}}"
  local verdict_waived="${LANDER_VERDICT_OVERRIDE:-0}"

  if [ "$risk_waived" = "1" ]; then
    log "land-risk-override" "$candidate integ=${integ:0:8} reason=${LANDER_OVERRIDE_REASON:-human}"
  else
    local risk; risk="$(risk_of "$base" "$integ")"
    [ "$risk" = LOW ] || { log "land-risk-block" "$candidate risk=$risk"; \
      die "RISK=$risk — auto-land refused; a human reviews and re-runs with LANDER_HUMAN_OVERRIDE=1" 7; }
  fi

  if [ "$verdict_waived" = "1" ]; then
    log "land-verdict-override" "$candidate integ=${integ:0:8} reason=${LANDER_OVERRIDE_REASON:-human}"
  else
    [ -f "$rec" ] || { log "land-verdict-block" "$candidate no-record"; \
      die "no verdict record for integration $integ — gate did not record a pass. Re-run the gate, or set LANDER_VERDICT_OVERRIDE=1 to land over a missing verdict" 8; }
    local cres
    cres="$(sed -n 's/^CODEX_RESULT=//p'    "$rec" | head -1)"
    _verdict_ok "$cres" || { log "land-verdict-block" "$candidate not-SHIP"; \
      die "recorded codex verdict is not a clean SHIP for $integ — refusing to land. Fix and re-gate, or set LANDER_VERDICT_OVERRIDE=1 to land over the verdict" 8; }
  fi

  local now; now="$(git rev-parse --verify "refs/heads/$target^{commit}")"
  [ "$now" = "$base" ] || { log "land-stale" "$candidate base=$base now=$now"; \
    die "STALE BASE: $target moved $base -> $now since prepare — re-run the lander" 5; }

  # ATOMIC CAS ref update in both cases (expected-old=base) - this is the real integrity guard,
  # so it must be a single atomic op, not a check-then-act (codex TOCTOU finding). If the target
  # is checked out in the primary, update-ref the branch then sync the (verified-clean) worktree.
  if [ "$(git -C "$PRIMARY" rev-parse --abbrev-ref HEAD 2>/dev/null)" = "$target" ]; then
    git -C "$PRIMARY" update-ref -m "lander: $candidate" "refs/heads/$target" "$integ" "$base" \
      || die "CAS update-ref failed (target moved since prepare)" 5
    git -C "$PRIMARY" reset --hard "$integ" >/dev/null 2>&1 \
      || die "worktree sync (reset --hard $integ) failed after ref update" 5
  else
    git update-ref "refs/heads/$target" "$integ" "$base" || die "CAS update-ref failed (target moved)" 5
  fi

  local pushed="n/a"
  if [ "${LANDER_PUSH:-0}" = "1" ]; then
    if git -C "$PRIMARY" push -q origin "$target" 2>/dev/null; then pushed=ok; else
      pushed=FAILED
      echo "lander: WARNING - $target landed LOCALLY but push to origin failed; push manually" >&2
      log "land-push-failed" "$target"
    fi
  fi
  # retire the session branch's worktree if it still exists (best-effort)
  local swt; swt="$(git -C "$PRIMARY" worktree list --porcelain | awk -v b="refs/heads/$candidate" '
    $1=="worktree"{w=$2} $1=="branch"&&$2==b{print w}')"
  [ -n "$swt" ] && git -C "$PRIMARY" worktree remove --force "$swt" 2>/dev/null
  log "land-ok" "$candidate -> $target @ ${integ:0:8} push=$pushed"
  rm -f "$rec" 2>/dev/null   # record consumed; orphan records on non-success exits are harmless (see SKILL Ceiling)
  # The LOCAL land is done and irreversible (ref moved) -> always log land-ok. But a REQUESTED
  # push that failed means "not fully done": exit non-zero so a LANDER_PUSH=1 caller can't read
  # exit 0 as "pushed" (codex finding). land-ok stays as the record that it landed locally.
  if [ "$pushed" = FAILED ]; then
    echo "LANDED-LOCAL-ONLY $candidate -> $target @ ${integ:0:8} (push FAILED - push manually)"
    return 6
  fi
  echo "LANDED $candidate -> $target @ ${integ:0:8}"
}

cmd_abort() {
  local wt="${1:?usage: abort <worktree> [reason]}" reason="${2:-unspecified}"
  git -C "$PRIMARY" worktree remove --force "$wt" 2>/dev/null
  git -C "$PRIMARY" worktree prune 2>/dev/null
  log "land-abort" "$reason"
  echo "ABORTED land ($reason) — target untouched"
}

# --------------------------------------------------------------------------------------------
selftest() {
  local d py; d="$(mktemp -d)"; py="$PY"
  # run entirely in a throwaway repo with GIT_* stripped (a leaked GIT_DIR would rewrite the
  # REAL repo — see CLAUDE.md). All git/lander calls below inherit the scrubbed env.
  env -i PATH="$PATH" HOME="$HOME" LANDER_PY="$py" SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")" \
      D="$d" bash -euo pipefail <<'EOSELF'
cd "$D"
git init -q; git config user.email t@t; git config user.name t
mkdir -p src docs; echo "print('ok')" > src/a.py; echo hi > docs/x.md
git add -A; git commit -qm base
main=$(git rev-parse HEAD)

pass=0; fail=0
check(){ if eval "$2"; then pass=$((pass+1)); else echo "FAIL: $1"; fail=$((fail+1)); fi; }

# fake fast suite + a fake land-risk classifier honoring the --gate contract: reads `git diff
# --numstat` on STDIN, and with --gate exits 0=LOW / 1=NOT_LOW / 2=error. NOT_LOW if any changed path
# is under src/secrets/ or the diff exceeds 15 files; else LOW. Keep it OUT of the tracked tree
# (untracked files would trip target_clean) via git's local exclude.
export LANDER_SUITE_CMD='true'
# .claude/ is gitignored in the real repo (verify.log lives there); mirror that here so the
# lander's own logging doesn't read as a dirty target. risk_classify.py is the fake classifier.
printf 'risk_classify.py\nexit2_classifier.py\ntrusted/\nimp_classifier.py\n' >> .git/info/exclude
cat > risk_classify.py <<'PYEOF'
import sys, json
lines = [ln for ln in sys.stdin.read().splitlines() if ln.strip()]
if any(len(ln.split('\t')) != 3 for ln in lines):   # malformed numstat -> could-not-classify (mirror real)
    print(json.dumps({"risk": "NOT_LOW", "reasons": ["could not classify"]})); sys.exit(2)
paths = [ln.split('\t', 2)[2] for ln in lines]
not_low = any('src/secrets/' in p for p in paths) or len(paths) > 15
print(json.dumps({"risk": "NOT_LOW" if not_low else "LOW"}))
sys.exit((1 if not_low else 0) if '--gate' in sys.argv else 0)
PYEOF
export LANDER_RISK_CLASSIFY="$D/risk_classify.py"

# 1. clean benign src-only candidate -> prepare succeeds, RISK=LOW (the new classifier widens LOW)
git checkout -q -b session/feat "$main"
echo "print('feat')" >> src/a.py; git commit -qam feat
rc=0; out=$("$SELF" prepare session/feat main) || rc=$?
check "prepare ok" "[ $rc -eq 0 ]"
eval "$out"                              # imports BASE/INTEGRATION/WORKTREE/RISK
check "prepare RISK=LOW on benign src" "[ '$RISK' = LOW ]"
check "worktree left in place" "[ -d '$WORKTREE' ]"
check "target untouched by prepare" "[ \"$(git rev-parse main)\" = '$main' ]"

# 1b. sensitive-path candidate -> RISK=HIGH
git checkout -q -b session/sens "$main"
mkdir -p src/secrets; echo "x=1" > src/secrets/key.py
git add src/secrets/key.py; git commit -qam sens
rc=0; out2=$("$SELF" prepare session/sens main) || rc=$?
check "prepare ok (sensitive)" "[ $rc -eq 0 ]"
RISK_SENS=$(printf '%s\n' "$out2" | sed -n 's/^RISK=//p'); WT_SENS=$(printf '%s\n' "$out2" | sed -n 's/^WORKTREE=//p')
check "prepare RISK=HIGH on src/secrets" "[ '$RISK_SENS' = HIGH ]"
"$SELF" abort "$WT_SENS" cleanup >/dev/null 2>&1 || true

# 2. commit lands it (target checked out is 'session/feat', not main -> CAS update-ref path)
git checkout -q session/feat
rc=0; LANDER_HUMAN_OVERRIDE=1 LANDER_VERDICT_OVERRIDE=1 "$SELF" commit session/feat main "$BASE" "$INTEGRATION" "$WORKTREE" >/dev/null || rc=$?
check "commit ok" "[ $rc -eq 0 ]"
check "main advanced to integration" "[ \"$(git rev-parse main)\" = '$INTEGRATION' ]"
check "throwaway worktree cleaned" "[ ! -d '$WORKTREE' ]"

# 3. STALE BASE: prepare, then move main out-of-band, then commit must fail closed
git checkout -q -b session/f2 main
echo x >> src/a.py; git commit -qam f2
out=$("$SELF" prepare session/f2 main); eval "$out"
git checkout -q -b session/other main; git commit -q --allow-empty -m other
git update-ref refs/heads/main "$(git rev-parse session/other)"   # move main behind the lander's back
before=$(git rev-parse main)
rc=0; LANDER_HUMAN_OVERRIDE=1 "$SELF" commit session/f2 main "$BASE" "$INTEGRATION" "$WORKTREE" >/dev/null 2>&1 || rc=$?
check "stale-base commit fails" "[ $rc -ne 0 ]"
check "target unchanged on stale" "[ \"$(git rev-parse main)\" = \"$before\" ]"

# 4. MERGE CONFLICT -> prepare fails, target untouched
git update-ref refs/heads/main "$main"; git checkout -q main 2>/dev/null || git checkout -q -f main
# two branches editing the SAME line -> conflict
git checkout -q -b session/c1 main; printf 'LINE-A\n' > src/a.py; git commit -qam c1
git checkout -q main; printf 'LINE-B\n' > src/a.py; git commit -qam mainline; mainc=$(git rev-parse HEAD)
git checkout -q -b session/c2 "$main"; printf 'LINE-C\n' > src/a.py; git commit -qam c2
rc=0; "$SELF" prepare session/c2 main >/dev/null 2>&1 || rc=$?
check "conflict prepare fails" "[ $rc -eq 3 ]"
check "target untouched on conflict" "[ \"$(git rev-parse main)\" = \"$mainc\" ]"

# 5. RED SUITE -> prepare fails, AND the diagnostic log outlives the cleanup trap.
# The trap on the die path `worktree remove --force`s $wt, so the log must be a SIBLING of $wt,
# never inside it - an in-worktree log is deleted on exactly the failure it exists to explain.
git checkout -q -b session/red main; echo z>>docs/x.md; git commit -qam red
rc=0; rederr="$(LANDER_SUITE_CMD='sh -c "echo RED_MARKER; exit 1"' "$SELF" prepare session/red main 2>&1 >/dev/null)" || rc=$?
check "red-suite prepare fails" "[ $rc -eq 4 ]"
redlog="$(printf '%s' "$rederr" | sed -n 's/.*(log: \(.*\))$/\1/p')"
check "red-suite error names a log path" "[ -n \"$redlog\" ]"
check "red-suite log survives cleanup with the failure output" "[ -s \"$redlog\" ] && grep -q RED_MARKER \"$redlog\""
check "red-suite log is a sibling, not inside the removed worktree" "[ ! -e \"${redlog%.suite.log}\" ]"
rm -f "$redlog"

# 6. TARGET CHECKED OUT in primary -> update-ref CAS + reset --hard path (the real-world case)
git checkout -q main                         # primary now ON the target
git checkout -q -b session/co main; echo "print('co')">>src/a.py; git commit -qam co
git checkout -q main                         # back on target, clean
before6=$(git rev-parse main)
out=$("$SELF" prepare session/co main); eval "$out"
rc=0; LANDER_HUMAN_OVERRIDE=1 LANDER_VERDICT_OVERRIDE=1 "$SELF" commit session/co main "$BASE" "$INTEGRATION" "$WORKTREE" >/dev/null || rc=$?
check "checked-out commit ok" "[ $rc -eq 0 ]"
check "checked-out main advanced" "[ \"$(git rev-parse main)\" = '$INTEGRATION' ]"
check "checked-out HEAD==main (worktree synced)" "[ \"$(git rev-parse HEAD)\" = '$INTEGRATION' ]"
check "checked-out worktree clean after reset" "[ -z \"$(git status --porcelain -- . ':(exclude).claude/state')\" ]"

# 7. CLASSIFIER CANNOT RUN -> risk fails CLOSED to HIGH (not empty->LOW->auto-land)
git checkout -q -b session/rc main; echo "print('rc')">>src/a.py; git commit -qam rc; git checkout -q main
out=$(LANDER_RISK_CLASSIFY="$D/no_such_classifier.py" "$SELF" prepare session/rc main); eval "$out"
check "classifier-fail -> RISK=HIGH (fail closed)" "[ '$RISK' = HIGH ]"
"$SELF" abort "$WORKTREE" cleanup >/dev/null 2>&1 || true

# 8. LANDER_PUSH=1 with no remote -> local land succeeds but push fails -> exit 6, target advanced
git checkout -q -b session/pf main; echo "print('pf')">>src/a.py; git commit -qam pf; git checkout -q main
out=$("$SELF" prepare session/pf main); eval "$out"
rc=0; LANDER_PUSH=1 LANDER_HUMAN_OVERRIDE=1 LANDER_VERDICT_OVERRIDE=1 "$SELF" commit session/pf main "$BASE" "$INTEGRATION" "$WORKTREE" >/dev/null 2>&1 || rc=$?
check "push-fail exits 6" "[ $rc -eq 6 ]"
check "push-fail still landed locally" "[ \"$(git rev-parse main)\" = '$INTEGRATION' ]"

# 9. CLASSIFIER RAN but could-not-classify (exit 2) -> RISK=HIGH (distinct from missing-classifier)
cat > exit2_classifier.py <<'PYEOF'
import sys, json
sys.stdin.read()
print(json.dumps({"risk": "NOT_LOW", "reasons": ["could not classify"]})); sys.exit(2)
PYEOF
git checkout -q -b session/e2 main; echo "print('e2')">>src/a.py; git commit -qam e2; git checkout -q main
out=$(LANDER_RISK_CLASSIFY="$D/exit2_classifier.py" "$SELF" prepare session/e2 main); eval "$out"
check "classifier exit-2 -> RISK=HIGH (fail closed)" "[ '$RISK' = HIGH ]"
"$SELF" abort "$WORKTREE" cleanup >/dev/null 2>&1 || true

# 10. FAIL-CLOSED on a failing upstream `git diff`: the exact risk_of pipeline pattern against a bogus
# range must return NON-ZERO (pipefail) so risk_of would echo HIGH, never LOW.
rc=0; git diff --numstat --no-renames no-such-ref..HEAD 2>/dev/null \
  | "$LANDER_PY" "$D/risk_classify.py" --gate >/dev/null 2>&1 || rc=$?
check "git-diff-fail -> nonzero (pipefail fail-closed)" "[ $rc -ne 0 ]"

# 11. SELF-DIRTY (P-C regression): lander writes .claude/state/verify.log during prepare; target_clean must
# exclude it via pathspec so commit is not refused. .claude/ is NOT in .git/info/exclude here.
git checkout -q -b session/sd main; echo "print('sd')">>src/a.py; git commit -qam sd; git checkout -q main
out=$("$SELF" prepare session/sd main); eval "$out"
rc=0; LANDER_HUMAN_OVERRIDE=1 LANDER_VERDICT_OVERRIDE=1 "$SELF" commit session/sd main "$BASE" "$INTEGRATION" "$WORKTREE" >/dev/null 2>&1 || rc=$?
check "self-dirty: commit succeeds despite .claude/state/verify.log" "[ $rc -eq 0 ]"

# 11b. UNTRACKED STRAY: prepare must IGNORE an untracked file in the target (it merges + runs the
# suite in a throwaway worktree at $base, so a stray cannot affect its result), while a TRACKED
# modification must still block it. 44 of 46 measured "target checkout dirty" refusals were prepare.
git checkout -q -b session/untr main; echo "print('untr')">>src/a.py; git commit -qam untr; git checkout -q main
printf 'stray, untracked, unrelated to any merge\n' > STRAY_NOTE.md
rc=0; out=$("$SELF" prepare session/untr main 2>&1) || rc=$?
check "untracked stray does NOT block prepare" "[ $rc -eq 0 ]"
eval "$out"; "$SELF" abort "$WORKTREE" "selftest 11b" >/dev/null 2>&1
# commit keeps the STRICT check: the same untracked file must still refuse the land.
out=$("$SELF" prepare session/untr main); eval "$out"
rc=0; LANDER_HUMAN_OVERRIDE=1 LANDER_VERDICT_OVERRIDE=1 "$SELF" commit session/untr main "$BASE" "$INTEGRATION" "$WORKTREE" >/dev/null 2>&1 || rc=$?
check "untracked stray STILL blocks commit (strict)" "[ $rc -ne 0 ]"
rm -f STRAY_NOTE.md
# ...and a TRACKED modification must still block prepare, stray or not.
echo "print('dirty-tracked')" >> src/a.py
rc=0; "$SELF" prepare session/untr main >/dev/null 2>&1 || rc=$?
check "tracked modification STILL blocks prepare" "[ $rc -ne 0 ]"
git checkout -q -- src/a.py

# 11c. REAPER: prepare releases its EXIT trap so the gate can use the integration worktree, so an
# abandoned prepare leaks it forever (10 leaked in one real repo, 7 from one branch). The NEXT
# prepare must reap a STALE one - and must NOT reap a fresh one, which would delete a live gate's
# worktree out from under it. Both directions are load-bearing.
stalewt="${TMPDIR:-/tmp}/lander-STALE-selftest-$$"
freshwt="${TMPDIR:-/tmp}/lander-FRESH-selftest-$$"
git worktree add -q --detach "$stalewt" main
git worktree add -q --detach "$freshwt" main
touch -t 202001010000 "$stalewt"        # backdate well past LANDER_WT_STALE
out=$("$SELF" prepare session/untr main); eval "$out"
check "stale abandoned worktree is reaped" "[ ! -d '$stalewt' ]"
check "FRESH worktree is NOT reaped (a live gate holds it)" "[ -d '$freshwt' ]"
check "reap is audited in verify.log" "grep -q land-reaped '$D/.claude/state/verify.log'"
"$SELF" abort "$WORKTREE" "selftest 11c" >/dev/null 2>&1
git worktree remove --force "$freshwt" 2>/dev/null; git worktree prune

# 12. LANDER_RISK_PYTHONPATH (P-D): a classifier that imports a module present ONLY via the trusted seam.
mkdir -p "$D/trusted"; printf 'OK = True\n' > "$D/trusted/trustedmod.py"
cat > "$D/imp_classifier.py" <<'PYEOF'
import sys, json, trustedmod
sys.stdin.read()
print(json.dumps({"risk": "LOW"})); sys.exit(0)
PYEOF
git checkout -q -b session/tp main; echo "print('tp')">>src/a.py; git commit -qam tp; git checkout -q main
out=$(LANDER_RISK_CLASSIFY="$D/imp_classifier.py" LANDER_RISK_PYTHONPATH="$D/trusted" "$SELF" prepare session/tp main); eval "$out"
check "trusted-pythonpath classifier imports -> RISK=LOW" "[ '$RISK' = LOW ]"
"$SELF" abort "$WORKTREE" cleanup >/dev/null 2>&1 || true

# 13. TAG SHADOW (P-E): a tag named identically to the target must not corrupt the base capture.
git checkout -q -b session/tg main; echo "print('tg')">>src/a.py; git commit -qam tg; git checkout -q main
git tag main "$(git rev-parse session/tg)" 2>/dev/null   # tag 'main' shadows branch 'main'
out=$("$SELF" prepare session/tg main); eval "$out"
check "tag-shadow: BASE from refs/heads/main not the tag" "[ '$BASE' = \"$(git rev-parse refs/heads/main)\" ]"
git tag -d main >/dev/null 2>&1 || true
"$SELF" abort "$WORKTREE" cleanup >/dev/null 2>&1 || true

# ---- Task 1: risk + integ-bound reviewer-verdict interlock -------------------------------------
# artifacts live under .claude/state (excluded by target_clean) so they don't dirty the target.
mk_codex(){ mkdir -p "$D/.claude/state"; printf '{"result":{"rawOutput":"review body\\nVERDICT: %s\\n"}}\n' "$1" > "$D/.claude/state/codex_$2.json"; echo "$D/.claude/state/codex_$2.json"; }
mk_rec(){ local rd="$D/.claude/state/land-verdicts"; mkdir -p "$rd"; printf 'CODEX_RESULT=%s\n' "$2" > "$rd/$1.rec"; }

# 14. LOW + codex SHIP + record -> commits, record cleaned
git checkout -q -b session/ok main; echo "print('ok14')">>src/a.py; git commit -qam ok14; git checkout -q main
out=$("$SELF" prepare session/ok main); eval "$out"
mk_rec "$INTEGRATION" "$(mk_codex SHIP ok)"
rc=0; "$SELF" commit session/ok main "$BASE" "$INTEGRATION" "$WORKTREE" >/dev/null 2>&1 || rc=$?
check "interlock: LOW+SHIP commits" "[ $rc -eq 0 ]"
check "interlock: main advanced" "[ \"$(git rev-parse main)\" = '$INTEGRATION' ]"
check "interlock: .rec cleaned on success" "[ ! -f \"$D/.claude/state/land-verdicts/$INTEGRATION.rec\" ]"

# 15. codex BLOCK -> exit 8, target untouched
git checkout -q -b session/blk main; echo "print('blk')">>src/a.py; git commit -qam blk; git checkout -q main
before15=$(git rev-parse main); out=$("$SELF" prepare session/blk main); eval "$out"
mk_rec "$INTEGRATION" "$(mk_codex BLOCK blk)"
rc=0; "$SELF" commit session/blk main "$BASE" "$INTEGRATION" "$WORKTREE" >/dev/null 2>&1 || rc=$?
check "interlock: codex BLOCK refused exit 8" "[ $rc -eq 8 ]"
check "interlock: target untouched on BLOCK" "[ \"$(git rev-parse main)\" = \"$before15\" ]"
"$SELF" abort "$WORKTREE" cleanup >/dev/null 2>&1 || true

# 15b. anchored parse: 'VERDICT: BLOCK' then prose with SHIP/SPACESHIP -> still refused
git checkout -q -b session/fo main; echo "print('fo')">>src/a.py; git commit -qam fo; git checkout -q main
out=$("$SELF" prepare session/fo main); eval "$out"; mkdir -p "$D/.claude/state"
printf '{"result":{"rawOutput":"VERDICT: BLOCK\\nafter fixing we could SHIP the SPACESHIP\\n"}}\n' > "$D/.claude/state/codex_fo.json"
mk_rec "$INTEGRATION" "$D/.claude/state/codex_fo.json"
rc=0; "$SELF" commit session/fo main "$BASE" "$INTEGRATION" "$WORKTREE" >/dev/null 2>&1 || rc=$?
check "interlock: BLOCK-then-SHIP-prose refused (anchored)" "[ $rc -eq 8 ]"
"$SELF" abort "$WORKTREE" cleanup >/dev/null 2>&1 || true

# 15c. CRLF verdict still parses as SHIP -> commits
git checkout -q -b session/crlf main; echo "print('crlf')">>src/a.py; git commit -qam crlf; git checkout -q main
out=$("$SELF" prepare session/crlf main); eval "$out"; mkdir -p "$D/.claude/state"
printf '{"result":{"rawOutput":"body\\r\\nVERDICT: SHIP\\r\\n"}}\n' > "$D/.claude/state/codex_crlf.json"
mk_rec "$INTEGRATION" "$D/.claude/state/codex_crlf.json"
rc=0; "$SELF" commit session/crlf main "$BASE" "$INTEGRATION" "$WORKTREE" >/dev/null 2>&1 || rc=$?
check "interlock: CRLF verdict parses -> commits" "[ $rc -eq 0 ]"

# 15d-h. THE SECOND REVIEWER SHAPE. `codex adversarial-review` writes rawOutput as ONE LINE of JSON,
# so no line begins 'VERDICT:' and the verdict lives inside the `summary` string. Before this was
# handled, a genuine SHIP was REFUSED (exit 8) and the only way past it was LANDER_VERDICT_OVERRIDE=1
# - waiving the reviewer to route around a format bug. These pin the fix AND the three ways it must
# not fail open. `env_json` builds the real envelope shape, `verdict` field included, because the
# parser must ignore that field (see _verdict_ok).
# The selftest body re-execs under `env -i` (see selftest()), so `$PY` from line 29 is NOT in scope
# here - only LANDER_PY is exported. Using $PY makes every case below fail on an unbound variable,
# and because these cases assert exit 8, four of the five would still "pass" - on a broken artifact
# rather than on the parser. A negative case that cannot tell a real refusal from a missing file is
# not a test, so the interpreter is resolved the same way the selftest harness resolves it.
_tpy(){ echo "${LANDER_PY:-python3}"; }
env_json(){ "$(_tpy)" -c 'import json,sys;print(json.dumps({"verdict":sys.argv[1],"summary":sys.argv[2],"findings":[],"next_steps":[]}))' "$1" "$2"; }
mk_env(){ mkdir -p "$D/.claude/state"; "$(_tpy)" -c 'import json,sys;json.dump({"result":{"rawOutput":sys.argv[2]}},open(sys.argv[1],"w"))' "$D/.claude/state/codex_$1.json" "$2"; echo "$D/.claude/state/codex_$1.json"; }
env_case(){ # <slug> <envelope-verdict> <summary> <expected-rc> <label>
  git checkout -q -b "session/$1" main; echo "print('$1')">>src/a.py; git commit -qam "$1"; git checkout -q main
  out=$("$SELF" prepare "session/$1" main); eval "$out"
  mk_rec "$INTEGRATION" "$(mk_env "$1" "$(env_json "$2" "$3")")" >/dev/null
  rc=0; "$SELF" commit "session/$1" main "$BASE" "$INTEGRATION" "$WORKTREE" >/dev/null 2>&1 || rc=$?
  check "$5" "[ $rc -eq $4 ]"
  [ "$rc" -eq 0 ] || "$SELF" abort "$WORKTREE" cleanup >/dev/null 2>&1 || true
}
env_case advship approve 'VERDICT: SHIP. No material blocking issue.' 0 \
  "adversarial-review: SHIP inside summary -> commits"
env_case advcrash approve 'I am applying the code-review skill to inspect the diff.' 8 \
  "adversarial-review: CRASHED turn (verdict=approve, no token) refused"
env_case advswc 'needs-attention' 'VERDICT: SHIP-WITH-CHANGES. Fix the guard first.' 8 \
  "adversarial-review: SHIP-WITH-CHANGES never reads as SHIP"
env_case advblk 'needs-attention' 'VERDICT: BLOCK. After fixing we could SHIP the SPACESHIP.' 8 \
  "adversarial-review: BLOCK with SHIP prose refused"
env_case advpfx approve 'VERDICT: SHIPPING SOON. we think' 8 \
  "adversarial-review: SHIPPING is a prefix, not the token"
env_case advcontra approve 'VERDICT: SHIP BLOCK' 8 \
  "adversarial-review: two tokens on the line is ambiguous -> refused"
env_case advlast approve 'VERDICT: SHIP. good
VERDICT: BLOCK. actually no' 8 \
  "adversarial-review: last verdict line wins even when it blocks"

# 15i-l. PLAIN TEXT MUST NOT HAVE BEEN RELAXED. The first cut of envelope support widened the SHARED
# shell rule to tolerate a delimiter after the token, which silently relaxed plain text too:
# 'VERDICT: SHIP BLOCK' went from rejected to ACCEPTED (codex, [high]). These pin that it stayed
# exact-match now that prose tolerance lives only inside the envelope branch.
plain_case(){ # <slug> <rawOutput> <expected-rc> <label>
  git checkout -q -b "session/$1" main; echo "print('$1')">>src/a.py; git commit -qam "$1"; git checkout -q main
  out=$("$SELF" prepare "session/$1" main); eval "$out"
  mk_rec "$INTEGRATION" "$(mk_env "$1" "$2")" >/dev/null
  rc=0; "$SELF" commit "session/$1" main "$BASE" "$INTEGRATION" "$WORKTREE" >/dev/null 2>&1 || rc=$?
  check "$4" "[ $rc -eq $3 ]"
  [ "$rc" -eq 0 ] || "$SELF" abort "$WORKTREE" cleanup >/dev/null 2>&1 || true
}
plain_case pfospc 'VERDICT: SHIP BLOCK' 8 "plain text: 'SHIP BLOCK' still refused (exact-match)"
plain_case pfocol 'VERDICT: SHIP: BLOCK' 8 "plain text: 'SHIP: BLOCK' still refused"
plain_case pfocom 'VERDICT: SHIP, pending fixes' 8 "plain text: 'SHIP, pending fixes' still refused"
plain_case pfoprose 'VERDICT: SHIP the release once tests pass' 8 \
  "plain text: 'SHIP <prose>' still refused"

# 16. codex SHIP-WITH-CHANGES -> exit 8
git checkout -q -b session/swc main; echo "print('swc')">>src/a.py; git commit -qam swc; git checkout -q main
out=$("$SELF" prepare session/swc main); eval "$out"
mk_rec "$INTEGRATION" "$(mk_codex SHIP-WITH-CHANGES swc)"
rc=0; "$SELF" commit session/swc main "$BASE" "$INTEGRATION" "$WORKTREE" >/dev/null 2>&1 || rc=$?
check "interlock: codex SWC refused exit 8" "[ $rc -eq 8 ]"
"$SELF" abort "$WORKTREE" cleanup >/dev/null 2>&1 || true

# 17. wrong-SHA record does NOT authorize this integ -> exit 8
git checkout -q -b session/mv main; echo "print('mv')">>src/a.py; git commit -qam mv; git checkout -q main
out=$("$SELF" prepare session/mv main); eval "$out"
mk_rec deadbeefdeadbeefdeadbeefdeadbeefdeadbeef "$(mk_codex SHIP mv)"
rc=0; "$SELF" commit session/mv main "$BASE" "$INTEGRATION" "$WORKTREE" >/dev/null 2>&1 || rc=$?
check "interlock: wrong-SHA record -> exit 8" "[ $rc -eq 8 ]"
"$SELF" abort "$WORKTREE" cleanup >/dev/null 2>&1 || true

# 17b. bogus integ (not a real OID) -> exit 8 even under override
git checkout -q -b session/bogus main; echo "print('bogus')">>src/a.py; git commit -qam bogus; git checkout -q main
out=$("$SELF" prepare session/bogus main); eval "$out"
rc=0; LANDER_HUMAN_OVERRIDE=1 "$SELF" commit session/bogus main "$BASE" 0000000000000000000000000000000000000000 "$WORKTREE" >/dev/null 2>&1 || rc=$?
check "interlock: bogus integ refused exit 8 (even override)" "[ $rc -eq 8 ]"
"$SELF" abort "$WORKTREE" cleanup >/dev/null 2>&1 || true

# 18. HIGH risk + SHIP + record, no override -> exit 7 (refused commit's trap removes the worktree)
git checkout -q -b session/hi main; mkdir -p src/secrets; echo x=1>src/secrets/k.py; git add src/secrets/k.py; git commit -qam hi; git checkout -q main
out=$("$SELF" prepare session/hi main); eval "$out"
mk_rec "$INTEGRATION" "$(mk_codex SHIP hi)"
rc=0; "$SELF" commit session/hi main "$BASE" "$INTEGRATION" "$WORKTREE" >/dev/null 2>&1 || rc=$?
check "interlock: HIGH without override refused exit 7" "[ $rc -eq 7 ]"

# 19. THE SPLIT: a risk waiver alone must NOT waive the codex verdict.
# Advance the branch first - re-preparing an UNCHANGED tree within the same second yields an
# IDENTICAL merge sha (same parents/tree/message/author), so case 18's record would still match and
# this case would silently pass for the wrong reason.
git checkout -q session/hi; echo x=9>>src/secrets/k.py; git commit -qam hi-again; git checkout -q main
out=$("$SELF" prepare session/hi main); eval "$out"
before19=$(git rev-parse main)
check "split: re-prepare produced a NEW integration sha" "[ ! -f '$D/.claude/state/land-verdicts/$INTEGRATION.rec' ]"
rc=0; LANDER_HUMAN_OVERRIDE=1 "$SELF" commit session/hi main "$BASE" "$INTEGRATION" "$WORKTREE" >/dev/null 2>&1 || rc=$?
check "split: risk waiver alone does NOT waive verdict (exit 8)" "[ $rc -eq 8 ]"
check "split: target untouched when verdict missing" "[ \"$(git rev-parse main)\" = \"$before19\" ]"
# exit 8 is shared with the integ/worktree validations above, so the code alone does not prove the
# missing-verdict branch ran. Assert that branch's own log line.
check "split: verdict branch actually ran (no-record logged)" "grep -q 'land-verdict-block	.*session/hi no-record' '$D/.claude/state/verify.log'"

# 19b. risk waiver + a real recorded SHIP -> commits (the intended override path).
# Re-prepare: case 19's die fired cmd_commit's EXIT trap, which removes the throwaway worktree, so
# the previous $WORKTREE is gone and the integ-vs-worktree-HEAD check would fail first.
out=$("$SELF" prepare session/hi main); eval "$out"
mk_rec "$INTEGRATION" "$(mk_codex SHIP hi)"
rc=0; LANDER_HUMAN_OVERRIDE=1 "$SELF" commit session/hi main "$BASE" "$INTEGRATION" "$WORKTREE" >/dev/null 2>&1 || rc=$?
check "interlock: HIGH with risk waiver + SHIP commits" "[ $rc -eq 0 ]"
check "interlock: override advanced main" "[ \"$(git rev-parse main)\" = '$INTEGRATION' ]"

# 19c. explicit verdict waiver lands over a MISSING record, and logs under its own tag.
# Fresh branch: session/hi is already merged into main by 19b.
git checkout -q -b session/hi2 main; echo x=2>>src/secrets/k.py; git commit -qam hi2; git checkout -q main
out=$("$SELF" prepare session/hi2 main); eval "$out"
rc=0; LANDER_HUMAN_OVERRIDE=1 LANDER_VERDICT_OVERRIDE=1 "$SELF" commit session/hi2 main "$BASE" "$INTEGRATION" "$WORKTREE" >/dev/null 2>&1 || rc=$?
check "split: explicit verdict waiver lands" "[ $rc -eq 0 ]"
# Scoped to this candidate: an unscoped grep would match a land-verdict-override from an earlier case.
check "split: verdict waiver logged under its own tag" "grep -q 'land-verdict-override	.*session/hi2' '$D/.claude/state/verify.log'"

# ---- Task 3: LANDER_RISK_EXTRA_FLAGS forwarding (uses the REAL classifier) ---------------------
SELF_CLASSIFIER="$(cd "$(dirname "$SELF")" && pwd)/risk_classify.py"
# 20. forwarded --deny-substr flips a benign src change LOW->HIGH
git checkout -q -b session/xf main; echo "print('xf')">>src/a.py; git commit -qam xf; git checkout -q main
out=$(LANDER_RISK_CLASSIFY="$SELF_CLASSIFIER" LANDER_RISK_EXTRA_FLAGS="--deny-substr a.py" "$SELF" prepare session/xf main); eval "$out"
check "extra-flags: --deny-substr flips LOW->HIGH" "[ '$RISK' = HIGH ]"
"$SELF" abort "$WORKTREE" cleanup >/dev/null 2>&1 || true
# 20b. glob value stays literal (set -f) -> HIGH
git checkout -q -b session/gx main; echo "print('gx')">>src/a.py; git commit -qam gx; git checkout -q main
out=$(LANDER_RISK_CLASSIFY="$SELF_CLASSIFIER" LANDER_RISK_EXTRA_FLAGS="--deny 'src/**'" "$SELF" prepare session/gx main); eval "$out"
check "extra-flags: glob value literal (set -f) -> HIGH" "[ '$RISK' = HIGH ]"
"$SELF" abort "$WORKTREE" cleanup >/dev/null 2>&1 || true
# 20c. malformed value fails CLOSED to HIGH
git checkout -q -b session/bad main; echo "print('bad')">>src/a.py; git commit -qam bad; git checkout -q main
out=$(LANDER_RISK_CLASSIFY="$SELF_CLASSIFIER" LANDER_RISK_EXTRA_FLAGS="--deny 'unterminated" "$SELF" prepare session/bad main); eval "$out"
check "extra-flags: malformed value fails closed -> HIGH" "[ '$RISK' = HIGH ]"
"$SELF" abort "$WORKTREE" cleanup >/dev/null 2>&1 || true

# ---- Task 4: merge INTERACTION SURFACE (OVERLAP) ----------------------------------------------
# 21. target has NOT moved since the fork -> nothing to interact with -> OVERLAP=0
git checkout -q main
git checkout -q -b session/ov0 main; echo "print('ov0')">>src/a.py; git commit -qam ov0; git checkout -q main
out=$("$SELF" prepare session/ov0 main); eval "$out"
check "overlap: unmoved target -> 0" "[ '$OVERLAP' = 0 ]"
"$SELF" abort "$WORKTREE" cleanup >/dev/null 2>&1 || true

# 22. target moved in a DISJOINT file -> still no shared file -> OVERLAP=0
git checkout -q -b session/ovd main; echo "print('ovd')">>src/a.py; git commit -qam ovd
git checkout -q main; echo "print('side')" > src/side.py; git add src/side.py; git commit -qam side
out=$("$SELF" prepare session/ovd main); eval "$out"
check "overlap: disjoint target move -> 0" "[ '$OVERLAP' = 0 ]"
"$SELF" abort "$WORKTREE" cleanup >/dev/null 2>&1 || true

# 23. target moved in the SAME file (non-conflicting region) -> OVERLAP=1, file named.
# src/wide.py gets a long body so the two edits land far apart and merge cleanly.
git checkout -q main
{ echo "# wide"; i=0; while [ $i -lt 40 ]; do echo "x$i = $i"; i=$((i+1)); done; } > src/wide.py
git add src/wide.py; git commit -qam wide
git checkout -q -b session/ovs main
{ echo "# TOP EDIT (branch)"; cat src/wide.py; } > src/wide.py.t && mv src/wide.py.t src/wide.py
git commit -qam ovs-top
git checkout -q main; echo "# BOTTOM EDIT (target)" >> src/wide.py; git commit -qam ovs-bottom
out=$("$SELF" prepare session/ovs main); eval "$out"
check "overlap: same-file target move -> 1" "[ '$OVERLAP' = 1 ]"
check "overlap: names the shared file" "case \"\$OVERLAP_FILES\" in *src/wide.py*) true;; *) false;; esac"
"$SELF" abort "$WORKTREE" cleanup >/dev/null 2>&1 || true

# 24. multi-file OVERLAP_FILES survives the caller's `eval` (unquoted would run the 2nd path).
# Two long files; branch edits the TOP of each, target edits the BOTTOM -> shared files, no conflict.
git checkout -q main
for f in src/m1.py src/m2.py; do
  { echo "# $f"; i=0; while [ $i -lt 40 ]; do echo "y$i = $i"; i=$((i+1)); done; } > "$f"
done
git add src/m1.py src/m2.py; git commit -qam multi
git checkout -q -b session/ovm main
for f in src/m1.py src/m2.py; do
  { echo "# TOP (branch)"; cat "$f"; } > "$f.t" && mv "$f.t" "$f"
done
git commit -qam ovm-branch
git checkout -q main
echo "# BOTTOM (target)" >> src/m1.py; echo "# BOTTOM (target)" >> src/m2.py; git commit -qam ovm-target
out=$("$SELF" prepare session/ovm main)
rc=0; eval "$out" || rc=$?
check "overlap: multi-file block evals cleanly" "[ $rc -eq 0 ]"
check "overlap: multi-file count = 2" "[ '$OVERLAP' = 2 ]"
"$SELF" abort "$WORKTREE" cleanup >/dev/null 2>&1 || true

# 25. FAIL-CLOSED: an unresolvable ref makes overlap_of print `unknown`, never an empty/0 overlap
check "overlap: bad ref fails closed to unknown" \
  "[ \"\$(\"$SELF\" _overlap deadbeefdeadbeefdeadbeefdeadbeefdeadbeef main 2>/dev/null)\" = unknown ]"
# 26. unrelated histories have no merge-base -> unknown (NOT 'no shared files')
git checkout -q --orphan session/orphan
git rm -rqf . >/dev/null 2>&1 || true
mkdir -p src; echo "print('orphan')" > src/o.py; git add src/o.py; git commit -qam orphan
git checkout -q main
check "overlap: unrelated histories -> unknown" \
  "[ \"\$(\"$SELF\" _overlap main session/orphan 2>/dev/null)\" = unknown ]"

echo "lander selftest: $pass passed, $fail failed"
[ $fail -eq 0 ]
EOSELF
  local rc=$?
  rm -rf "$d"
  return $rc
}

case "${1:-}" in
  prepare) shift; cmd_prepare "$@" ;;
  commit)  shift; cmd_commit  "$@" ;;
  abort)   shift; cmd_abort   "$@" ;;
  _overlap) shift; overlap_of "$@" ;;   # introspection only (selftest covers the fail-closed path)
  --selftest) selftest ;;
  *) echo "usage: lander.sh prepare <candidate> [target] | commit <cand> <target> <base> <integ> <wt> | abort <wt> [reason] | --selftest" >&2; exit 2 ;;
esac
