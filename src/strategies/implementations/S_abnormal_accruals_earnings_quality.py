"""
S_abnormal_accruals_earnings_quality — Modified Cross-Sectional Jones (1991) Model
LONG bottom-quintile abnormal-accruals (high earnings quality),
SHORT top-quintile abnormal-accruals (likely earnings inflation).
Rebalances quarterly on financials data. Based on Biswas (2025).
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['AbnormalAccrualsEarningsQuality']


class AbnormalAccrualsEarningsQuality(BaseStrategy):
    """LONG low-accrual / SHORT high-accrual quintiles — modified cross-sectional Jones model."""

    id               = 'S_abnormal_accruals_earnings_quality'
    name             = 'AbnormalAccrualsEarningsQuality'
    description      = 'LONG low-accrual (high earnings quality) / SHORT high-accrual firms via cross-sectional Jones model'
    tier             = 2
    signal_frequency = 'quarterly'
    min_lookback     = 1260
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    def _compute_accruals(self, fin: dict) -> tuple[float | None, float | None, float | None, float | None]:
        """Return (accruals_scaled, inv_ta, delta_rev_scaled, ppe_scaled) or None on missing data."""
        total_assets = fin.get('totalAssets') or fin.get('totalAssetsGrowth')
        if not total_assets:
            total_assets = fin.get('marketCap')  # last resort proxy
        if not total_assets or float(total_assets) <= 0:
            return None, None, None, None

        ta = abs(float(total_assets))

        # Actual accruals via cash-flow approach: NetIncome - OperatingCashFlow
        net_income = fin.get('netIncome') or fin.get('netIncomeRatio', 0) * ta
        op_cf = (
            fin.get('operatingCashFlow')
            or fin.get('cashFlowFromOperations')
            or fin.get('netCashProvidedByOperatingActivities')
        )
        if net_income is None or op_cf is None:
            return None, None, None, None

        accruals_scaled = (float(net_income) - float(op_cf)) / ta

        # Jones model regressors
        inv_ta = 1.0 / ta

        # ΔREV proxy: use revenueGrowth * revenue / TA
        revenue = fin.get('revenue') or fin.get('totalRevenue') or 0.0
        rev_growth = fin.get('revenueGrowth') or fin.get('revenueGrowthRate') or 0.0
        if revenue and rev_growth:
            delta_rev = abs(float(revenue)) * float(rev_growth)
        elif revenue:
            delta_rev = abs(float(revenue)) * 0.05  # assume 5% if no growth rate
        else:
            delta_rev = 0.0
        delta_rev_scaled = delta_rev / ta

        # PPE / TA
        ppe = fin.get('propertyPlantEquipmentNet') or fin.get('netPPE') or 0.0
        ppe_scaled = abs(float(ppe)) / ta

        return accruals_scaled, inv_ta, delta_rev_scaled, ppe_scaled

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        financials = (aux_data or {}).get('financials', {})
        if not financials:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # ── Step 1: collect Jones model inputs per ticker ─────────────────────
        data: dict[str, dict] = {}
        for ticker in universe:
            if ticker not in prices.columns:
                continue
            fin = financials.get(ticker)
            if not fin:
                continue

            accruals, inv_ta, delta_rev, ppe = self._compute_accruals(fin)
            if accruals is None:
                continue
            if not all(np.isfinite(v) for v in [accruals, inv_ta, delta_rev, ppe]):
                continue

            data[ticker] = {
                'accruals': accruals,
                'inv_ta':   inv_ta,
                'delta_rev': delta_rev,
                'ppe':       ppe,
            }

        if len(data) < 20:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # ── Step 2: cross-sectional OLS — Jones model fit ─────────────────────
        tickers = list(data.keys())
        y  = np.array([data[t]['accruals']  for t in tickers], dtype=float)
        x1 = np.array([data[t]['inv_ta']    for t in tickers], dtype=float)
        x2 = np.array([data[t]['delta_rev'] for t in tickers], dtype=float)
        x3 = np.array([data[t]['ppe']       for t in tickers], dtype=float)

        # Winsorize accruals at 1%/99% to reduce outlier influence
        lo, hi = np.percentile(y, 1), np.percentile(y, 99)
        y = np.clip(y, lo, hi)

        # OLS: y = β1*x1 + β2*x2 + β3*x3 (no intercept — Jones model specification)
        X = np.column_stack([x1, x2, x3])
        try:
            betas, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
            fitted = X @ betas
        except np.linalg.LinAlgError:
            # Fallback: use raw accruals if OLS fails
            fitted = np.zeros_like(y)

        abnormal = y - fitted   # Jones model residuals = abnormal accruals

        # ── Step 3: percentile rank and quintile selection ────────────────────
        abn_pct = pd.Series(abnormal).rank(pct=True).to_numpy()  # 0=lowest, 1=highest

        n      = len(tickers)
        q_cut  = max(n // 5, 1)
        q_cut  = min(q_cut, self.MAX_SIGNALS // 2)

        sorted_idx = np.argsort(abn_pct)
        long_idx   = sorted_idx[:q_cut]    # LONG: lowest abnormal accruals (high quality)
        short_idx  = sorted_idx[-q_cut:]   # SHORT: highest abnormal accruals (earnings inflation)

        per_pos = min(round(scale / max(q_cut, 1), 4), 0.05)

        signals: List[Signal] = []

        for idx in long_idx:
            t = tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'LONG', price, regime_state=regime_state)
            pct   = float(abn_pct[idx])
            conf  = 'HIGH' if pct < 0.10 else ('MED' if pct < 0.20 else 'LOW')
            signals.append(Signal(
                ticker            = t,
                direction         = 'LONG',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = per_pos,
                confidence        = conf,
                signal_params     = {
                    'abnormal_accruals': round(float(abnormal[idx]), 6),
                    'accruals_pct':      round(pct,                  4),
                    'strategy':          'abnormal_accruals_earnings_quality',
                },
            ))

        for idx in short_idx:
            t = tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'SHORT', price, regime_state=regime_state)
            pct   = float(abn_pct[idx])
            conf  = 'HIGH' if pct > 0.90 else ('MED' if pct > 0.80 else 'LOW')
            signals.append(Signal(
                ticker            = t,
                direction         = 'SHORT',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = per_pos,
                confidence        = conf,
                signal_params     = {
                    'abnormal_accruals': round(float(abnormal[idx]), 6),
                    'accruals_pct':      round(pct,                  4),
                    'strategy':          'abnormal_accruals_earnings_quality',
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
