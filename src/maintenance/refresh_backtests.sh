#!/usr/bin/env bash
#
# Weekly backtest refresh — runs unified_backtest on every live/candidate/
# staging strategy and then refreshes their eligible_regimes from the
# discovered per-regime metrics.
#
# Scheduled via openclaw-backtest-refresh.timer (Saturday 06:00 UTC, before
# the Saturday mastermind corpus run at 10:00 ET). Logs to journal and to
# /var/log/openclaw/backtest_refresh.log; posts a one-line summary to
# #botjohn-log on completion (best-effort).

set -euo pipefail

cd /root/openclaw
export PYTHONPATH=src

LOG_FILE="/var/log/openclaw/backtest_refresh_$(date -u +%Y%m%d_%H%M%S).log"
mkdir -p "$(dirname "$LOG_FILE")"

echo "[refresh_backtests] start $(date -u +%FT%TZ)" | tee -a "$LOG_FILE"

# 1. Run unified backtest for every live/candidate/staging strategy
python3 -m backtest.unified_backtest --all-live 2>&1 | tee -a "$LOG_FILE"
BT_RC=${PIPESTATUS[0]}

# 2. Refresh eligible_regimes from data (only if backtest succeeded)
if [ "$BT_RC" -eq 0 ]; then
  python3 -m backtest.eligibility_assigner --all 2>&1 | tee -a "$LOG_FILE"
  EA_RC=${PIPESTATUS[0]}
else
  echo "[refresh_backtests] backtest exit=$BT_RC; skipping eligibility_assigner" | tee -a "$LOG_FILE"
  EA_RC=99
fi

echo "[refresh_backtests] done $(date -u +%FT%TZ) backtest_rc=$BT_RC eligibility_rc=$EA_RC log=$LOG_FILE" \
  | tee -a "$LOG_FILE"

exit $BT_RC
