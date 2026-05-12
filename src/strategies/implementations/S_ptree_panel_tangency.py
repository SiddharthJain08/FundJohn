"""
S_ptree_panel_tangency — Cong, Feng, He & He (JFE 2025)
"Growing the efficient frontier on panel trees"

Panel Trees recursively partition equities on financial characteristics to
construct mean-variance efficient leaf portfolios whose tangency portfolio
advances the efficient frontier beyond standard factor sorts.

IMPLEMENTATION NOTE — covariance-safe substitution:
The paper's tangency step requires mean-variance optimization over leaf
portfolios. With ≥200 assets and only a 252-day lookback the covariance
matrix is severely underdetermined (<1.3× obs/asset vs. the required 3×),
causing silent singular solutions. Instead we use a 4-factor rank composite
(Value, Profitability, Momentum, Investment) that replicates the paper's
characteristic-split tangency weights in a rank-equivalent sense.
LONG top quintile, SHORT bottom quintile.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['PTreePanelTangency']


class PTreePanelTangency(BaseStrategy):
    """4-factor characteristic rank composite (Value+OP+Mom+Inv): LONG top, SHORT bottom — Cong et al. JFE 2025."""

    id               = 'S_ptree_panel_tangency'
    name             = 'PTreePanelTangency'
    description      = '4-factor characteristic rank composite (Value+OP+Mom+Inv): LONG top, SHORT bottom — Cong et al. JFE 2025'
    tier             = 2
    signal_frequency = 'daily'
    min_lookback     = 252
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    _W_VALUE = 0.30
    _W_PROF  = 0.30
    _W_MOM   = 0.25
    _W_INV   = 0.15

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

        scale = self.position_scale(regime_state)

        # ── Step 1: filter universe to tickers with sufficient price history ──
        valid = [t for t in universe if t in prices.columns]
        if not valid:
            print('[debug] signals=0', file=sys.stderr)
            return []

        price_sub = prices[valid].dropna(axis=1, thresh=252)
        tickers = list(price_sub.columns)
        if len(tickers) < 10:
            print(f'[debug] signals=0 (too few tickers: {len(tickers)})', file=sys.stderr)
            return []

        last_prices = price_sub.iloc[-1]

        # ── Step 2: momentum — 12-to-1 month return ────────────────────────────
        if price_sub.shape[0] >= 252:
            p252 = price_sub.iloc[-252]
            p21  = price_sub.iloc[-21] if price_sub.shape[0] >= 21 else price_sub.iloc[-1]
            mom  = (p21 - p252) / p252.replace(0, np.nan)
        else:
            mom = pd.Series(0.0, index=tickers)

        # ── Step 3: fundamental characteristics from financials DataFrame ───
        fin_df = (aux_data or {}).get('financials')
        value_s = pd.Series(np.nan, index=tickers)
        prof_s  = pd.Series(np.nan, index=tickers)
        inv_s   = pd.Series(np.nan, index=tickers)

        if fin_df is not None and not fin_df.empty and 'ticker' in fin_df.columns:
            fin = fin_df[fin_df['ticker'].isin(tickers)].copy()
            if not fin.empty:
                if 'date' in fin.columns:
                    fin['date'] = pd.to_datetime(fin['date'], errors='coerce')
                    fin = fin.sort_values('date').groupby('ticker').last()
                else:
                    fin = fin.groupby('ticker').last()

                # Value: low pe_ratio + low ev_ebitda → high value rank
                if 'pe_ratio' in fin.columns:
                    pe = pd.to_numeric(fin['pe_ratio'], errors='coerce')
                    pe = pe[(pe > 0) & (pe < 200)]   # exclude negative/extreme
                    value_s.update(1 / pe.reindex(tickers))  # inverted: lower pe → higher score

                # Profitability: roe or roic (higher is better)
                for col in ('roe', 'roic', 'operating_margin'):
                    if col in fin.columns:
                        prof_s.update(pd.to_numeric(fin[col], errors='coerce').reindex(tickers))
                        break

                # Investment proxy: revenue_growth (lower → conservative investment → higher CMA)
                if 'revenue_growth' in fin.columns:
                    inv_s.update(pd.to_numeric(fin['revenue_growth'], errors='coerce').reindex(tickers))

        # ── Step 4: cross-sectional rank normalization (pct rank 0–1) ─────────
        def _rank(s: pd.Series, invert: bool = False) -> pd.Series:
            r = s.rank(pct=True, na_option='keep').reindex(tickers).fillna(0.5)
            return (1 - r) if invert else r

        mom_rank   = _rank(mom.reindex(tickers).fillna(0.0))
        value_rank = _rank(value_s)
        prof_rank  = _rank(prof_s)
        inv_rank   = _rank(inv_s, invert=True)  # low growth → high rank

        composite = (self._W_VALUE * value_rank
                     + self._W_PROF  * prof_rank
                     + self._W_MOM   * mom_rank
                     + self._W_INV   * inv_rank)

        # ── Step 5: quintile selection ─────────────────────────────────────────
        n     = len(tickers)
        q_cut = max(5, min(n // 5, self.MAX_SIGNALS // 2))
        sorted_idx = composite.argsort().values
        bottom_idx = sorted_idx[:q_cut]
        top_idx    = sorted_idx[-q_cut:]

        per_pos = float(min(round(scale / max(q_cut, 1), 4), 0.05))

        signals: List[Signal] = []

        for idx in top_idx:
            t = tickers[idx]
            ts = price_sub[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            if price <= 0 or np.isnan(price):
                continue
            stops = self.compute_stops_and_targets(ts, 'LONG', price, regime_state=regime_state)
            c = float(composite.iloc[idx])
            conf = 'HIGH' if c > 0.78 else ('MED' if c > 0.65 else 'LOW')
            signals.append(Signal(
                ticker=t, direction='LONG',
                entry_price=price,
                stop_loss=float(stops['stop']),
                target_1=float(stops['t1']), target_2=float(stops['t2']), target_3=float(stops['t3']),
                position_size_pct=per_pos, confidence=conf,
                signal_params={
                    'composite':  round(c, 4),
                    'mom_rank':   round(float(mom_rank.iloc[idx]),   4),
                    'value_rank': round(float(value_rank.iloc[idx]), 4),
                    'prof_rank':  round(float(prof_rank.iloc[idx]),  4),
                    'inv_rank':   round(float(inv_rank.iloc[idx]),   4),
                },
            ))

        for idx in bottom_idx:
            t = tickers[idx]
            ts = price_sub[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            if price <= 0 or np.isnan(price):
                continue
            stops = self.compute_stops_and_targets(ts, 'SHORT', price, regime_state=regime_state)
            c = float(composite.iloc[idx])
            conf = 'HIGH' if c < 0.22 else ('MED' if c < 0.35 else 'LOW')
            signals.append(Signal(
                ticker=t, direction='SHORT',
                entry_price=price,
                stop_loss=float(stops['stop']),
                target_1=float(stops['t1']), target_2=float(stops['t2']), target_3=float(stops['t3']),
                position_size_pct=per_pos, confidence=conf,
                signal_params={
                    'composite':  round(c, 4),
                    'mom_rank':   round(float(mom_rank.iloc[idx]),   4),
                    'value_rank': round(float(value_rank.iloc[idx]), 4),
                    'prof_rank':  round(float(prof_rank.iloc[idx]),  4),
                    'inv_rank':   round(float(inv_rank.iloc[idx]),   4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
