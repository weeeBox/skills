#!/usr/bin/env bash
# review-gate per-worktree lock + reconnect state. Best-effort advisory backstop (NOT a hard
# distributed mutex) + the reconnect/crash-recovery record. See the review-gate SKILL.md and
# docs/plans/2026-07-11-review-gate-skill.md.
#
# Subcommands:
#   acquire "<SCOPE_CMD>"          -> prints one of: ACQUIRED | RECONNECT <c> <a> <round> |
#                                     RECLAIM <c> <a> | BLOCKED <owner>   (exit 0; exit 3 = not a git repo)
#   record  <codex_job> <agy_job> <round>   (after a validated fresh dispatch; preserves bootstrap diff_hash)
#   heartbeat                      -> refresh lock mtime so "stale" means genuinely dead
#   release                        -> remove the lock (idempotent)
#   --selftest
#
# Owner is the Claude session id ONLY (no faked durable owner). Empty owner => never auto-reconnect
# => BLOCKED (fail closed). Reconnect/reclaim-mine is safe because a single Claude session runs
# sequentially (no same-owner concurrent gate). Residual: two DIFFERENT sessions in one checkout
# hitting the sub-second bootstrap window is a documented v1 limitation (one gate per worktree).
set -uo pipefail

WINDOW=${REVIEW_GATE_LOCK_WINDOW:-1800}
mtime(){ stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0; }
val(){ sed -n "s/^$1=//p" "$2" 2>/dev/null; }   # parse a key from the state file (never `source`)

_paths(){
  gitdir=$(git rev-parse --absolute-git-dir 2>/dev/null) || { echo "not a git repo" >&2; exit 3; }
  lockdir="$gitdir/review-gate.lock.d"; state="$lockdir/state"
  owner="${CLAUDE_SESSION_ID:-}"
}

_write_state(){ # atomic: owner diff_hash codex_job agy_job round
  printf 'owner=%s\ndiff_hash=%s\ncodex_job=%s\nagy_job=%s\nround=%s\nts=%s\n' \
    "$1" "$2" "$3" "$4" "$5" "$(date +%s)" > "$state.tmp" && mv "$state.tmp" "$state"
}

cmd_acquire(){
  _paths
  local scope_cmd="${1:-}" out rc
  out=$(eval "$scope_cmd"); rc=$?                # do NOT suppress scope errors (codex#3)
  [ "$rc" -eq 0 ] || { echo "scope command failed (rc=$rc): $scope_cmd" >&2; exit 4; }
  local dh; dh=$(printf '%s' "$out" | git hash-object --stdin)
  if mkdir "$lockdir" 2>/dev/null; then
    _write_state "$owner" "$dh" "" "" ""      # bootstrap immediately (no empty-state window)
    echo "ACQUIRED"; return 0
  fi
  # lockdir exists -> decide. mtime target falls back to lockdir while state not yet written.
  local t; [ -f "$state" ] && t="$state" || t="$lockdir"
  local age=$(( $(date +%s) - $(mtime "$t") ))
  local sowner sdh sc sa sr
  sowner=$(val owner "$state"); sdh=$(val diff_hash "$state")
  sc=$(val codex_job "$state"); sa=$(val agy_job "$state"); sr=$(val round "$state")
  if [ "$age" -ge "$WINDOW" ]; then            # stale (heartbeat lapsed) -> dead. ATOMIC steal (codex#1):
    local corpse="$lockdir.dead.$$"            # only ONE concurrent reclaimer wins the rename
    if mv "$lockdir" "$corpse" 2>/dev/null; then
      rm -rf "$corpse"; echo "RECLAIM $sc $sa"; return 0
    fi
    echo "BLOCKED ${sowner:-?}"; return 0       # lost the steal race -> BLOCKED (fail closed; caller STOPs, does not retry)
  fi
  if [ -n "$owner" ] && [ "$sowner" = "$owner" ]; then   # confirmably mine (single session => sequential)
    if [ "$sdh" = "$dh" ] && [ -n "$sc" ] && [ -n "$sa" ]; then
      echo "RECONNECT $sc $sa $sr"; return 0    # my in-flight gate, same code
    fi
    echo "RECLAIM $sc $sa"; rm -rf "$lockdir"; return 0   # my crashed half-dispatch, or code changed
  fi
  echo "BLOCKED ${sowner:-?}"; return 0          # different / empty / unreadable owner -> fail closed
}

