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

# Seconds until 13:00 UTC — the window close (clears the EDGAR/premarket band).
now=$(date -u +%s); close=$(date -u -d "13:00" +%s)
[ "$close" -le "$now" ] && close=$(date -u -d "tomorrow 13:00" +%s)
budget=$(( close - now ))
echo "[sp7-ladder] window budget ${budget}s" | tee -a "$LOG"

set +e
timeout --signal=TERM "$budget" nice -n 19 \
    python3 scripts/run_universe_ladder.py drain >> "$LOG" 2>&1
rc=$?
set -e

if [ $rc -eq 0 ] && grep -q "\[ladder\] DONE" "$LOG"; then
  rm -f "$ARMED"
  echo "[sp7-ladder] COMPLETE — disarmed" | tee -a "$LOG"
elif [ $rc -eq 124 ]; then
  echo "[sp7-ladder] window closed (SIGTERM at 13:00 UTC) — resumes next night" | tee -a "$LOG"
else
  echo "[sp7-ladder] driver rc=$rc — investigate $LOG" | tee -a "$LOG"
fi
