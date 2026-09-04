#!/bin/bash
# fresh_book_deploy_report.sh — evening report on the first fresh-book deploy
# after the 2026-09-04 paper-account cutover (account PA3K16GEOQ4E), posted to
# #botjohn-log. Read-only: no broker writes, no .env changes.
#
# Covers: bench_sizing.apply + bench_corr_removal + entry_hygiene + exit_hook
# lines from the day's cycle log; broker equity / SPY position / SPY fills /
# position count; the dashboard's SPY benchmark section source
# (/api/portfolio/positions benchmark rows) and the epoch-reset stats
# (/api/portfolio/summary). On a non-trading day it says so and exits.
#
# Usage: scripts/fresh_book_deploy_report.sh [YYYY-MM-DD] [--no-post]
# Armed 2026-09-04 as transient timers openclaw-freshbook-report-20260907 /
# -20260908 (21:00 UTC). Transient timers DIE ON REBOOT — re-arm with:
#   systemd-run --unit=openclaw-freshbook-report-<date> \
#     --on-calendar='<date> 21:00:00 UTC' /root/openclaw/scripts/fresh_book_deploy_report.sh
set -uo pipefail
cd /root/openclaw || exit 2
DATE="${1:-$(date -u +%F)}"
POST=1; [ "${2:-}" = "--no-post" ] && POST=0
OUT=/root/openclaw/logs/fresh_book_report_${DATE}.log
ts() { date -u +%FT%TZ; }
A=/root/go/bin/alpaca

{
  echo "[fresh-book $(ts)] date=$DATE account cutover report"

  # Trading day? (calendar row for DATE present)
  CAL="$($A calendar --start "$DATE" --end "$DATE" 2>/dev/null | tr -d ' \n')"
  if [ -z "$CAL" ] || [ "$CAL" = "[]" ] || [ "$CAL" = "null" ]; then
    echo "market CLOSED on $DATE (holiday/weekend) — no deploy; first fresh-book deploy fires the next session (15:00/15:55 ET)."
  else
    echo "--- sizer lines (logs/daily_cycle_steps_${DATE}.log)"
    grep -hE "bench_sizing\.(shadow|apply)|bench_corr_removal|beta budget|no benchmark ticker survived" \
      "logs/daily_cycle_steps_${DATE}.log" 2>/dev/null | grep -v DEBUG | cut -c1-420 | tail -6 \
      || echo "(no bench_sizing line — compute may not have run)"
    grep -hE "entry_hygiene: premarket-veto|\[exit_hook\]" "logs/daily_cycle_steps_${DATE}.log" 2>/dev/null \
      | grep -v DEBUG | cut -c1-300 | tail -3
    grep -h "bench_realized" "logs/daily_cycle_steps_${DATE}.log" 2>/dev/null | cut -c1-320 | tail -1

    echo "--- broker (account PA3K16GEOQ4E)"
    $A account get --jq '{equity, cash, buying_power}' 2>/dev/null
    echo "positions: $($A position list --jq 'length' 2>/dev/null)"
    echo "SPY position: $($A position list --jq '.[] | select(.symbol=="SPY") | {qty, market_value, avg_entry_price, unrealized_pl}' 2>/dev/null | tr -d ' \n')"
    echo "SPY orders today:"
    $A order list --symbols SPY --status all --limit 20 --nested \
      --jq "[.[] | select(.submitted_at >= \"${DATE}\") | {t:.submitted_at, side, qty, status, filled:.filled_qty}]" 2>/dev/null | tr -d ' \n' | cut -c1-500
    echo

    echo "--- dashboard SPY section + epoch stats (:3000)"
    curl -s --max-time 20 http://127.0.0.1:3000/api/portfolio/positions \
      | python3 -c "
import json, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    print('positions endpoint unreadable'); raise SystemExit
bench = [r for r in rows if r.get('benchmark')]
tk = sorted({r['ticker'] for r in bench})
alpha = sorted({r['ticker'] for r in rows if not r.get('benchmark')})
print(f'benchmark section rows: {len(bench)} (tickers {tk}) | alpha tickers in tiles: {len(alpha)}')" 2>/dev/null
    curl -s --max-time 20 http://127.0.0.1:3000/api/portfolio/summary \
      | python3 -c "
import json, sys
s = json.load(sys.stdin)
print('epoch stats: open=%s closed=%s win_rate=%s%% avg_realized=%s' % (
    s.get('open_count'), s.get('closed_count'), s.get('win_rate'), s.get('avg_realized')))" 2>/dev/null
  fi
  echo "[fresh-book $(ts)] done"
} 2>&1 | tee -a "$OUT"

# Post the condensed result (best-effort).
if [ "$POST" = 1 ]; then
  SUMMARY="$(grep -vE "^\[fresh-book" "$OUT" | tail -24)"
  POSTGRES_URI="$(grep -E '^POSTGRES_URI=' .env | cut -d= -f2- | tr -d '"')" \
  FRESH_TEXT="[fresh-book $DATE] first-deploy report (cutover 09-04 → PA3K16GEOQ4E)
$SUMMARY" \
  python3 - <<'PY' 2>>"$OUT" || true
import json, os, urllib.request, psycopg2
text = os.environ['FRESH_TEXT'][:1900]
with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
    cur.execute("SELECT webhook_urls->>'botjohn-log' FROM agent_registry WHERE webhook_urls->>'botjohn-log' IS NOT NULL LIMIT 1")
    row = cur.fetchone()
if row and row[0]:
    req = urllib.request.Request(row[0], data=json.dumps({'content': text}).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'fundjohn-fresh-book/1.0'})
    urllib.request.urlopen(req, timeout=8).read()
PY
fi
exit 0
