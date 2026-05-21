"""
Quality-Filtered Momentum — top-N JT momentum gated by gross-margin quality screen.
Ranks universe by 12mo (skip 1mo) momentum, then filters to high-quality names
(top 50% by gross margin if financials available, else by return Sharpe proxy).
Long the top-N quality-momentum names in LOW_VOL and TRANSITIONING regimes.
"""
from __future__ import annotations
import sys
import pandas as pd
import numpy as np
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['S22QualityMomentum']


class S22QualityMomentum(BaseStrategy):
    """Quality-Filtered Momentum — top-N JT momentum gated by gross-margin quality screen."""

    id                = 'S22_quality_momentum'
    name              = 'Quality-Filtered Momentum'
    description       = 'Quality-Filtered Momentum — top-N JT momentum gated by gross-margin quality screen'
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 273
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    def default_parameters(self) -> dict:
        return {
            'lookback_long':    252,   # 12-month lookback (trading days)
            'lookback_skip':    21,    # skip most recent month
            'top_n':            8,     # long positions
            'min_mom_thresh':   0.03,  # minimum momentum to qualify
            'quality_pct':      0.50,  # keep top 50% by quality score
            'vol_window':       63,    # rolling window for Sharpe-proxy quality
        }

    def _quality_scores_from_financials(self, universe: List[str], financials: pd.DataFrame) -> pd.Series:
        """Extract gross-margin-based quality scores from financials aux_data."""
        scores = {}
        if not isinstance(financials, pd.DataFrame) or financials.empty:
            return pd.Series(scores)
        # financials may be indexed by (date, ticker) or (ticker,) depending on data shape
        try:
            if isinstance(financials.index, pd.MultiIndex):
                latest = financials.groupby(level='ticker').last()
            elif 'ticker' in financials.columns:
                latest = financials.set_index('ticker')
            else:
                latest = financials
            gm_col = None
            for candidate in ('gross_margin', 'grossMargin', 'gross_profit_margin'):
                if candidate in latest.columns:
                    gm_col = candidate
                    break
            if gm_col is None:
                return pd.Series(scores)
            for t in universe:
                if t in latest.index:
                    val = latest.loc[t, gm_col]
                    if pd.notna(val):
                        scores[t] = float(val)
        except Exception:
            pass
        return pd.Series(scores)

    def _quality_scores_from_prices(self, prices_u: pd.DataFrame, vol_window: int) -> pd.Series:
        """Sharpe-proxy quality: annualized return / annualized vol over vol_window days."""
        scores = {}
        rets = prices_u.pct_change().dropna(how='all')
        if len(rets) < vol_window:
            return pd.Series(scores)
        window_rets = rets.iloc[-vol_window:]
        for t in prices_u.columns:
            col = window_rets[t].dropna()
            if len(col) < 20:
                continue
            mu  = float(col.mean()) * 252
            std = float(col.std())  * (252 ** 0.5)
            if std > 0:
                scores[t] = mu / std
        return pd.Series(scores)

    def generate_signals(self, prices: pd.DataFrame, regime: dict, universe: List[str], aux_data: dict = None) -> List[Signal]:
        if prices is None or prices.empty:
            print(f'[debug] signals=0', file=sys.stderr)
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        p = self.parameters
        lookback   = p.get('lookback_long', 252)
        skip       = p.get('lookback_skip', 21)
        top_n      = p.get('top_n', 8)
        min_mom    = p.get('min_mom_thresh', 0.03)
        qual_pct   = p.get('quality_pct', 0.50)
        vol_window = p.get('vol_window', 63)

        if len(prices) < lookback + skip:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        available = [
            t for t in universe
            if t in prices.columns
            and not t.startswith('^')
            and not t.endswith('=F')
            and '-USD' not in t
        ]
        if len(available) < 10:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        prices_u = prices[available].copy()

        # Step 1: momentum scores (12mo skip 1mo)
        mom = {}
        for t in prices_u.columns:
            s = prices_u[t].dropna()
            if len(s) < lookback + skip:
                continue
            try:
                m = float(s.iloc[-skip]) / float(s.iloc[-(lookback + skip)]) - 1
            except (ZeroDivisionError, ValueError):
                continue
            if m >= min_mom:
                mom[t] = m
        mom_series = pd.Series(mom)
        if len(mom_series) < 3:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Step 2: quality scores
        financials = (aux_data or {}).get('financials')
        qual_series = self._quality_scores_from_financials(list(mom_series.index), financials)
        if len(qual_series) < len(mom_series) * 0.3:
            qual_series = self._quality_scores_from_prices(prices_u[list(mom_series.index)], vol_window)

        # Filter to quality threshold
        if len(qual_series) >= 4:
            qual_thresh = qual_series.quantile(1 - qual_pct)
            quality_universe = qual_series[qual_series >= qual_thresh].index.tolist()
            mom_filtered = mom_series[mom_series.index.isin(quality_universe)]
        else:
            mom_filtered = mom_series

        if len(mom_filtered) < 2:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        top_names = mom_filtered.nlargest(top_n).index.tolist()
        scale     = self.position_scale(regime_state)
        pos_size  = round((1.0 / max(len(top_names), 1)) * 0.16 * scale, 4)

        signals: List[Signal] = []
        mom_ranked = mom_filtered.rank(pct=True)

        for ticker in top_names:
            ts = prices_u[ticker].dropna()
            if len(ts) < 14:
                continue
            current = float(ts.iloc[-1])
            stops   = self.compute_stops_and_targets(ts, 'LONG', current, regime_state=regime_state)
            mom_rank = float(mom_ranked.get(ticker, 0.5))
            qual_val = float(qual_series.get(ticker, 0.0)) if ticker in qual_series.index else 0.0

            if mom_rank >= 0.90:
                confidence = 'HIGH'
            elif mom_rank >= 0.65:
                confidence = 'MED'
            else:
                confidence = 'LOW'

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = current,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = pos_size,
                confidence        = confidence,
                signal_params     = {
                    'momentum_12mo':  round(float(mom_series.get(ticker, 0.0)), 4),
                    'momentum_rank':  round(mom_rank, 4),
                    'quality_score':  round(qual_val, 4),
                    'regime':         regime_state,
                    'scale':          scale,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
