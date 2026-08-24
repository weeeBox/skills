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
# Test SOURCE, split out from the substrate list below so `is_test_source_only` can be DERIVED
# from that list instead of restating it. Restating it is not a style preference: a duplicated
# copy re-introduced the basename-vs-path bug the scoping comments below already warn about
# (`*gate.py` matches `test_renewal_claim_gate.py`), and it would silently rot every time a new
# substrate rule is added to only one of the two lists.
is_test_path() {
  case "$1" in tests/*|*/tests/*) return 0 ;; esac
  return 1
}

# Everything that is substrate for a reason OTHER than being test source.
is_substrate() {
  case "$1" in
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

is_guarded() { is_test_path "$1" || is_substrate "$1"; }

# Test source that is NOT also substrate for another reason (tests/conftest.py and
# tests/run_all.sh are, and keep the path-level rule). This is the one guarded class where
# "was it touched?" is the wrong question.
#
# Measured 2026-08-24 by replaying all 65 historical gateloop-tamper events against reconstructed
# fork points: the path-level rule fired on 60 of 60 usable episodes, and the logged details argue
# the case for the defence ("NEW file, no existing test modified", "3 tests ADDED, none
# modified/removed"). In a repo whose CLAUDE.md mandates red-repro-first TDD, "the diff touches
# tests/" describes every branch, not a threat: the guard was 100% precise against its own rule and
# near-0% against tampering, and a guard that fires on routine work stops being read - the exact
# erosion the gate.py scoping comment above was written about. Rescoped to DELETED-or-WEAKENED it
# fires on 5 of the same 60: 4 are the machinery rules (lander.sh, risk_classify.py, tamper-check.sh)
# firing exactly as designed, and 2 episodes carry 5 genuine test-evidence regressions - including
# tests/memory/test_preference_write_receipt.py dropping 27 assertions to 25, which the old rule
# reported with the same weight as every additive branch and therefore buried.
is_test_source_only() { is_test_path "$1" && ! is_substrate "$1"; }

# Exit 0 (= this test file REMOVED or WEAKENED evidence, treat as tamper) / 1 (= clean).
# FAIL CLOSED on any git error, same contract as tamper_check itself.
test_weakened() {
  local st="$1" old="$2" new="$3" mb="$4" cand="$5" a b ca cb
  case "$st" in
    A*) return 1 ;;   # net-new test file: adds evidence, cannot fake a green run
    D*) return 0 ;;   # deleted test file: always tamper
  esac
  a="$(git show "$mb:$old" 2>/dev/null)"   || return 0
  b="$(git show "$cand:$new" 2>/dev/null)" || return 0
  # grep -c counts LINES, symmetric on both sides; `|| true` because grep exits 1 on zero matches.
  # Anchored on statement SHAPE, not the bare word: a plain /assert/ also counted PROSE, so deleting
  # one comment reading "...is what is asserted" registered as a weakened test (measured false
  # positive on tests/eldercare_verify/test_replay_ui_browser.py: 446->445 loose, 305->305 anchored).
  ca="$(printf '%s\n' "$a" | grep -cE '^[[:space:]]*assert[[:space:](]|self\.assert|pytest\.raises|^[[:space:]]*(async )?def test_' || true)"
  cb="$(printf '%s\n' "$b" | grep -cE '^[[:space:]]*assert[[:space:](]|self\.assert|pytest\.raises|^[[:space:]]*(async )?def test_' || true)"
  [ "${cb:-0}" -lt "${ca:-0}" ]
}

tamper_check() {
  local base="$1" cand="${2:-HEAD}" hits=() f old st line rest mb diff_out
  # three-dot: what THIS branch introduced since it forked from base (matches
  # ship/review-gate's `git diff <base>...HEAD` scoping - ignores base-side drift so a
  # test that landed on main after the fork is never mistaken for tampering here).
  # FAIL CLOSED: a safety guard must never report "clean" when it could not actually check. A
  # bad base/candidate (or any git error) returns TAMPER, not a false all-clear (codex finding).
  if ! diff_out="$(git diff --name-status "$base...$cand" 2>&1)"; then
    printf 'TAMPER-CHECK ERROR: `git diff --name-status %s...%s` failed - failing CLOSED:\n%s\n' \
      "$base" "$cand" "$diff_out" >&2
    return 3
  fi
  # Content comparison needs the fork point, not the base tip, to match the three-dot diff above.
  if ! mb="$(git merge-base "$base" "$cand" 2>&1)"; then
    printf 'TAMPER-CHECK ERROR: `git merge-base %s %s` failed - failing CLOSED:\n%s\n' \
      "$base" "$cand" "$mb" >&2
    return 3
  fi
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    st="${line%%	*}"; rest="${line#*	}"
    case "$st" in
      R*|C*) old="${rest%%	*}"; f="${rest#*	}" ;;   # rename/copy: <status>\t<old>\t<new>
      *)     f="$rest"; old="$f" ;;
    esac
    is_guarded "$f" || continue
    if is_test_source_only "$f"; then
      # Test SOURCE is judged on whether it lost evidence, not on whether it was touched.
      test_weakened "$st" "$old" "$f" "$mb" "$cand" || continue
      hits+=("$f (test evidence removed or weakened)")
    else
      hits+=("$f")
    fi
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
  # is_test_source_only: test source is content-judged; test-shaped substrate is not. The
  # `test_*_gate.py` / `test_risk_classify.py` cases are regressions - a duplicated substrate list
  # with basename globs (`*gate.py`) swallowed them, which is why the predicate derives from
  # is_substrate instead.
  for p in tests/test_foo.py tests/app/test_bar.py src/tests/x.py \
           tests/test_renewal_claim_gate.py tests/test_risk_classify.py tests/test_gate.py; do
    is_test_source_only "$p" || { echo "FAIL: expected TEST-SOURCE-ONLY: $p"; fails=$((fails+1)); }
  done
  for p in tests/conftest.py tests/run_all.sh tests/requirements.txt \
           src/app/main.py scripts/lander.sh; do
    is_test_source_only "$p" && { echo "FAIL: expected NOT test-source-only: $p"; fails=$((fails+1)); }
  done
  # TAMPER_EXTRA_SUBSTR keeps its path-level force even under tests/
  ( local TAMPER_EXTRA_SUBSTR="tests/golden/"
    is_test_source_only "tests/golden/fixture.py" ) && \
    { echo "FAIL: TAMPER_EXTRA_SUBSTR should force path-level under tests/"; fails=$((fails+1)); }
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
