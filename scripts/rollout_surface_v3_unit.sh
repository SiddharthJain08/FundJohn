#!/usr/bin/env bash
# scripts/rollout_surface_v3_unit.sh — body of openclaw-surface-v3-rollout.service
# (systemd mangles `$` and `%` inside ExecStart, so the chain lives here).
# 1. scripts/rollout_surface_v3.sh: wait for fleet units, rebuild the surface
#    master 2026-06-29..today, rebuild the panel, verify (§C thresholds).
# 2. On success: drop the five options strategies from the fleet checkpoint so
#    the next nightly fleet run re-derives their canonical rows on the §H panel
#    (manifest ids, macro rf, default window — see ledger ruling 2026-09-06).
# 3. Re-run the holiday strategy (no manifest id) via --strategy-file.
set -uo pipefail
cd /root/openclaw || exit 2
export PYTHONPATH=/root/openclaw/src
echo "[v3-rollout] start $(date -u +%FT%TZ)"
bash scripts/rollout_surface_v3.sh
rc=$?
echo "[v3-rollout] rollout rc=$rc"
if [ "$rc" -eq 0 ]; then
  sed -i -E '/^(S21_iv_hv_spread|S_HV8_gamma_theta_carry|S_HV19_iv_surface_tilt|S_HV20_iv_dispersion_reversion|S_options_flow_confirmed_momentum)$/d' data/.refresh_backtests.done
  echo "[v3-rollout] sleeve re-queued in the fleet checkpoint: $(wc -l < data/.refresh_backtests.done) done entries remain"
  python3 -m backtest.unified_backtest --strategy-file /root/openclaw/src/strategies/implementations/S_holiday_seasonality_energy_etf_tv1.py --universe-cap tier_liquid --start-date 2023-09-04
  echo "[v3-rollout] holiday rc=$?"
fi
echo "[v3-rollout] end rc=$rc $(date -u +%FT%TZ)"
exit "$rc"
