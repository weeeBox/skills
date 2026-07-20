#!/usr/bin/env bash
# tamper-check.sh — the gate-loop test-tamper guard (safety-critical, deterministic).
#
# An autonomous fixer must not "go green" by editing the test/verification substrate:
# the tests themselves, their config, the deps they run against, or the verify scripts.
# This is CODED (not prose in the skill) so the loop physically cannot skip it, and it
# runs over the whole branch diff every round (durable — a fix that tampers on round 2
# is still caught).
#
# Usage:   tamper-check.sh <base> [candidate]   # candidate defaults to HEAD
#          tamper-check.sh --selftest
# Exit:    0 = clean (no guarded path touched — self-approval allowed)
#          3 = TAMPER (guarded paths printed to stderr) — human/codex-only approval
#          2 = usage error
set -uo pipefail

# Matched against `git diff --name-only` output (repo-relative paths). A hit means the
# session touched something that could fake a green run, so it may not self-approve.
is_guarded() {
  case "$1" in
    tests/*|*/tests/*)                                 return 0 ;;  # any test source
    conftest.py|*/conftest.py)                         return 0 ;;  # pytest fixtures/config
    pytest.ini|*/pytest.ini|pyproject.toml|*/pyproject.toml) return 0 ;;
    requirements*.txt|*/requirements*.txt)             return 0 ;;  # deps (mock the world)
    verify.sh|*/verify.sh|run_all.sh|*/run_all.sh)     return 0 ;;  # common verify-script names
    .dev-loop.conf|*/.dev-loop.conf)                   return 0 ;;  # suite/base/classifier selector
    # Add your own verification substrate here — anything a self-driving session could edit to force a
    # green run: the suite selector (.dev-loop.conf, guarded above), an in-repo risk/policy classifier
    # if you vendor one (LANDER_RISK_CLASSIFY target), a coverage gate, etc.: pattern) return 0 ;;
    *)                                                 return 1 ;;
  esac
}

tamper_check() {
  local base="$1" cand="${2:-HEAD}" hits=() f diff_out
  # three-dot: what THIS branch introduced since it forked from base (matches
  # ship/review-gate's `git diff <base>...HEAD` scoping - ignores base-side drift so a
  # test that landed on main after the fork is never mistaken for tampering here).
  # FAIL CLOSED: a safety guard must never report "clean" when it could not actually check. A
  # bad base/candidate (or any git error) returns TAMPER, not a false all-clear (codex finding).
  if ! diff_out="$(git diff --name-only "$base...$cand" 2>&1)"; then
    printf 'TAMPER-CHECK ERROR: `git diff --name-only %s...%s` failed - failing CLOSED:\n%s\n' \
      "$base" "$cand" "$diff_out" >&2
    return 3
  fi
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    is_guarded "$f" && hits+=("$f")
  done <<< "$diff_out"
  if ((${#hits[@]})); then
    printf 'TAMPER: branch diff touches the verification substrate - human/codex-only approval:\n' >&2
    printf '  %s\n' "${hits[@]}" >&2
    return 3
  fi
  return 0
}

selftest() {
  local fails=0
  # guarded paths
  for p in \
      tests/test_foo.py src/tests/x.py conftest.py pkg/conftest.py \
      pytest.ini pyproject.toml a/pyproject.toml \
      requirements.txt requirements-dev.txt tools/requirements.txt \
      verify.sh .claude/hooks/verify.sh run_all.sh tests/run_all.sh \
      .dev-loop.conf sub/.dev-loop.conf; do
    is_guarded "$p" || { echo "FAIL: expected GUARDED: $p"; fails=$((fails+1)); }
  done
  # clean paths - note the edge cases that must NOT trip the guard
  for p in \
      src/app/main.py docs/x.md README.md \
      testdata/foo.py src/mytests.py verify_thing.py my_pyproject.toml \
      src/lib/util.py docs/dev-loop.conf.md; do
    is_guarded "$p" && { echo "FAIL: expected CLEAN: $p"; fails=$((fails+1)); }
  done
  # fail-closed: an unresolvable base must return TAMPER (3), never a false all-clear
  if tamper_check __definitely_no_such_ref__ HEAD 2>/dev/null; then
    echo "FAIL: bad-base tamper_check returned clean (should fail closed)"; fails=$((fails+1))
  fi
  if ((fails)); then echo "tamper-check selftest: $fails FAILED"; return 1; fi
  echo "tamper-check selftest: OK"; return 0
}

case "${1:-}" in
  --selftest) selftest ;;
  ""|-h|--help) echo "usage: tamper-check.sh <base> [candidate] | --selftest" >&2; exit 2 ;;
  *) tamper_check "$@" ;;
esac
