"""S-HV11: Cross-Stock Dispersion  Drechsler & Yaron (2011). Sell individual vol when
stocks are decorrelated despite high IV (dispersion opportunity)."""
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal

# Pairwise correlation is O(N²) — cap the universe sampled for the correlation matrix.
# At 5K+ tickers this would take minutes; 300 tickers runs in ~50ms.
MAX_CORR_UNIVERSE = 300


class CrossStockDispersion(BaseStrategy):
    id            = 'S_HV11_cross_stock_dispersion'
    name          = 'Cross-Stock Dispersion'
    version       = '1.0.0'
    active_in_regimes = ['HIGH_VOL', 'TRANSITIONING']

    def default_parameters(self) -> dict:
        return {
            'min_mean_iv_rank':   55.0,
            'max_pairwise_corr':  0.45,
            'min_iv_rank':        60.0,
            'min_iv_rv_spread':   0.04,
            'min_tickers':        5,
            'base_size_pct':      0.015,
        }

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty or len(prices) < 22:
            return []
        regime_state = (regime or {}).get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        options_data = (aux_data or {}).get('options', {})
        scale = self.position_scale(regime_state)
        p     = self.parameters

        # Collect tickers with valid options data
        eligible = []
        for ticker in universe:
            if ticker not in prices.columns:
                continue
            opts = options_data.get(ticker, {})
            iv_rank = opts.get('iv_rank')
            iv30    = opts.get('iv30')
            rv_20   = opts.get('rv_20')
            if iv_rank is None or iv30 is None or rv_20 is None:
                continue
            eligible.append({'ticker': ticker, 'iv_rank': iv_rank,
                             'iv30': iv30, 'rv_20': rv_20,
                             'iv_rv_spread': iv30 - rv_20})

        if len(eligible) < p['min_tickers']:
            return []

        # Compute pairwise correlations on 20-day returns
        # Cap to MAX_CORR_UNIVERSE by IV rank to keep runtime bounded at large universes
        tickers_with_prices = [e['ticker'] for e in eligible if e['ticker'] in prices.columns]
        if len(tickers_with_prices) > MAX_CORR_UNIVERSE:
            iv_rank_map = {e['ticker']: e['iv_rank'] for e in eligible}
            tickers_with_prices = sorted(
                tickers_with_prices, key=lambda t: iv_rank_map.get(t, 0), reverse=True
            )[:MAX_CORR_UNIVERSE]
        price_sub = prices[tickers_with_prices].iloc[-21:].pct_change().dropna()
        if price_sub.shape[0] < 15 or price_sub.shape[1] < 2:
            return []
        corr_matrix = price_sub.corr()
        n = corr_matrix.shape[0]
        corr_vals = [corr_matrix.iloc[i, j]
                     for i in range(n) for j in range(i+1, n)
                     if not np.isnan(corr_matrix.iloc[i, j])]
        if not corr_vals:
            return []
        mean_pairwise_corr = float(np.mean(corr_vals))
        mean_iv_rank = float(np.mean([e['iv_rank'] for e in eligible]))

        # Dispersion condition
        if mean_iv_rank < p['min_mean_iv_rank'] or mean_pairwise_corr > p['max_pairwise_corr']:
            return []

        conf = 'HIGH' if mean_pairwise_corr < 0.30 and mean_iv_rank > 65 else 'MED'
        signals = []
        for e in eligible:
            ticker = e['ticker']
            if e['iv_rank'] < p['min_iv_rank']:
                continue
            if e['iv_rv_spread'] < p['min_iv_rv_spread']:
                continue
            ts            = prices[ticker].dropna()
            current_price = float(ts.iloc[-1])
            size          = min(p['base_size_pct'] * (e['iv_rv_spread'] / 0.10) * scale, 0.03)
            signals.append(Signal(
                ticker=ticker, direction='SELL_VOL',
                entry_price=current_price, stop_loss=current_price * 1.07,
                target_1=current_price*0.93, target_2=current_price*0.85, target_3=current_price*0.78,
                position_size_pct=round(size, 4), confidence=conf,
                signal_params={
                    'iv_rank': round(e['iv_rank'],2), 'iv30': round(e['iv30'],4),
                    'rv_20': round(e['rv_20'],4), 'iv_rv_spread': round(e['iv_rv_spread'],4),
                    'mean_pairwise_corr': round(mean_pairwise_corr,4),
                    'mean_iv_rank': round(mean_iv_rank,2),
                    'n_eligible': len(eligible),
                },
            ))
        signals.sort(key=lambda s: s.signal_params.get('iv_rv_spread',0), reverse=True)
        return signals[:5]
