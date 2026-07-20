#!/usr/bin/env bash
# diff-size.sh <base> [head]  — one authoritative per-reviewer size disposition, so the gate stops
# reconciling three prose thresholds. BYTES=ERROR on a bad ref OR a failed `git diff` (e.g. no merge
# base), never a silent BYTES=0. Prints KEY=VALUE lines; exit 0 always (advisory to the skill).
#   diff-size.sh --selftest
set -uo pipefail
AGY_MAX=$((200*1024)); DS_MAX=$((1024*1024)); CODEX_WARN=$((50*1024))
disp() { # <bytes>
  local b="$1"
  printf 'BYTES=%s\n' "$b"
  [ "$b" -gt "$AGY_MAX" ]    && echo "AGY=OVERSIZE"      || echo "AGY=OK"
  [ "$b" -gt "$DS_MAX" ]     && echo "DEEPSEEK=OVERSIZE" || echo "DEEPSEEK=OK"
  [ "$b" -gt "$CODEX_WARN" ] && echo "CODEX=WARN"        || echo "CODEX=OK"
}
err() { echo "BYTES=ERROR"; echo "AGY=ERROR"; echo "DEEPSEEK=ERROR"; echo "CODEX=ERROR"; }
measure() { local base="$1" head="$2" tmp rc
  git rev-parse --verify -q "$base" >/dev/null 2>&1 || { err; return; }
  git rev-parse --verify -q "$head" >/dev/null 2>&1 || { err; return; }
  # write to a temp file (NOT $(...), which strips trailing newlines and undercounts near the -gt
  # boundary), check the diff exit, then count exact bytes. A failed diff (no merge base) -> ERROR.
  tmp="$(mktemp)"; git diff "$base...$head" > "$tmp" 2>/dev/null; rc=$?
  [ $rc -eq 0 ] || { rm -f "$tmp"; err; return; }
  disp "$(wc -c < "$tmp" | tr -d ' ')"; rm -f "$tmp"; }
selftest() {
  local f=0
  [ "$(disp 1000 | sed -n 's/AGY=//p')" = OK ] || { echo FAIL small; f=1; }
  [ "$(disp $((300*1024)) | sed -n 's/AGY=//p')" = OVERSIZE ] || { echo FAIL agy; f=1; }
  [ "$(disp $((60*1024)) | sed -n 's/CODEX=//p')" = WARN ] || { echo FAIL codex; f=1; }
  [ "$(disp $((2*1024*1024)) | sed -n 's/DEEPSEEK=//p')" = OVERSIZE ] || { echo FAIL ds; f=1; }
  [ "$(measure __no_such_ref__ HEAD | sed -n 's/BYTES=//p')" = ERROR ] || { echo FAIL bad-base; f=1; }
  [ $f -eq 0 ] && echo "diff-size selftest: OK" || { echo "diff-size selftest: FAILED"; return 1; }
}
case "${1:-}" in
  --selftest) selftest ;;
  "") echo "usage: diff-size.sh <base> [head] | --selftest" >&2; exit 2 ;;
  *) measure "$1" "${2:-HEAD}" ;;
esac
