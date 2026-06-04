#!/usr/bin/env bash
# SP-7 A5 — overnight-window wrapper for the liquid-5k price backfill.
# Armed by the operator: touch /root/openclaw/data/.sp7_backfill_armed
# Runs nightly (timer 01:00 UTC = ~21:00 ET) until the driver reports DONE
# and the v2 list is fully covered, then disarms itself.
set -euo pipefail
cd /root/openclaw
ARMED=data/.sp7_backfill_armed
LOG=logs/sp7_backfill_$(date -u +%F).log
[ -f "$ARMED" ] || { echo "[sp7-backfill] not armed, exiting"; exit 0; }

set -a; . <(grep -E '^(POSTGRES_URI|REDIS_URL|ALPACA_API_KEY|ALPACA_API_SECRET|ALPACA_SECRET_KEY|DISCORD)' .env | sed 's/\r$//'); set +a

# Seconds until 13:00 UTC (08:00 ET) — the window close.
now=$(date -u +%s); close=$(date -u -d "13:00" +%s)
[ "$close" -le "$now" ] && close=$(date -u -d "tomorrow 13:00" +%s)
budget=$(( close - now ))
echo "[sp7-backfill] window budget ${budget}s" | tee -a "$LOG"

set +e
timeout --signal=TERM "$budget" nice -n 19 python3 scripts/backfill_universe_5y.py \
    --target prices --resume \
    --tickers "$(paste -sd, data/.backfill_universe_v2.txt)" >> "$LOG" 2>&1
rc=$?
set -e

missing=$(nice -n 19 python3 - <<'PYEOF'
import pandas as pd
cov = set(pd.read_parquet('data/master/prices.parquet', columns=['ticker']).ticker.unique())
v2 = [l.strip() for l in open('data/.backfill_universe_v2.txt') if l.strip()]
print(len([t for t in v2 if t not in cov]))
PYEOF
)
if [ $rc -eq 0 ] && grep -q "\[prices\] DONE" "$LOG" && [ "$missing" -eq 0 ]; then
  rm -f "$ARMED"
  echo "[sp7-backfill] COMPLETE — disarmed" | tee -a "$LOG"
elif [ $rc -eq 0 ] && grep -q "\[prices\] DONE" "$LOG" ; then
  echo "[sp7-backfill] driver DONE but $missing v2 tickers uncovered —" \
       "likely never-listed/quarantined-empty names; review backfill_audit," \
       "then disarm manually (rm $ARMED)" | tee -a "$LOG"
elif [ $rc -eq 124 ]; then
  echo "[sp7-backfill] window closed (SIGTERM at 13:00 UTC) — resumes tonight" | tee -a "$LOG"
else
  echo "[sp7-backfill] driver rc=$rc — investigate $LOG" | tee -a "$LOG"
fi
