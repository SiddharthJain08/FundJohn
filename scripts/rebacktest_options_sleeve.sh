#!/usr/bin/env bash
# scripts/rebacktest_options_sleeve.sh — serial re-backtests after the options
# surface v2 panel rebuild (spec 2026-09-04 A.8) + the holiday strategy (D.3).
# Run as a transient unit outside the Saturday research lane:
#   sudo systemd-run --unit=rebacktest-options-20260905 --nice=19 -p MemoryMax=3500M \
#     -p RuntimeMaxSec=6h -E PYTHONUNBUFFERED=1 -E PYTHONPATH=/root/openclaw/src \
#     --working-directory=/root/openclaw /bin/bash scripts/rebacktest_options_sleeve.sh
set -uo pipefail
cd /root/openclaw
IMPL=src/strategies/implementations
STRATS=(
  "$IMPL/S21_iv_hv_spread.py"
  "$IMPL/shv8_gamma_theta_carry.py"
  "$IMPL/shv19_iv_surface_tilt.py"
  "$IMPL/shv20_iv_dispersion_reversion.py"
  "$IMPL/S_options_flow_confirmed_momentum.py"
  "$IMPL/S_pre_earnings_vol_runup.py"
  "$IMPL/S_holiday_seasonality_energy_etf_tv1.py"
)
rc_all=0
for f in "${STRATS[@]}"; do
  echo "[rebacktest] $(date -u +%FT%TZ) start $(basename "$f")"
  python3 -m backtest.unified_backtest --strategy-file "$PWD/$f" --universe-cap tier_liquid --start-date 2023-09-04
  rc=$?; echo "[rebacktest] $(date -u +%FT%TZ) rc=$rc $(basename "$f")"; [ $rc -ne 0 ] && rc_all=1
done
echo "[rebacktest] done rc_all=$rc_all"
exit $rc_all
