#!/bin/bash
cd /root/openclaw || exit 1
LOG=/root/openclaw/logs/fleet_tail_runs.log
for sid in S_vp_macd_index_sensitivity; do
  echo "=== $sid START $(date -u +%FT%TZ) ===" >> "$LOG"
  nice -n 19 python3 -m backtest.unified_backtest --strategy-id "$sid" >> "$LOG" 2>&1
  echo "=== $sid rc=$? END $(date -u +%FT%TZ) ===" >> "$LOG"
done
echo "=== ALL DONE $(date -u +%FT%TZ) ===" >> "$LOG"
