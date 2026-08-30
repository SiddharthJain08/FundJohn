#!/bin/bash
# bench_replay_parity_post.sh — spec 2026-08-30 §5.2 parity artefact, unattended.
# Runs the read-only rule-C/beta-budget replay against live NAV, pairs it with
# the day's live bench_sizing shadow line from logs/daily_cycle_steps_<date>.log,
# writes logs/bench_replay_parity_<date>.log and posts the summary to #botjohn-log.
# Read-only: no broker writes, no Redis, no .env changes. Run outside 13:00–20:15 UTC.
# Armed 2026-08-30 as transient timer openclaw-bench-replay-20260831 (Mon 20:50 UTC).
set -uo pipefail
cd /root/openclaw || exit 2
DATE="${1:-$(date -u +%F)}"
OUT=/root/openclaw/logs/bench_replay_parity_${DATE}.log
ts() { date -u +%FT%TZ; }
export POSTGRES_URI="$(grep -E '^POSTGRES_URI=' .env | cut -d= -f2- | tr -d '"')"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_MAX_THREADS=1

NAV="$(/root/go/bin/alpaca account get --jq .equity 2>/dev/null | tr -d '"')"
[ -n "$NAV" ] || { echo "[parity $(ts)] NAV unavailable from alpaca CLI" | tee -a "$OUT"; NAV=0; }

{
  echo "[parity $(ts)] date=$DATE nav=$NAV flags: $(grep -E '^OPENCLAW_BENCH_(RELATIVE_SIZING|BETA_BUDGET)=' .env | tr '\n' ' ')"
  echo "--- live sizer line(s) for $DATE"
  grep -h "bench_sizing" "logs/daily_cycle_steps_${DATE}.log" 2>/dev/null | grep -v DEBUG | cut -c1-600 || echo "(no bench_sizing line in logs/daily_cycle_steps_${DATE}.log)"
  echo "--- replay (rule C OFF vs ON + beta budget)"
  if [ "$NAV" != 0 ]; then
    timeout 900 nice -n 19 python3 scripts/bench_relative_sizing_replay.py --nav "$NAV" --beta-budget --top 15 2>&1 \
      | grep -vE "^\[debug\]|DEBUG|asset_corr_lw|asset_gate|entry_hygiene"
  fi
  echo "[parity $(ts)] done"
} 2>&1 | tee -a "$OUT"

# Post the condensed result (best-effort).
SUMMARY="$(grep -E "^\[parity|bench_sizing\.(shadow|apply)|^regime=|^gross |^beta_usd_on|^dropped|^added|no net-direction|bench_sizing: failed" "$OUT" | tail -12 | cut -c1-300)"
BENCH_TEXT="[replay-parity $DATE] $(printf '%s\n' "$SUMMARY")" \
python3 - <<'PY' 2>>"$OUT" || true
import json, os, urllib.request, psycopg2
text = os.environ['BENCH_TEXT'][:1900]
with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
    cur.execute("SELECT webhook_urls->>'botjohn-log' FROM agent_registry WHERE webhook_urls->>'botjohn-log' IS NOT NULL LIMIT 1")
    row = cur.fetchone()
if row and row[0]:
    req = urllib.request.Request(row[0], data=json.dumps({'content': text}).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'fundjohn-replay-parity/1.0'})
    urllib.request.urlopen(req, timeout=8).read()
PY
exit 0
