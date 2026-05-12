"""
S_q_theory_investment_growth — Xing (2007) RFS
Q-theory investment factor: LONG lowest-IGR quintile, SHORT highest-IGR quintile.
IGR = (CapEx_t - CapEx_{t-1}) / CapEx_{t-1}
Low investment growth → high discount rates → high expected returns (and vice versa).
Monthly rebalance using most recent fiscal-year financials.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['QTheoryInvestmentGrowth']


class QTheoryInvestmentGrowth(BaseStrategy):
    """LONG low-IGR / SHORT high-IGR quintiles — Q-theory investment factor (Xing 2007)."""

    id               = 'S_q_theory_investment_growth'
    name             = 'QTheoryInvestmentGrowth'
    description      = 'LONG low-IGR / SHORT high-IGR quintiles — Q-theory rational discount-rate explanation for value premium (Xing 2007)'
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 1260
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

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

        # ── Step 1: compute IGR for each ticker ──────────────────────────────
        igr_map: dict[str, float] = {}
        for ticker in universe:
            if ticker not in prices.columns:
                continue
            fin = financials.get(ticker)
            if not fin:
                continue

            # CapEx: prefer capitalExpenditures, fallback to capex / investmentsInPropertyPlantAndEquipment
            capex_curr = (
                fin.get('capitalExpenditures')
                or fin.get('capex')
                or fin.get('investmentsInPropertyPlantAndEquipment')
            )
            capex_prev = (
                fin.get('capitalExpendituresPriorYear')
                or fin.get('capexPriorYear')
                or fin.get('capitalExpendituresPY')
            )

            # Fallback: use total investment / net capital if CapEx not directly available
            if capex_curr is None:
                total_inv = fin.get('totalInvestment') or fin.get('propertyPlantEquipmentNet')
                capex_curr = total_inv

            if capex_curr is None:
                continue

            capex_curr = abs(float(capex_curr))   # CapEx is often reported negative in cash flow

            if capex_prev is not None:
                capex_prev = abs(float(capex_prev))
                if capex_prev <= 0:
                    continue
                igr = (capex_curr - capex_prev) / capex_prev
            else:
                # No prior-year CapEx: use asset-growth as proxy (common in FMP data)
                asset_growth = fin.get('totalAssetsGrowth') or fin.get('assetGrowth')
                if asset_growth is None:
                    continue
                igr = float(asset_growth)

            if not np.isfinite(igr):
                continue

            igr_map[ticker] = igr

        if len(igr_map) < 20:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # ── Step 2: cross-sectional rank by IGR ──────────────────────────────
        tickers  = list(igr_map.keys())
        igr_vals = np.array([igr_map[t] for t in tickers], dtype=float)
        igr_pct  = pd.Series(igr_vals).rank(pct=True).to_numpy()  # 0=lowest, 1=highest

        # ── Step 3: quintile selection ────────────────────────────────────────
        n      = len(tickers)
        q_cut  = max(n // 5, 1)
        q_cut  = min(q_cut, self.MAX_SIGNALS // 2)

        sorted_idx  = np.argsort(igr_pct)
        long_idx    = sorted_idx[:q_cut]    # LONG: lowest IGR (high expected returns)
        short_idx   = sorted_idx[-q_cut:]   # SHORT: highest IGR (low expected returns)

        per_pos = min(round(scale / max(len(long_idx), 1), 4), 0.05)

        signals: List[Signal] = []

        for idx in long_idx:
            t = tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'LONG', price, regime_state=regime_state)
            pct   = float(igr_pct[idx])
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
                    'igr':      round(float(igr_map[t]), 4),
                    'igr_pct':  round(pct,               4),
                    'strategy': 'q_theory_investment_growth',
                },
            ))

        for idx in short_idx:
            t = tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'SHORT', price, regime_state=regime_state)
            pct   = float(igr_pct[idx])
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
                    'igr':      round(float(igr_map[t]), 4),
                    'igr_pct':  round(pct,               4),
                    'strategy': 'q_theory_investment_growth',
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
