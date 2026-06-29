"""
Time Series Momentum Effect — ETF proxy implementation.

Source: https://quantpedia.com/strategies/time-series-momentum-effect/
Reference: Moskowitz, Ooi & Pedersen (2012), "Time Series Momentum", JFE.

Original universe: 24 commodity futures, 12 FX pairs, 9 equity-index futures,
13 government bond futures. This implementation proxies via daily-bar ETFs in
prices.parquet covering the same four asset classes.

LONG when 12-month trailing return > 0; FLAT otherwise (no shorting in ETP class).
Sized inversely to realized vol, targeting 10% annualized portfolio volatility.
Monthly rebalance (fires on the first trading day of each calendar month).
"""
from __future__ import annotations

import sys
from math import sqrt
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['AstTimeSeriesMomentumEffect']

INSTRUMENT_CLASS = 'etp'

STRATEGY_ID = 'S_ast_time_series_momentum_effect'

# ETP proxy basket — 4 asset classes from the original paper
BASKET = [
    # Commodity ETFs
    'GLD', 'SLV', 'USO', 'DBC', 'PDBC',
    # Currency ETFs (USD + major FX)
    'UUP', 'FXE', 'FXY', 'FXB', 'FXA', 'FXC', 'FXF', 'FXS',
    # Equity-index ETFs
    'SPY', 'EFA', 'EWG', 'EWJ', 'EWU', 'EWL', 'EWQ',
    # Bond ETFs
    'TLT', 'IEF', 'SHY', 'BNDX', 'BWX',
]

LOOKBACK     = 252   # 12-month trailing return (≈12×21 trading days)
VOL_WINDOW   = 60    # realized-vol estimation window (trading days)
TARGET_VOL   = 0.10  # 10% annualized portfolio vol target
LEVERAGE_CAP = 4.0   # max per-asset leverage (from original paper)


class AstTimeSeriesMomentumEffect(BaseStrategy):
    """ETF proxy of Moskowitz, Ooi & Pedersen (2012) TSMOM.

    Monthly rebalance: go LONG each ETF with positive 12-month trailing return,
    sized inversely to its 60-day realized vol to target 10% portfolio vol.
    No short leg — original used futures; ETF shorting costs are prohibitive.
    """

    id                = STRATEGY_ID
    name              = 'Time Series Momentum Effect (ETF proxy)'
    description       = (
        'Monthly 12-month trailing return signal across 24 multi-asset ETFs; '
        'LONG when return > 0, inverse-vol sized to target 10% annualized portfolio vol.'
    )
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = LOOKBACK + 1
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 24

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        # Monthly rebalance gate — only fire on the first trading day of each month
        if len(prices) < 2:
            return []
        if prices.index[-1].month == prices.index[-2].month:
            return []

        if len(prices) < LOOKBACK + 1:
            return []

        scale = self.position_scale(regime_state)

        available = [t for t in BASKET if t in prices.columns]
        if not available:
            print(f'[{STRATEGY_ID}] no basket tickers in prices', file=sys.stderr)
            return []

        # 12-month return + 60-day realized vol per ticker; keep only LONG candidates
        long_candidates: dict[str, tuple[float, float]] = {}  # ticker → (ret, ann_vol)

        for ticker in available:
            series = prices[ticker].dropna()
            if len(series) < LOOKBACK + 1:
                continue
            try:
                ret_12m = float(series.iloc[-1]) / float(series.iloc[-LOOKBACK]) - 1.0
            except (ZeroDivisionError, ValueError):
                continue
            if ret_12m <= 0:
                continue  # negative momentum → FLAT (no shorting for ETPs)
            if len(series) < VOL_WINDOW + 1:
                continue
            daily_rets = series.pct_change().dropna()
            if len(daily_rets) < VOL_WINDOW:
                continue
            ann_vol = float(daily_rets.iloc[-VOL_WINDOW:].std()) * sqrt(252)
            if ann_vol <= 1e-8:
                continue
            long_candidates[ticker] = (ret_12m, ann_vol)

        if not long_candidates:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Inverse-vol weights normalized to sum=1 across long leg
        inv_vols = {t: 1.0 / v for t, (_, v) in long_candidates.items()}
        total_inv = sum(inv_vols.values())
        weights = {t: iv / total_inv for t, iv in inv_vols.items()}

        # Portfolio vol: sqrt(sum(w_i^2 * vol_i^2)) — zero-correlation lower bound
        # avoids underdetermined covariance with up to 24 assets on a 60-day window
        port_vol = sqrt(
            sum((weights[t] ** 2) * (long_candidates[t][1] ** 2) for t in long_candidates)
        )
        port_vol = max(port_vol, 1e-4)
        leverage = min(LEVERAGE_CAP, TARGET_VOL / port_vol)

        signals: List[Signal] = []
        for ticker, (ret_12m, ann_vol) in long_candidates.items():
            w = weights[ticker]
            pos_size = float(round(w * leverage * scale, 4))
            pos_size = max(0.01, min(pos_size, 0.50))  # floor 1%, cap 50% per asset

            series = prices[ticker].dropna()
            if len(series) < 14:
                continue
            current_price = float(series.iloc[-1])
            stops = self.compute_stops_and_targets(
                series, direction='LONG', current_price=current_price,
                regime_state=regime_state,
            )

            if ret_12m > 0.20:
                confidence = 'HIGH'
            elif ret_12m > 0.05:
                confidence = 'MED'
            else:
                confidence = 'LOW'

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = pos_size,
                confidence        = confidence,
                signal_params     = {
                    'ret_12m':  round(ret_12m, 4),
                    'vol_60d':  round(ann_vol, 4),
                    'weight':   round(w, 4),
                    'leverage': round(leverage, 4),
                    'port_vol': round(port_vol, 4),
                    'regime':   regime_state,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