cmd_record(){
  _paths
  local c="${1:-}" a="${2:-}" r="${3:-}"
  [ -n "$c" ] && [ -n "$a" ] || { echo "record: empty job id" >&2; return 1; }
  local so=""; [ -f "$state" ] && so=$(val owner "$state")   # owner-guarded (codex): only the owner records,
  [ -z "$so" ] || [ "$so" = "$owner" ] || {                  # else a non-owner could hijack the owner field
    echo "record: lock owned by '$so', not me ('$owner') - refusing" >&2; return 1; }
  local sdh; sdh=$(val diff_hash "$state")       # PRESERVE bootstrap hash, never recompute
  _write_state "$owner" "$sdh" "$c" "$a" "$r"
}

cmd_heartbeat(){ _paths; [ -f "$state" ] && touch "$state" || { [ -d "$lockdir" ] && touch "$lockdir"; }; }
cmd_release(){ _paths                            # only release MY lock (codex#2: a BLOCKED non-owner must not free the holder's)
  local so=""; [ -f "$state" ] && so=$(val owner "$state")
  if [ ! -d "$lockdir" ] || [ -z "$so" ] || [ "$so" = "$owner" ]; then rm -rf "$lockdir"
  else echo "release: lock owned by '$so', not me ('$owner') - NOT releasing" >&2; return 0; fi
}

