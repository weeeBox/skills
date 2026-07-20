#!/usr/bin/env bash
# round-count.sh <verify.log>  — count gateloop-block rows appended AFTER the last `gateloop-start` row,
# by LINE POSITION in the append-only log (not by timestamp). Position, not wall-clock, so a system
# clock shift (NTP/VM-restore) can never mis-count. Prints the integer; exit 0. >=3 = cap-out.
# FIELD-aware (awk -F '\t' on field $2), so a `gateloop-start` string appearing in a DETAIL field of a
# later row can never be mistaken for a real start marker.
set -uo pipefail
count() { local log="$1"
  [ -f "$log" ] || { echo 0; return; }
  awk -F '\t' '$2=="gateloop-start"{s=NR} $2=="gateloop-block"{blk[++m]=NR} END{c=0; for(i=1;i<=m;i++) if(blk[i]>s) c++; print c+0}' "$log"; }
selftest() { local d; d="$(mktemp -d)"; local L="$d/v.log"; local f=0
  printf '2026-07-20T09:00:00Z\tgateloop-block\told\tz\n' >> "$L"     # before start -> not counted
  printf '2026-07-20T09:30:00Z\tgateloop-start\thead\t-\n' >> "$L"
  printf '2026-07-20T08:00:00Z\tgateloop-block\tb1\tx\n' >> "$L"      # clock BACKWARD but AFTER start -> counted
  printf '2026-07-20T10:30:00Z\tgateloop-capout\thead\tnote mentions gateloop-start in detail\n' >> "$L"  # substring in DETAIL -> must NOT reset
  printf '2026-07-20T11:00:00Z\tgateloop-block\tb2\ty\n' >> "$L"
  [ "$(count "$L")" = 2 ] || { echo "FAIL count=$(count "$L")"; f=1; }
  rm -rf "$d"; [ $f -eq 0 ] && echo "round-count selftest: OK" || return 1; }
case "${1:-}" in
  --selftest) selftest ;;
  "") echo "usage: round-count.sh <verify.log> | --selftest" >&2; exit 2 ;;
  *) count "$@" ;;
esac
