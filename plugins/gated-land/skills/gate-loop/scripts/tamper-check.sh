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
    # THE GATE MACHINERY ITSELF. Everything above protects evidence the gate READS; these are the
    # things that RENDER THE VERDICT, and until 2026-08-09 none of them was substrate. A repo that
    # vendors them (or the plugin's own repo) could edit an interlock and self-approve the change
    # through the interlock it just weakened. Not hypothetical: on 2026-08-07 three successive edits
    # to `_verdict_ok` each introduced a distinct FAIL-OPEN - one of them read `VERDICT: DO NOT SHIP`
    # as approval - and this guard returned 0 (clean) on every one of them.
    lander.sh|*/lander.sh)                             return 0 ;;  # merge queue + verdict/risk interlocks
    tamper-check.sh|*/tamper-check.sh)                 return 0 ;;  # THIS guard - it must guard itself
    risk_classify.py|*/risk_classify.py)               return 0 ;;  # the static risk gate
    # ...and the CONTROL PLANE, which in an LLM-driven pipeline is prose. These four SKILL.md files
    # are what tell the agent to run the tamper check at all, to stop on exit 3, and to require a
    # codex SHIP. Weaken those lines and every coded brake above is still intact but never invoked,
    # and this guard reports clean because no guarded CODE path was touched - the same self-approval
    # class, one level up (codex, [high]). "Prose, not code" is the wrong axis; "does it decide
    # whether the gate runs and what clears it" is the right one.
    # Scoped to the four gate skills ON PURPOSE: guarding every SKILL.md would trip on ordinary
    # skill edits and train people to wave the guard through.
    gate-loop/SKILL.md|*/gate-loop/SKILL.md)           return 0 ;;
    land/SKILL.md|*/land/SKILL.md)                     return 0 ;;
    ship/SKILL.md|*/ship/SKILL.md)                     return 0 ;;
    review-gate/SKILL.md|*/review-gate/SKILL.md)       return 0 ;;
  esac
  # Repo-specific substrate a self-driving session could edit to force a green run (an in-repo
  # risk/policy classifier, a coverage gate, etc.) that the built-in patterns above don't cover: set
  # TAMPER_EXTRA_SUBSTR in .dev-loop.conf to a space-separated list of substrings to additionally guard.
  local p
  for p in ${TAMPER_EXTRA_SUBSTR:-}; do
    case "$1" in *"$p"*) return 0 ;; esac
  done
  return 1
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
      .dev-loop.conf sub/.dev-loop.conf \
      lander.sh scripts/lander.sh plugins/gated-land/engine/lander.sh \
      tamper-check.sh skills/gate-loop/scripts/tamper-check.sh \
      risk_classify.py engine/risk_classify.py src/cuj_loop/risk_classify.py \
      .agents/skills/gate-loop/SKILL.md plugins/gated-land/skills/gate-loop/SKILL.md \
      plugins/gated-land/skills/land/SKILL.md plugins/gated-land/skills/ship/SKILL.md \
      plugins/dev-loop-core/skills/review-gate/SKILL.md; do
    is_guarded "$p" || { echo "FAIL: expected GUARDED: $p"; fails=$((fails+1)); }
  done
  # clean paths - note the edge cases that must NOT trip the guard
  for p in \
      src/app/main.py docs/x.md README.md \
      testdata/foo.py src/mytests.py verify_thing.py my_pyproject.toml \
      src/lib/util.py docs/dev-loop.conf.md \
      docs/lander.sh.md src/mylander.shim.py notes/tamper-check.md \
      .agents/skills/research/SKILL.md .agents/skills/code-review/SKILL.md \
      docs/land/SKILL.md.draft skills/landing-page/SKILL.md; do
    is_guarded "$p" && { echo "FAIL: expected CLEAN: $p"; fails=$((fails+1)); }
  done
  # fail-closed: an unresolvable base must return TAMPER - and EXACTLY 3, not merely non-zero. A
  # test that accepts any non-zero would pass on exit 1 or 2, which callers do not treat as tamper.
  local rc=0
  tamper_check __definitely_no_such_ref__ HEAD 2>/dev/null || rc=$?
  [ "$rc" -eq 3 ] || { echo "FAIL: bad-base should return exactly 3, got $rc"; fails=$((fails+1)); }
  # TAMPER_EXTRA_SUBSTR: repo-specific substrate guard, additive to the built-ins above
  local TAMPER_EXTRA_SUBSTR="src/cuj_loop/classify.py"
  is_guarded "src/cuj_loop/classify.py" || { echo "FAIL: expected GUARDED via TAMPER_EXTRA_SUBSTR"; fails=$((fails+1)); }
  is_guarded "src/other/file.py" && { echo "FAIL: TAMPER_EXTRA_SUBSTR over-matched an unrelated path"; fails=$((fails+1)); }
  if ((fails)); then echo "tamper-check selftest: $fails FAILED"; return 1; fi
  echo "tamper-check selftest: OK"; return 0
}

case "${1:-}" in
  --selftest) selftest ;;
  ""|-h|--help) echo "usage: tamper-check.sh <base> [candidate] | --selftest" >&2; exit 2 ;;
  *) tamper_check "$@" ;;
esac
