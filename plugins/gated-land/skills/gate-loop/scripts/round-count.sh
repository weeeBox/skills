#!/usr/bin/env bash
# round-count.sh <verify.log> [branch]  — count this BRANCH's blocked gate rounds of BOTH kinds,
# `gateloop-block` (this skill) and `land-verdict-block` (a repo's lander), in the append-only log.
#
# TWO ROW KINDS, READ TWO WAYS (2026-09-08). A branch driven through a lander rather than this skill
# emits `land-verdict-block`, so counting only `gateloop-block` left the per-branch cap
# STRUCTURALLY BLIND to a land loop. Measured in the scratch ledger: one branch ran EIGHTEEN land
# rounds while this counter returned 0 for it the whole time (17 land-verdict-block rows, 0
# gateloop-block).
#
# They cannot be read the same way. A land row is a four-field row whose detail is
# `<branch> <reason> run=<id>`, a shape its dashboard parses and which cannot grow a fifth field, so
# the branch is read as the FIRST WHITESPACE TOKEN of field 4. That is a structural POSITION, not a
# substring search, so a detail merely mentioning another branch still does not count: `see
# session/alpha for context` has first token `see`. Verified over that ledger: all 64
# land-verdict-block rows carry a branch as the detail's first token, zero non-branch.
#
# Only `land-verdict-block` counts, not `land-risk-block` or `land-verdict-override` or `land-abort`:
# a round is a GATE returning not-SHIP. A risk stop awaits a human override and an aborted round was
# discarded, so counting either would cap a branch nobody reviewed. Defaults to the current branch. Prints the integer; exit 0. >=3 = cap-out.
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
# THIRD SHAPE: THE BRANCH AS THE DETAIL'S FIRST TOKEN (2026-10-07, codegenalex/family-assistant#84).
# Sessions also wrote gateloop rows the land-row way, `<branch> r1 <findings>` in field 4. Measured
# on the family-assistant verify.log: session/issue-31 and session/issue-39 each have a block row
# shaped so, and the whole-field match read both as 0 - fail-OPEN. A gateloop-block row now counts
# when a whole field 3..NF OR the first whitespace token of field 4 is the branch; still a position,
# never a search. A row naming no branch at all (`r1: ...`, 3 of the last 3 there) is nobody's: it
# cannot be attributed, so it caps no one. That is the fail-open direction, and it is chosen over
# counting it for EVERY branch, which would cap branches that never ran a round.
#
# ponytail: a reused branch name (a deleted and recreated session/<slug>) over-counts, which
# stops the loop EARLY — the safe direction.
set -uo pipefail

count() { local log="$1" br="$2"
  if [ ! -f "$log" ]; then echo 0; return; fi
  awk -F '\t' -v br="$br" '
    $2 == "gateloop-block" {
      split($4, t, " "); hit = (t[1] == br)
      for (i = 3; i <= NF && !hit; i++) if ($i == br) hit = 1
      if (hit) c++
    }
    $2 == "land-verdict-block" {
      split($4, a, " "); if (a[1] == br) c++
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

  # THIRD SHAPE: branch as the first token of the 4-field detail (family-assistant#84), two branches
  # interleaved; a prefix-extended branch and a mid-detail mention still do not count
  local L3="$d/f3.log"
  printf '2026-09-18T22:30:30Z\tgateloop-block\tb7cee34\tsession/issue-31 r1 medium: x\n' >> "$L3"
  printf '2026-09-18T23:05:28Z\tgateloop-block\t43fb2ae\tsession/issue-39 r1 SHIP-WITH-CHANGES\n' >> "$L3"
  printf '2026-09-18T23:06:00Z\tgateloop-block\t43fb2ae\tsession/issue-31-r1 r1 x\n' >> "$L3"
  printf '2026-09-18T23:07:00Z\tgateloop-block\t43fb2ae\tsee session/issue-31 for context\n' >> "$L3"
  printf '2026-09-18T23:08:00Z\tgateloop-block\t43fb2ae\tsession/issue-31 r2 y\n' >> "$L3"
  printf '2026-10-02T11:01:18Z\tgateloop-block\t07ba0b9\tr1: no branch named\n' >> "$L3"
  want first-token-counted 2 "$L3" session/issue-31
  want first-token-other   1 "$L3" session/issue-39
  want first-token-prefix  1 "$L3" session/issue-31-r1

  # LAND ROUNDS COUNT TOO (2026-09-08). Real shape from the ledger: FOUR fields, the detail being
  # `<branch> <reason> run=<id>`, so the branch is its first whitespace token and not its own field.
  landrow() { printf '%s\tland-verdict-block\t%s\t%s not-SHIP run=lander-%s-1788636279-67311\n' \
    "2026-09-05T19:27:47Z" "d7dd6fdc" "$1" "$1" >> "$L"; }

  landrow session/mem-gaps
  want land-round-counts 8 "$L" session/mem-gaps
  landrow session/other
  want gateloop-and-land-SUM 2 "$L" session/other

  # the anti-substring property must SURVIVE the new row kind: first TOKEN, not a search
  printf '%s\tland-verdict-block\t%s\t%s\n' "2026-09-05T19:27:47Z" "d7dd6fdc" \
    'see session/mem-gaps for context not-SHIP run=lander-x-1-2' >> "$L"
  want land-detail-substring 8 "$L" session/mem-gaps

  # ...and a branch whose name EXTENDS ours is a different branch
  landrow session/mem-gaps-two
  want land-prefix-is-not-a-match 8 "$L" session/mem-gaps

  # a risk stop and an override are not review rounds, and an aborted round was discarded
  printf '%s\tland-risk-block\t%s\t%s\n' "2026-09-05T19:27:47Z" "d7dd6fdc" \
    'session/mem-gaps RISK=HIGH run=lander-x-1-3' >> "$L"
  printf '%s\tland-abort\t%s\t%s\n' "2026-09-05T19:27:47Z" "d7dd6fdc" \
    'session/mem-gaps stale round worktree run=lander-x-1-4' >> "$L"
  want land-nonblock-rows-ignored 8 "$L" session/mem-gaps

  # cumulative: the count still moves after all of the non-counting rows above
  landrow session/mem-gaps
  want land-count-still-moves 9 "$L" session/mem-gaps

  rm -rf "$d"
  if [ "$f" -eq 0 ]; then echo "round-count selftest: OK"; return 0; fi
  return 1
}

case "${1:-}" in
  --selftest) selftest ;;
  "") echo "usage: round-count.sh <verify.log> [branch] | --selftest" >&2; exit 2 ;;
  *) count "$1" "${2:-$(git rev-parse --abbrev-ref HEAD)}" ;;
esac
