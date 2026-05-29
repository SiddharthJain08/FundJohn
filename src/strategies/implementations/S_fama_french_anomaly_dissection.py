"""
S_fama_french_anomaly_dissection — Fama & French (2008) JF
Equal-weighted composite of net stock issuance (NSI), accruals, and 12m-1m
price momentum ranks. LONG top quintile, SHORT bottom quintile across all cap tiers.
[Dissecting Anomalies, §3 NSI, §4 accruals, §5 momentum]
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE
from src.strategies.universe_default import r3000 as universe_filter

__all__ = ['FamaFrenchAnomalyDissection']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_fama_french_anomaly_dissection'


class FamaFrenchAnomalyDissection(BaseStrategy):
    """NSI + accruals + momentum composite: LONG top quintile, SHORT bottom — Fama & French (2008)."""

    id               = STRATEGY_ID
    name             = 'FamaFrenchAnomalyDissection'
    description      = ('NSI + accruals + 12m-1m momentum composite rank: '
                        'LONG top quintile, SHORT bottom quintile — Fama & French (2008)')
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
        scale = self.position_scale(regime_state)

        # ── Step 1: compute per-ticker factor scores ──────────────────────────
        scores: dict[str, dict] = {}
        for ticker in universe:
            if ticker not in prices.columns:
                continue

            ts = prices[ticker].dropna()
            if len(ts) < 252:
                continue

            # ── Momentum: 12m-1m return (skip last 21 trading days) ──────────
            mom = float(ts.iloc[-21] / ts.iloc[-252] - 1.0) if len(ts) >= 252 else None

            # ── Financials-based factors ─────────────────────────────────────
            fin = (financials.get(ticker) or {}) if financials else {}
            total_assets = abs(float(fin['totalAssets'])) if fin.get('totalAssets') else None

            # Accruals: (net_income - CFO) / total_assets
            accruals = None
            if total_assets and total_assets > 0:
                net_income = fin.get('netIncome')
                cfo = (fin.get('operatingCashFlow')
                       or fin.get('cashFlowFromOperations')
                       or fin.get('netCashProvidedByOperatingActivities'))
                if net_income is not None and cfo is not None:
                    accruals = (float(net_income) - float(cfo)) / total_assets

            # NSI: net stock issuance proxy from cash flow statement
            # Positive = issuance (bad), Negative = repurchase (good)
            nsi = None
            if total_assets and total_assets > 0:
                issued = float(fin.get('issuanceOfCommonStock') or fin.get('stockIssuance') or 0.0)
                repurchased = abs(float(fin.get('repurchasesOfCommonStock')
                                        or fin.get('stockRepurchase') or 0.0))
                if issued != 0.0 or repurchased != 0.0:
                    nsi = (issued - repurchased) / total_assets

            scores[ticker] = {
                'mom':      mom,
                'accruals': accruals,
                'nsi':      nsi,
                'price':    float(ts.iloc[-1]),
                'ts':       ts,
            }

        # Need enough stocks to form meaningful quintiles
        valid = {t: v for t, v in scores.items() if v['mom'] is not None}
        if len(valid) < 20:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        tickers = list(valid.keys())

        # ── Step 2: cross-sectional percentile ranks ──────────────────────────
        moms      = np.array([valid[t]['mom'] for t in tickers], dtype=float)
        mom_rank  = pd.Series(moms).rank(pct=True).to_numpy()

        # Accruals rank: lower accruals → higher score (negate)
        accruals_arr = np.array([
            valid[t]['accruals'] if valid[t]['accruals'] is not None else np.nan
            for t in tickers
        ], dtype=float)
        acc_rank = pd.Series(-accruals_arr).rank(pct=True, na_option='keep').to_numpy()
        acc_rank = np.where(np.isnan(acc_rank), 0.5, acc_rank)  # neutral for missing

        # NSI rank: lower issuance → higher score (negate)
        nsi_arr = np.array([
            valid[t]['nsi'] if valid[t]['nsi'] is not None else np.nan
            for t in tickers
        ], dtype=float)
        nsi_rank = pd.Series(-nsi_arr).rank(pct=True, na_option='keep').to_numpy()
        nsi_rank = np.where(np.isnan(nsi_rank), 0.5, nsi_rank)  # neutral for missing

        # Equal-weighted composite
        composite = (nsi_rank + acc_rank + mom_rank) / 3.0

        # ── Step 3: quintile selection ────────────────────────────────────────
        n       = len(tickers)
        q_cut   = max(n // 5, 1)
        q_cut   = min(q_cut, self.MAX_SIGNALS // 2)
        sorted_idx = np.argsort(composite)
        bottom_idx = sorted_idx[:q_cut]     # SHORT: low NSI score, high accruals, weak momentum
        top_idx    = sorted_idx[-q_cut:]    # LONG:  low issuance, low accruals, strong momentum

        per_pos = min(round(scale / max(len(top_idx), 1), 4), 0.04)

        signals: List[Signal] = []

        for idx in top_idx:
            t    = tickers[idx]
            v    = valid[t]
            ts   = v['ts']
            price = v['price']
            stops = self.compute_stops_and_targets(ts, 'LONG', price, regime_state=regime_state)
            c     = float(composite[idx])
            conf  = 'HIGH' if c > 0.80 else ('MED' if c > 0.65 else 'LOW')
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
                    'composite':  round(c, 4),
                    'mom_rank':   round(float(mom_rank[idx]), 4),
                    'acc_rank':   round(float(acc_rank[idx]), 4),
                    'nsi_rank':   round(float(nsi_rank[idx]), 4),
                },
            ))

        for idx in bottom_idx:
            t    = tickers[idx]
            v    = valid[t]
            ts   = v['ts']
            price = v['price']
            stops = self.compute_stops_and_targets(ts, 'SHORT', price, regime_state=regime_state)
            c     = float(composite[idx])
            conf  = 'HIGH' if c < 0.20 else ('MED' if c < 0.35 else 'LOW')
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
                    'composite':  round(c, 4),
                    'mom_rank':   round(float(mom_rank[idx]), 4),
                    'acc_rank':   round(float(acc_rank[idx]), 4),
                    'nsi_rank':   round(float(nsi_rank[idx]), 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
