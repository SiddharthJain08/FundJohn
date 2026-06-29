"""
WTI-Brent Crude Oil Spread Mean Reversion via USO/BNO ETF proxies.

Source: https://quantpedia.com/strategies/trading-wti-brent-spread/
Ported from QuantConnect LEAN implementation (clean-room re-implementation).

Daily: compute spread = USO_price - BNO_price. Maintain a 20-bar rolling SMA.
If spread > SMA20: SHORT USO (WTI proxy), LONG BNO (Brent proxy) — fade the elevated spread.
If spread < SMA20: LONG USO, SHORT BNO — fade the depressed spread.
Flat when data is stale or insufficient history exists.

NOTE: The original strategy used 5x leverage on continuous futures (ICE_WT1/ICE_B1).
This implementation uses USO and BNO as ETF proxies without leverage, which alters
spread dynamics. Treat as an approximation only.
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['WtiBrentSpreadMeanReversion']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID = 'S_wti_brent_spread_mean_reversion'

WTI_TICKER = 'USO'    # United States Oil Fund — WTI crude proxy
BRENT_TICKER = 'BNO'  # United States Brent Oil Fund — Brent crude proxy
LOOKBACK = 20
STALE_DAYS = 5
BASE_POS_SIZE = 0.15  # 15% per leg (pre-regime-scale); 2 legs = ~30% gross


class WtiBrentSpreadMeanReversion(BaseStrategy):
    """WTI-Brent crude oil spread mean reversion via USO/BNO ETF proxies.

    Computes the USO-BNO price spread and fades deviations from its 20-day SMA.
    Emits two signals per bar (one per leg): SHORT/LONG USO + reciprocal on BNO.
    """

    id                = STRATEGY_ID
    name              = 'WTI-Brent Spread Mean Reversion'
    description       = (
        'Fade USO/BNO spread deviations from their 20-day SMA; '
        'exit on reversion to fair value.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = LOOKBACK
    # Mean-reversion edge; avoid CRISIS where spread dislocations can persist.
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 2

    def default_parameters(self) -> dict:
        return {
            'lookback':      LOOKBACK,
            'stale_days':    STALE_DAYS,
            'pos_size_frac': BASE_POS_SIZE,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0 — empty prices', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print(f'[debug] signals=0 — regime {regime_state} not in active_in_regimes', file=sys.stderr)
            return []

        lookback      = int(self.parameters.get('lookback', LOOKBACK))
        stale_days    = int(self.parameters.get('stale_days', STALE_DAYS))
        pos_size_frac = float(self.parameters.get('pos_size_frac', BASE_POS_SIZE))

        # Both legs required.
        if WTI_TICKER not in prices.columns or BRENT_TICKER not in prices.columns:
            print(
                f'[debug] signals=0 — {WTI_TICKER} or {BRENT_TICKER} missing from prices',
                file=sys.stderr,
            )
            return []

        # Data freshness: if both legs have been NaN for stale_days consecutive rows, flat.
        uso_raw = prices[WTI_TICKER]
        bno_raw = prices[BRENT_TICKER]
        tail = min(stale_days, len(prices))
        if uso_raw.iloc[-tail:].isna().all() or bno_raw.iloc[-tail:].isna().all():
            print('[debug] signals=0 — stale data detected', file=sys.stderr)
            return []

        # Align both series on common non-NaN dates to compute spread.
        combined = pd.concat(
            [uso_raw.rename('uso'), bno_raw.rename('bno')], axis=1
        ).dropna()

        if len(combined) < lookback:
            print(
                f'[debug] signals=0 — only {len(combined)} aligned bars < {lookback} required',
                file=sys.stderr,
            )
            return []

        spread   = combined['uso'] - combined['bno']
        sma20    = spread.rolling(lookback).mean()
        spread_std = spread.rolling(lookback).std()

        if pd.isna(sma20.iloc[-1]):
            print('[debug] signals=0 — SMA not ready', file=sys.stderr)
            return []

        current_spread = float(spread.iloc[-1])
        current_sma    = float(sma20.iloc[-1])
        uso_price      = float(combined['uso'].iloc[-1])
        bno_price      = float(combined['bno'].iloc[-1])

        # Z-score for confidence tier.
        std_val = float(spread_std.iloc[-1]) if pd.notna(spread_std.iloc[-1]) else 0.0
        z_score = abs(current_spread - current_sma) / std_val if std_val > 0 else 0.0
        if z_score > 1.5:
            confidence = 'HIGH'
        elif z_score > 0.5:
            confidence = 'MED'
        else:
            confidence = 'LOW'

        scale    = self.position_scale(regime_state)
        pos_size = round(pos_size_frac * scale, 4)

        common_params = {
            'spread':      round(current_spread, 4),
            'sma20':       round(current_sma, 4),
            'z_score':     round(z_score, 3),
            'regime':      regime_state,
        }

        signals: List[Signal] = []

        if current_spread > current_sma:
            # Spread elevated → SHORT USO (WTI), LONG BNO (Brent).
            uso_st = self.compute_stops_and_targets(
                combined['uso'], 'SHORT', uso_price, regime_state=regime_state
            )
            bno_st = self.compute_stops_and_targets(
                combined['bno'], 'LONG',  bno_price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=WTI_TICKER, direction='SHORT',
                entry_price=uso_price, stop_loss=uso_st['stop'],
                target_1=uso_st['t1'], target_2=uso_st['t2'], target_3=uso_st['t3'],
                position_size_pct=pos_size, confidence=confidence,
                signal_params={**common_params, 'leg': 'wti_short'},
            ))
            signals.append(Signal(
                ticker=BRENT_TICKER, direction='LONG',
                entry_price=bno_price, stop_loss=bno_st['stop'],
                target_1=bno_st['t1'], target_2=bno_st['t2'], target_3=bno_st['t3'],
                position_size_pct=pos_size, confidence=confidence,
                signal_params={**common_params, 'leg': 'brent_long'},
            ))

        elif current_spread < current_sma:
            # Spread depressed → LONG USO (WTI), SHORT BNO (Brent).
            uso_st = self.compute_stops_and_targets(
                combined['uso'], 'LONG',  uso_price, regime_state=regime_state
            )
            bno_st = self.compute_stops_and_targets(
                combined['bno'], 'SHORT', bno_price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=WTI_TICKER, direction='LONG',
                entry_price=uso_price, stop_loss=uso_st['stop'],
                target_1=uso_st['t1'], target_2=uso_st['t2'], target_3=uso_st['t3'],
                position_size_pct=pos_size, confidence=confidence,
                signal_params={**common_params, 'leg': 'wti_long'},
            ))
            signals.append(Signal(
                ticker=BRENT_TICKER, direction='SHORT',
                entry_price=bno_price, stop_loss=bno_st['stop'],
                target_1=bno_st['t1'], target_2=bno_st['t2'], target_3=bno_st['t3'],
                position_size_pct=pos_size, confidence=confidence,
                signal_params={**common_params, 'leg': 'brent_short'},
            ))
        # If spread == sma exactly (degenerate), emit nothing.

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
