"""
Time Series Momentum Effect (Moskowitz, Ooi & Pedersen 2012).

Source: https://quantpedia.com/strategies/time-series-momentum-effect/

Clean-room ETF proxy of the original futures-based strategy. Universe: up to 24 daily-bar
ETFs covering commodities, FX, equity indexes, and bonds. Monthly rebalance: go LONG on
assets with positive trailing 12-month return, sized inversely to 60-day realized
volatility, scaled to target 10% annualized portfolio volatility. Individual leverage
capped at 4×.

Note: original paper uses commodity/FX/equity-index/bond futures (continuous contracts)
not available in our stack. ETF proxies substitute per strategy_spec.
"""
from __future__ import annotations

import math
import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['AstTimeSeriesMomentumEffect']

INSTRUMENT_CLASS = "etp"

STRATEGY_ID = 'S_ast_time_series_momentum_effect'

# 24-instrument ETF proxy basket: commodity, FX, equity-index, bond
BASKET = [
    # Commodity ETFs
    'GLD', 'SLV', 'USO', 'DBC', 'PDBC',
    # Currency ETFs
    'UUP', 'FXE', 'FXY', 'FXB', 'FXA', 'FXC', 'FXF', 'FXS',
    # Equity-index ETFs
    'SPY', 'EFA', 'EWG', 'EWJ', 'EWU', 'EWL', 'EWQ',
    # Bond ETFs
    'TLT', 'IEF', 'SHY', 'BNDX', 'BWX',
]

LOOKBACK     = 252   # ~12 months of trading days
VOL_PERIOD   = 60    # rolling days for realized vol estimate
TARGET_VOL   = 0.10  # annualized portfolio volatility target
LEVERAGE_CAP = 4.0   # individual leverage cap


class AstTimeSeriesMomentumEffect(BaseStrategy):
    """Monthly time-series momentum across 24 ETF proxies, vol-targeted at 10% p.a.

    Source: Moskowitz, Ooi & Pedersen (2012) — ETF proxy per strategy_spec.
    Each month: LONG assets with positive trailing 12M return, weighted
    inversely to 60-day realized vol, leverage scaled to target 10% portfolio vol.
    """

    id                = STRATEGY_ID
    name              = 'Time Series Momentum Effect (ETP)'
    description       = (
        'Monthly TSMOM across 24 commodity/FX/equity-index/bond ETF proxies: '
        'long assets with positive 12M return, inverse-vol sized, 10% vol target.'
    )
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = LOOKBACK
    # All-weather: TSMOM works in trending (LOW_VOL) and crisis (flight-to-quality
    # in bonds/gold) as well as intermediate regimes.
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {
            'lookback':     LOOKBACK,
            'vol_period':   VOL_PERIOD,
            'target_vol':   TARGET_VOL,
            'leverage_cap': LEVERAGE_CAP,
        }

    def _is_month_boundary(self, prices: pd.DataFrame) -> bool:
        """True if the last bar is the first trading day of a new month."""
        if not isinstance(prices.index, pd.DatetimeIndex) or len(prices) < 2:
            return True  # can't tell — allow through so backtests work
        return prices.index[-1].month != prices.index[-2].month

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Monthly rebalance only
        if not self._is_month_boundary(prices):
            print('[debug] signals=0', file=sys.stderr)
            return []

        lookback   = int(self.parameters.get('lookback',     LOOKBACK))
        vol_period = int(self.parameters.get('vol_period',   VOL_PERIOD))
        target_vol = float(self.parameters.get('target_vol', TARGET_VOL))
        lev_cap    = float(self.parameters.get('leverage_cap', LEVERAGE_CAP))

        if len(prices) < lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Filter to basket tickers present in the price panel
        available = [t for t in BASKET if t in prices.columns]
        if not available:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Compute 12M return and 60-day annualized vol per ticker
        stats: dict[str, tuple[float, float]] = {}  # ticker -> (ret_12m, vol_60d)
        for ticker in available:
            series = prices[ticker].dropna()
            if len(series) < lookback:
                continue
            p_start = float(series.iloc[-lookback])
            p_end   = float(series.iloc[-1])
            if p_start <= 0 or p_end <= 0:
                continue
            ret12m = p_end / p_start - 1.0
            vols   = min(vol_period, len(series) - 1)
            if vols < 5:
                continue
            daily_rets = series.pct_change().dropna().iloc[-vols:]
            vol60d = float(daily_rets.std()) * math.sqrt(252)
            if vol60d <= 0:
                continue
            stats[ticker] = (ret12m, vol60d)

        # Only enter LONG on assets with positive trailing 12M return
        longs = {t: v for t, v in stats.items() if v[0] > 0}
        if not longs:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Inverse-vol weights (normalized to sum to 1)
        inv_vols    = {t: 1.0 / v[1] for t, v in longs.items()}
        total_iv    = sum(inv_vols.values())
        weights     = {t: iv / total_iv for t, iv in inv_vols.items()}

        # Portfolio vol estimate assuming zero correlation (avoids underdetermined
        # covariance matrix: up to 24 assets, only 60 observations).
        port_vol = math.sqrt(sum(
            (w ** 2) * (longs[t][1] ** 2) for t, w in weights.items()
        ))
        if port_vol <= 0:
            port_vol = target_vol

        leverage = min(lev_cap, target_vol / port_vol)
        scale    = self.position_scale(regime_state)

        signals: List[Signal] = []
        for ticker, w in weights.items():
            series        = prices[ticker].dropna()
            current_price = float(series.iloc[-1])
            stops = self.compute_stops_and_targets(
                series,
                direction='LONG',
                current_price=current_price,
                regime_state=regime_state,
            )
            ret12m = longs[ticker][0]
            vol60d = longs[ticker][1]
            pos_size = min(round(w * leverage * scale, 4), 1.0)

            if ret12m > 0.20:
                confidence = 'HIGH'
            elif ret12m > 0.05:
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
                    'ret_12m':  round(ret12m, 4),
                    'vol_60d':  round(vol60d, 4),
                    'weight':   round(w, 4),
                    'leverage': round(leverage, 4),
                    'regime':   regime_state,
                    'scale':    round(scale, 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
