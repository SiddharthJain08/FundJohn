"""
Commodity ETP Momentum — reference implementation for SP-3 instrument_class rails.

Ranks three commodity ETPs (GLD, SLV, USO) by 90-trading-day total return and
emits a single LONG signal for the top-ranked name. Designed as a proof-of-concept
that `instrument_class='etp'` flows end-to-end through the backtest engine; it is
NOT intended as a production alpha strategy.

Basket rationale:
  GLD — SPDR Gold Shares  (gold)
  SLV — iShares Silver Trust (silver)
  USO — United States Oil Fund (crude oil)

All three have ~10 years of price history in data/master/prices.parquet.

State: candidate only. Do NOT promote to live without operator sign-off and
a full coverage-freshness / tradability review.
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['CommodityEtpMomentum']

# Fixed basket — exactly these three commodity ETPs.
BASKET = ('GLD', 'SLV', 'USO')

# Minimum trading days needed to compute a meaningful momentum signal.
LOOKBACK = 90


class CommodityEtpMomentum(BaseStrategy):
    """90-day momentum rotator across GLD, SLV, USO.

    Each day the strategy ranks the three ETPs by trailing 90-trading-day
    total return and emits ONE LONG signal for the top-ranked name.
    Returns empty list if fewer than LOOKBACK days of history are available
    for any basket member, or if none of the tickers appear in the price panel.
    """

    id                = 'S_commodity_etp_momentum'
    name              = 'Commodity ETP Momentum'
    description       = (
        '90-day total-return momentum rotator across commodity ETPs '
        '(GLD / SLV / USO); always long the single strongest name.'
    )
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = LOOKBACK
    # Active in all four canonical regimes — commodities behave differently
    # from equities; suppressing in CRISIS would kill gold signals during
    # the most relevant episodes. Keep all regimes, let regime-scale discount.
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    MAX_SIGNALS = 1

    def default_parameters(self) -> dict:
        return {
            'lookback':       LOOKBACK,   # trailing days for momentum ranking
            'pos_size_frac':  0.10,       # base position size fraction (pre-scale)
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        """Rank BASKET tickers by 90-day return, emit 1 LONG signal for the winner."""
        if prices is None or prices.empty:
            print('[CommodityEtpMomentum] no price data — returning []', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')

        # Identify basket members that are present in the price panel.
        available = [t for t in BASKET if t in prices.columns]
        if not available:
            print('[CommodityEtpMomentum] none of BASKET in prices columns — returning []', file=sys.stderr)
            return []

        lookback = self.parameters.get('lookback', LOOKBACK)

        # Need at least `lookback` rows to compute a meaningful return.
        if len(prices) < lookback:
            print(
                f'[CommodityEtpMomentum] only {len(prices)} rows < {lookback} required — returning []',
                file=sys.stderr,
            )
            return []

        # Compute trailing lookback-day total return for each available ticker.
        momentum: dict[str, float] = {}
        for ticker in available:
            series = prices[ticker].dropna()
            if len(series) < lookback:
                # Not enough non-NaN history for this specific ticker.
                continue
            try:
                ret = float(series.iloc[-1]) / float(series.iloc[-lookback]) - 1.0
            except (ZeroDivisionError, ValueError):
                continue
            momentum[ticker] = ret

        if not momentum:
            print('[CommodityEtpMomentum] no usable momentum scores — returning []', file=sys.stderr)
            return []

        # Pick the highest-momentum ticker.
        winner = max(momentum, key=momentum.__getitem__)
        winner_mom = momentum[winner]

        # Build entry / stop / target bracket exactly as S22 does:
        # call compute_stops_and_targets with ATR-based stops scaled by regime.
        ticker_series = prices[winner].dropna()
        if len(ticker_series) < 14:
            # ATR computation needs at least 14 bars.
            print('[CommodityEtpMomentum] winner series too short for ATR — returning []', file=sys.stderr)
            return []

        current_price = float(ticker_series.iloc[-1])
        stops = self.compute_stops_and_targets(
            ticker_series,
            direction='LONG',
            current_price=current_price,
            regime_state=regime_state,
        )

        # Scale position size by regime (same convention as S22).
        scale    = self.position_scale(regime_state)
        pos_size = round(self.parameters.get('pos_size_frac', 0.10) * scale, 4)

        # Confidence: HIGH if winner momentum > 10%, MED if > 0, LOW otherwise.
        if winner_mom > 0.10:
            confidence = 'HIGH'
        elif winner_mom > 0.0:
            confidence = 'MED'
        else:
            confidence = 'LOW'

        signal = Signal(
            ticker            = winner,
            direction         = 'LONG',
            entry_price       = current_price,
            stop_loss         = stops['stop'],
            target_1          = stops['t1'],
            target_2          = stops['t2'],
            target_3          = stops['t3'],
            position_size_pct = pos_size,
            confidence        = confidence,
            signal_params     = {
                'momentum_90d':   round(winner_mom, 4),
                'basket_scores':  {t: round(v, 4) for t, v in momentum.items()},
                'regime':         regime_state,
                'scale':          scale,
                'lookback':       lookback,
            },
        )

        print(
            f'[CommodityEtpMomentum] winner={winner} mom90d={winner_mom:.3f} '
            f'entry={current_price:.2f} stop={stops["stop"]:.2f} '
            f't1={stops["t1"]:.2f} regime={regime_state}',
            file=sys.stderr,
        )
        return [signal]
