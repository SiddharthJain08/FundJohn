"""
S_quality_adjusted_size — "Size Matters, if You Control Your Junk" (Asness et al. 2018)
Quality-adjusted size: OLS residual of size rank on junk score.
LONG top quintile (quality small caps), SHORT bottom quintile (junk large caps).
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['QualityAdjustedSize']


class QualityAdjustedSize(BaseStrategy):
    """OLS residual of size rank on junk factor: LONG quality small caps, SHORT junk large caps."""

    id               = 'S_quality_adjusted_size'
    name             = 'QualityAdjustedSize'
    description      = 'Quality-adjusted size premium: OLS-residualise size rank on junk score; LONG top quintile, SHORT bottom quintile.'
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 60
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

        # ── Step 1: compute quality and size metrics per ticker ──────────────
        records: dict[str, dict] = {}
        for ticker in universe:
            if ticker not in prices.columns:
                continue
            fin = financials.get(ticker)
            if not fin:
                continue

            # Market cap for size rank
            mkt_cap = fin.get('marketCap') or fin.get('market_cap') or 0.0
            if mkt_cap <= 0:
                continue

            # ── Profitability ────────────────────────────────────────────────
            total_assets = fin.get('totalAssets') or 0.0
            book_eq      = (fin.get('totalStockholdersEquity')
                            or fin.get('totalEquity') or 0.0)
            revenue      = fin.get('revenue') or fin.get('totalRevenue') or 0.0
            cogs         = fin.get('costOfRevenue') or fin.get('costOfGoodsSold') or 0.0
            net_income   = fin.get('netIncome') or 0.0
            gross_profit = revenue - cogs

            roa = float(net_income) / float(total_assets) if total_assets > 0 else 0.0
            roe = float(net_income) / float(book_eq) if book_eq > 0 else 0.0
            gpa = float(gross_profit) / float(total_assets) if total_assets > 0 else 0.0
            profitability = roa + roe + gpa

            # ── Growth (proxy: YoY net income growth or asset growth) ────────
            asset_growth  = fin.get('totalAssetsGrowth') or fin.get('assetGrowth') or 0.0
            ni_growth     = fin.get('netIncomeGrowth') or 0.0
            growth        = float(ni_growth) - float(asset_growth)   # high profit growth, low asset growth

            # ── Safety (low leverage, low beta via stability) ────────────────
            total_debt    = (fin.get('totalDebt') or fin.get('longTermDebt') or 0.0)
            lev           = float(total_debt) / float(total_assets) if total_assets > 0 else 1.0
            safety        = -lev   # lower leverage → higher safety score

            # ── Payout (dividends + buybacks relative to earnings) ───────────
            dividends     = abs(fin.get('dividendsPaid') or fin.get('dividends') or 0.0)
            payout        = float(dividends) / float(abs(net_income)) if net_income != 0 else 0.0
            payout        = min(payout, 2.0)   # cap extreme values

            quality_score = profitability + growth + safety + payout
            records[ticker] = {
                'mkt_cap':       float(mkt_cap),
                'quality_score': quality_score,
            }

        if len(records) < 20:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # ── Step 2: cross-sectional ranks ────────────────────────────────────
        tickers      = list(records.keys())
        mkt_caps     = np.array([records[t]['mkt_cap']       for t in tickers], dtype=float)
        qual_scores  = np.array([records[t]['quality_score'] for t in tickers], dtype=float)

        # Size rank: rank(-mkt_cap) → small caps get high rank
        size_rank  = pd.Series(-mkt_caps).rank(pct=True).to_numpy()
        # Junk score: -quality_score; rank it pct
        junk_score = pd.Series(-qual_scores).rank(pct=True).to_numpy()

        # ── Step 3: OLS residual of size_rank ~ junk_score ───────────────────
        # alpha_signal = size_rank - (a + b * junk_score)
        X     = np.column_stack([np.ones(len(tickers)), junk_score])
        coef, _, _, _ = np.linalg.lstsq(X, size_rank, rcond=None)
        fitted        = X @ coef
        alpha_signal  = size_rank - fitted   # quality-adjusted size

        # ── Step 4: quintile selection ────────────────────────────────────────
        n         = len(tickers)
        q_cut     = max(n // 5, 1)
        q_cut     = min(q_cut, self.MAX_SIGNALS // 2)
        sorted_idx = np.argsort(alpha_signal)
        bottom_idx = sorted_idx[:q_cut]    # SHORT: junk large caps
        top_idx    = sorted_idx[-q_cut:]   # LONG:  quality small caps

        per_pos = min(round(scale / max(len(top_idx), 1), 4), 0.04)

        signals: List[Signal] = []

        for idx in top_idx:
            t = tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'LONG', price, regime_state=regime_state)
            a = float(alpha_signal[idx])
            conf = 'HIGH' if a > 0.3 else ('MED' if a > 0.1 else 'LOW')
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
                    'alpha_signal': round(a, 4),
                    'size_rank':    round(float(size_rank[idx]), 4),
                    'junk_score':   round(float(junk_score[idx]), 4),
                    'quality_score': round(float(qual_scores[idx]), 4),
                },
            ))

        for idx in bottom_idx:
            t = tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'SHORT', price, regime_state=regime_state)
            a = float(alpha_signal[idx])
            conf = 'HIGH' if a < -0.3 else ('MED' if a < -0.1 else 'LOW')
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
                    'alpha_signal': round(a, 4),
                    'size_rank':    round(float(size_rank[idx]), 4),
                    'junk_score':   round(float(junk_score[idx]), 4),
                    'quality_score': round(float(qual_scores[idx]), 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
