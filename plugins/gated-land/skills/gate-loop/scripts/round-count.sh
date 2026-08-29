#!/usr/bin/env bash
# round-count.sh <verify.log> [branch]  — count this BRANCH's gateloop-block rows in the
# append-only log. Defaults to the current branch. Prints the integer; exit 0. >=3 = cap-out.
#
# rec:2026-08-23#3. It used to count rows after the LAST `gateloop-start`, which made the cap
# defeatable by starting a "fresh" loop: 2026-08-23 session 45d27e2c wrote
#   05:03 gateloop-capout (loop 1, round 3) / 05:16 gateloop-start "loop 2 ... fresh round
#   count" / 05:44 gateloop-capout (loop 2, round 3) / two further rounds with no start row
# — 7+ rounds and ~62 minutes on ONE branch, 3 past a cap it had logged itself, and the
# counter honestly returned 2. A boundary marker a later round can re-emit is not a cap.
# There is no start marker any more: identity is the branch, in field 5 of every row.
#
# FIELD-aware (awk -F '\t'), so an event name appearing in a DETAIL field can never be
# mistaken for a row of that type.
#
# FIELD POSITION IS NOT FIXED, so the branch is looked for in fields 3..NF rather than in $5.
# SKILL.md documented a 4-field row and sessions improvised a 5th, so both shapes are live in one
# log. Measured 2026-08-29 over a real ledger: of the gateloop rows carrying a session/ value, 62
# sat in field 5 and 9 in field 4. Pinning $5 undercounts — three rounds on one branch read as
# one — and an undercounting cap is a cap that can be exceeded, which is fail-OPEN and the exact
# defect class this counter exists to close.
#
# The match is EXACT against a whole field, never a prefix or substring, so a detail string that
# merely mentions another branch cannot be counted.
#
# ponytail: a reused branch name (a deleted and recreated session/<slug>) over-counts, which
# stops the loop EARLY — the safe direction.
set -uo pipefail

count() { local log="$1" br="$2"
  if [ ! -f "$log" ]; then echo 0; return; fi
  awk -F '\t' -v br="$br" '
    $2 == "gateloop-block" {
      for (i = 3; i <= NF; i++) if ($i == br) { c++; break }
    }
    END { print c+0 }' "$log"
}

selftest() {
  local d; d="$(mktemp -d)"
  local L="$d/v.log"
  local f=0
  row() { printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" >> "$L"; }
  want() { # want <label> <expected> <log> <branch>
    local got; got="$(count "$3" "$4")"
    if [ "$got" != "$2" ]; then echo "FAIL $1: count=$got want $2"; f=1; fi
  }

  # the 45d27e2c arc: a cap-out, a relabelled "fresh" loop, and rounds past both
  row 2026-08-23T04:31:00Z gateloop-block  aaa1 'r1 findings' session/mem-gaps
  row 2026-08-23T04:47:00Z gateloop-block  aaa2 'r2 findings' session/mem-gaps
  row 2026-08-23T05:03:00Z gateloop-block  aaa3 'r3 findings' session/mem-gaps
  row 2026-08-23T05:03:10Z gateloop-capout aaa3 'loop 1, round 3' session/mem-gaps
  row 2026-08-23T05:28:00Z gateloop-block  aaa4 'loop 2 ... fresh round count' session/mem-gaps
  row 2026-08-23T05:36:00Z gateloop-block  aaa5 'r2' session/mem-gaps
  row 2026-08-23T05:44:00Z gateloop-block  aaa6 'r3' session/mem-gaps
  row 2026-08-23T05:44:10Z gateloop-capout aaa6 'loop 2, round 3' session/mem-gaps
  row 2026-08-23T05:58:00Z gateloop-block  aaa7 'past the cap, no start row' session/mem-gaps
  want relabelled-loop 7 "$L" session/mem-gaps

  # another branch's rounds never leak in, and a pass row is not a block row
  row 2026-08-23T06:10:00Z gateloop-block aaa8 'other branch' session/other
  row 2026-08-23T06:20:00Z gateloop-pass  aaa9 'base+rounds' session/mem-gaps
  want cross-branch-and-pass 7 "$L" session/mem-gaps
  want other-branch          1 "$L" session/other

  # an event name quoted in a DETAIL field must not be counted
  row 2026-08-23T06:30:00Z gateloop-capout aab0 'the gateloop-block rows above stand' session/mem-gaps
  want detail-substring 7 "$L" session/mem-gaps

  # FIELD 4 vs FIELD 5. Both shapes are live in one log; pinning $5 read three rounds as one.
  # Its own log, so the count is unambiguous rather than an offset from the arc above.
  local L4="$d/f4.log"
  printf '2026-08-29T09:00:00Z\tgateloop-block\thead\tr1 findings\tsession/x\n' >> "$L4"
  printf '2026-08-29T09:10:00Z\tgateloop-block\thead\tsession/x\n'              >> "$L4"
  printf '2026-08-29T09:20:00Z\tgateloop-block\thead\tsession/x\n'              >> "$L4"
  want field4-rows-counted 3 "$L4" session/x
  # PRESENCE CONTROL: a counter hard-wired to 3 would pass the line above.
  want field4-other-branch 0 "$L4" session/y

  # a branch with no rows, and legacy 4-field rows that carry no branch
  want fresh-branch 0 "$L" session/fresh
  printf '2026-07-20T09:00:00Z\tgateloop-block\told\tlegacy 4-field row\n' >> "$L"
  want legacy-row 7 "$L" session/mem-gaps

  want missing-log 0 "$d/absent.log" session/mem-gaps

  rm -rf "$d"
  if [ "$f" -eq 0 ]; then echo "round-count selftest: OK"; return 0; fi
  return 1
}

case "${1:-}" in
  --selftest) selftest ;;
  "") echo "usage: round-count.sh <verify.log> [branch] | --selftest" >&2; exit 2 ;;
  *) count "$1" "${2:-$(git rev-parse --abbrev-ref HEAD)}" ;;
esac