# ---------------- selftest ----------------
if [ "${1:-}" = "--selftest" ]; then
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  ( cd "$tmp" && git init -q && git config user.email t@t && git config user.name t \
      && printf 'a\n' > f && git add f )
  S="$0"; SC="git -C $tmp diff --cached"        # scope cmd for the temp repo
  run(){ ( cd "$tmp" && env "$@" ); }           # run with env overrides, in the repo
  fail(){ echo "FAIL: $1"; exit 1; }

  # 1. non-git dir -> exit 3
  ( cd "$(mktemp -d)" && "$S" acquire "true" ) >/dev/null 2>&1; [ $? -eq 3 ] || fail "non-git should exit 3"
  echo "PASS: non-git -> exit 3"

  # 2. first acquire -> ACQUIRED (+ bootstrap state written)
  o=$(run CLAUDE_SESSION_ID=A "$S" acquire "$SC"); [ "$o" = "ACQUIRED" ] || fail "first acquire ($o)"
  [ -f "$tmp/.git/review-gate.lock.d/state" ] || fail "no bootstrap state"
  echo "PASS: first acquire -> ACQUIRED + bootstrap"

  # 3. half-dispatched (no record), DIFFERENT owner -> BLOCKED, not RECLAIM (no double dispatch)
  o=$(run CLAUDE_SESSION_ID=B "$S" acquire "$SC"); [ "${o%% *}" = "BLOCKED" ] || fail "other-owner half-dispatch should BLOCK ($o)"
  echo "PASS: other-owner fresh half-dispatch -> BLOCKED"

  # 4. half-dispatched, SAME owner -> RECLAIM (empty ids) and lockdir removed
  o=$(run CLAUDE_SESSION_ID=A "$S" acquire "$SC"); [ "${o%% *}" = "RECLAIM" ] || fail "same-owner half-dispatch RECLAIM ($o)"
  [ -d "$tmp/.git/review-gate.lock.d" ] && fail "RECLAIM must remove lockdir"
  echo "PASS: same-owner half-dispatch -> RECLAIM + lockdir removed"

  # 5. acquire -> record -> acquire same session, unchanged diff -> RECONNECT with ids
  run CLAUDE_SESSION_ID=A "$S" acquire "$SC" >/dev/null
  run CLAUDE_SESSION_ID=A "$S" record cx-1 ay-1 1 >/dev/null
  bh=$(val diff_hash "$tmp/.git/review-gate.lock.d/state")
  o=$(run CLAUDE_SESSION_ID=A "$S" acquire "$SC"); [ "$o" = "RECONNECT cx-1 ay-1 1" ] || fail "reconnect ($o)"
  echo "PASS: same session + unchanged diff -> RECONNECT cx-1 ay-1 1"

  # 6. record preserved the bootstrap diff_hash (didn't recompute)
  ah=$(val diff_hash "$tmp/.git/review-gate.lock.d/state"); [ "$bh" = "$ah" ] || fail "record changed diff_hash"
  echo "PASS: record preserves bootstrap diff_hash"

  # 7. change the staged diff -> acquire same session -> RECLAIM (stale code not trusted)
  ( cd "$tmp" && printf 'a\nb\n' > f && git add f )
  o=$(run CLAUDE_SESSION_ID=A "$S" acquire "$SC"); [ "${o%% *}" = "RECLAIM" ] || fail "changed-diff RECLAIM ($o)"
  echo "PASS: changed diff -> RECLAIM (stale code not trusted)"

  # 8. NO session id -> acquire/record/acquire -> BLOCKED (no unsafe reconnect)
  run "$S" acquire "$SC" >/dev/null                       # CLAUDE_SESSION_ID unset in this env
  run "$S" record cx ay 1 >/dev/null
  o=$(run "$S" acquire "$SC"); [ "${o%% *}" = "BLOCKED" ] || fail "no-session should BLOCK ($o)"
  run "$S" release
  echo "PASS: no CLAUDE_SESSION_ID -> BLOCKED (degrade to fail-closed)"

  # 9. stale lock (backdate state mtime) -> RECLAIM regardless of owner
  run CLAUDE_SESSION_ID=A "$S" acquire "$SC" >/dev/null
  run CLAUDE_SESSION_ID=A "$S" record cx-2 ay-2 2 >/dev/null
  touch -t 200001010000 "$tmp/.git/review-gate.lock.d/state"
  o=$(run CLAUDE_SESSION_ID=Z "$S" acquire "$SC"); [ "$o" = "RECLAIM cx-2 ay-2" ] || fail "stale RECLAIM ($o)"
  [ -d "$tmp/.git/review-gate.lock.d" ] && fail "stale RECLAIM must remove lockdir"
  echo "PASS: stale lock -> RECLAIM cx-2 ay-2 + removed"

  # 10. after RECLAIM removes the lock, re-acquire -> ACQUIRED (no loop)
  o=$(run CLAUDE_SESSION_ID=Z "$S" acquire "$SC"); [ "$o" = "ACQUIRED" ] || fail "re-acquire after reclaim ($o)"
  echo "PASS: re-acquire after RECLAIM -> ACQUIRED (no loop)"

  # 11. heartbeat refreshes mtime so a slow gate is not stale-reclaimed
  touch -t 200001010000 "$tmp/.git/review-gate.lock.d/state"
  run CLAUDE_SESSION_ID=Z "$S" heartbeat
  age=$(( $(date +%s) - $(mtime "$tmp/.git/review-gate.lock.d/state") ))
  [ "$age" -lt 60 ] || fail "heartbeat did not refresh mtime (age=$age)"
  run CLAUDE_SESSION_ID=Z "$S" release
  echo "PASS: heartbeat refreshes mtime (stale=dead, not slow)"

  # 12. owner-guarded release: a DIFFERENT owner cannot free the holder's lock (codex#2)
  run CLAUDE_SESSION_ID=A "$S" acquire "$SC" >/dev/null
  run CLAUDE_SESSION_ID=A "$S" record cx ay 1 >/dev/null
  run CLAUDE_SESSION_ID=B "$S" release >/dev/null 2>&1
  [ -d "$tmp/.git/review-gate.lock.d" ] || fail "non-owner release removed the holder's lock"
  run CLAUDE_SESSION_ID=A "$S" release
  [ -d "$tmp/.git/review-gate.lock.d" ] && fail "owner release did not remove the lock"
  echo "PASS: release is owner-guarded (non-owner can't free the holder's lock)"

  # 13. scope command that ERRORS -> exit 4 (not a bogus empty hash) (codex#3)
  run CLAUDE_SESSION_ID=A "$S" acquire "git -C /no/such/repo diff" >/dev/null 2>&1
  [ $? -eq 4 ] || fail "scope error should exit 4, not hash empty"
  echo "PASS: scope command error -> exit 4 (fail closed, no empty-hash reconnect)"

  # 14. record is owner-guarded: a NON-owner cannot overwrite the owner field (codex round-2 #1)
  run CLAUDE_SESSION_ID=A "$S" acquire "$SC" >/dev/null
  run CLAUDE_SESSION_ID=B "$S" record cx ay 1 >/dev/null 2>&1 && fail "non-owner record should be refused"
  so=$(val owner "$tmp/.git/review-gate.lock.d/state"); [ "$so" = "A" ] || fail "non-owner overwrote owner ($so)"
  run CLAUDE_SESSION_ID=A "$S" release
  echo "PASS: record is owner-guarded (non-owner cannot hijack the owner field)"

  echo "ALL PASS"; exit 0
fi

# ---------------- dispatch ----------------
sub="${1:-}"; shift || true
case "$sub" in
  acquire)   cmd_acquire "$@" ;;
  record)    cmd_record "$@" ;;
  heartbeat) cmd_heartbeat ;;
  release)   cmd_release ;;
  *) echo "usage: review-gate-lock.sh acquire|record|heartbeat|release|--selftest" >&2; exit 2 ;;
esac
