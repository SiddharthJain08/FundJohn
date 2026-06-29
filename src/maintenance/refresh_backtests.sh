#!/usr/bin/env bash
#
# Weekly backtest refresh — runs unified_backtest on every live/candidate/
# staging strategy and then refreshes their eligible_regimes from the
# discovered per-regime metrics.
#
# Invoked as step 5 of weekend_saturday.sh (Sat 08:00 ET). The standalone
# openclaw-backtest-refresh.timer (Sat 06:00 UTC) is DISABLED, so this is the
# sole Saturday caller. Logs to /var/log/openclaw/backtest_refresh_*.log.

set -euo pipefail

cd /root/openclaw
export PYTHONPATH=src

LOG_FILE="/var/log/openclaw/backtest_refresh_$(date -u +%Y%m%d_%H%M%S).log"
mkdir -p "$(dirname "$LOG_FILE")"

echo "[refresh_backtests] start $(date -u +%FT%TZ)" | tee -a "$LOG_FILE"

# 1. Run unified backtest for every live/candidate/staging strategy.
# Bounded (6h) + niced (W1 reconcile 2026-06-28): the full --all-live refresh can
# exceed the weekend window on the 2-core VPS. The cap lets the caller
# (weekend_saturday.sh) still run weekly weights/panels on last-known backtests
# instead of being killed mid-refresh. timeout wraps the python directly so the
# whole process tree dies cleanly (no orphaned backtest).
# Durable fix = per-strategy subprocess/watchdog resumable refresh (tracked separately).
timeout -k 60 21600 nice -n 19 python3 -m backtest.unified_backtest --all-live 2>&1 | tee -a "$LOG_FILE"
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
