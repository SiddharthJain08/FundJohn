#!/usr/bin/env bash
# SP-7 Phase B — overnight-window wrapper for the tier-ladder queue.
# Armed by: python3 scripts/run_universe_ladder.py seed --arm
#   (or check_ladder_saturday.py / the :7870 recompute button)
# Runs nightly (timer 01:00 UTC) until the queue drains, then disarms.
set -euo pipefail
cd /root/openclaw
ARMED=data/.sp7_ladder_armed
LOG=logs/sp7_ladder_$(date -u +%F).log
[ -f "$ARMED" ] || { echo "[sp7-ladder] not armed, exiting"; exit 0; }

# Backfill has priority on the box — never share a window with it.
[ -f data/.sp7_backfill_armed ] && {
  echo "[sp7-ladder] backfill armed — yielding tonight" | tee -a "$LOG"; exit 0; }

set -a; . <(grep -E '^(POSTGRES_URI|REDIS_URL|OPENCLAW_UNIVERSE_AUTOADOPT)' .env | sed 's/\r$//'); set +a

# CONTINUOUS MODE (operator directive 2026-06-10): no 13:00 UTC window cap —
# the drain runs through the trading day as well. Memory-safety on the 8 GB
# no-swap box is via oom_score_adj=1000 (the kernel evicts a DRAIN cell first
# under memory pressure, NEVER the live trading cycle) + the unit's
# OOMPolicy=continue (a cell OOM is recorded as 'error' and the driver continues,
# instead of systemd tearing the whole drain down — MemoryMax=3G was removed
# 2026-06-11 after it deterministically killed every heavy cell).
# nice -19 still yields the CPU to live/intraday processes.
if echo 1000 > /proc/self/oom_score_adj 2>/dev/null; then
  echo "[sp7-ladder] continuous mode — oom_score_adj=1000 (drain is first OOM victim)" | tee -a "$LOG"
else
  echo "[sp7-ladder] continuous mode — WARN oom_score_adj unset; relying on unit OOMScoreAdjust" | tee -a "$LOG"
fi

set +e
nice -n 19 python3 scripts/run_universe_ladder.py drain >> "$LOG" 2>&1
rc=$?
set -e

if [ $rc -eq 0 ] && grep -q "\[ladder\] DONE" "$LOG"; then
  rm -f "$ARMED"
  echo "[sp7-ladder] COMPLETE — disarmed" | tee -a "$LOG"
else
  echo "[sp7-ladder] driver rc=$rc — still armed; resumes on next start / investigate $LOG" | tee -a "$LOG"
fi
