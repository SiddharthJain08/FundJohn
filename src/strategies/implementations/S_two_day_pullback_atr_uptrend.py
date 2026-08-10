"""
Two-Day Pullback ATR Uptrend — two consecutive red candles with a pullback
exceeding one average daily range (20-bar ATR proxy), filtered to uptrend
conditions (close > 200-bar SMA), exploits mean-reversion in liquid ETFs
(SPY/QQQ) as short-term oversold dips snap back within ~3 bars.

Source: "New Trading Strategy: 76.84% Win Rate on QQQ Since 1999"
        Trading & Investing Strategies, 2026
        https://tradinginvestingstrategies.substack.com/p/2-day-pullback-trading-strategy

Reported metrics (in-sample): win_rate 76.84% (QQQ), 73.23% (SPY),
PF 2.27 (QQQ), 1.89 (SPY), max_dd 2.09%, avg hold ~3 bars.
Overfitting flags: in_sample_only, no_transaction_costs.

instrument_class: etp
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['TwoDayPullbackAtrUptrend']

INSTRUMENT_CLASS   = 'etp'
STRATEGY_ID        = 'S_two_day_pullback_atr_uptrend'

ETP_TICKERS        = ['SPY', 'QQQ']
ATR_PERIOD         = 20      # bars for average daily range proxy
TREND_SMA_PERIOD   = 200     # long-term uptrend filter
PULLBACK_THRESHOLD = 1.0     # minimum pullback in ATR units to trigger entry
BASE_POSITION_PCT  = 0.10    # 10% equity per signal (as per spec)


class TwoDayPullbackAtrUptrend(BaseStrategy):
    """Two consecutive down-closes > 1 ATR pullback in SPY/QQQ uptrends — mean-reversion snap-back."""

    id                = STRATEGY_ID
    name              = 'Two-Day Pullback ATR Uptrend'
    description       = (
        'A two-consecutive-day pullback exceeding one average daily range (20-bar ATR normalized), '
        'filtered to uptrend conditions, exploits mean-reversion in liquid ETFs as short-term '
        'oversold dips snap back within ~3 bars.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 252
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 2   # at most one signal per ETP ticker

    def default_parameters(self) -> dict:
        return {
            'atr_period':         ATR_PERIOD,
            'trend_sma_period':   TREND_SMA_PERIOD,
            'pullback_threshold': PULLBACK_THRESHOLD,
            'pos_size_frac':      BASE_POSITION_PCT,
            'tickers':            ETP_TICKERS,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        """Emit LONG for each ETP that satisfies 2-day pullback + ATR + uptrend conditions."""
        if prices is None or prices.empty:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        atr_p         = int(self.parameters.get('atr_period', ATR_PERIOD))
        trend_p       = int(self.parameters.get('trend_sma_period', TREND_SMA_PERIOD))
        pb_threshold  = float(self.parameters.get('pullback_threshold', PULLBACK_THRESHOLD))
        pos_size_frac = float(self.parameters.get('pos_size_frac', BASE_POSITION_PCT))
        tickers       = self.parameters.get('tickers', ETP_TICKERS)

        min_bars = trend_p + 5
        scale    = self.position_scale(regime_state)
        signals: List[Signal] = []

        for ticker in tickers:
            if ticker not in prices.columns:
                print(f'[{STRATEGY_ID}] {ticker} not in price panel', file=sys.stderr)
                continue

            series = prices[ticker].dropna()
            if len(series) < min_bars:
                print(
                    f'[{STRATEGY_ID}] {ticker}: insufficient bars {len(series)} < {min_bars}',
                    file=sys.stderr,
                )
                continue

            # today / yesterday / day-before-yesterday
            c0 = float(series.iloc[-1])   # today's close
            c1 = float(series.iloc[-2])   # yesterday's close
            c2 = float(series.iloc[-3])   # two days ago

            # Gate 1: two consecutive red candles
            two_red = (c1 < c2) and (c0 < c1)
            if not two_red:
                continue

            # Gate 2: pullback strength > threshold in ATR units
            # ATR proxy = mean(|daily diff|) over last atr_p bars
            daily_range = series.diff().abs()
            atr_raw     = daily_range.rolling(atr_p).mean().iloc[-1]
            atr_val     = float(atr_raw) if (pd.notna(atr_raw) and float(atr_raw) > 0) else c0 * 0.01
            pullback_strength = (c2 - c0) / atr_val   # total 2-day drop in ATR units
            if pullback_strength < pb_threshold:
                continue

            # Gate 3: price above long-term SMA (uptrend filter)
            sma_200 = float(series.rolling(trend_p).mean().iloc[-1])
            if pd.isna(sma_200) or c0 <= sma_200:
                continue

            # All gates passed — compute stops/targets from base class
            stops = self.compute_stops_and_targets(
                series,
                direction='LONG',
                current_price=c0,
                regime_state=regime_state,
            )

            pos_size = float(round(pos_size_frac * scale, 4))

            # Confidence tier: larger dip → higher mean-reversion edge
            if pullback_strength >= 2.0:
                confidence = 'HIGH'
            elif pullback_strength >= 1.5:
                confidence = 'MED'
            else:
                confidence = 'LOW'

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = float(round(c0, 4)),
                stop_loss         = float(round(stops['stop'], 4)),
                target_1          = float(round(stops['t1'], 4)),
                target_2          = float(round(stops['t2'], 4)),
                target_3          = float(round(stops['t3'], 4)),
                position_size_pct = pos_size,
                confidence        = confidence,
                signal_params     = {
                    'pullback_strength': float(round(pullback_strength, 4)),
                    'atr_val':           float(round(atr_val, 4)),
                    'sma_200':           float(round(sma_200, 4)),
                    'close_t0':          float(round(c0, 4)),
                    'close_t1':          float(round(c1, 4)),
                    'close_t2':          float(round(c2, 4)),
                    'regime':            regime_state,
                    'scale':             scale,
                },
            ))
            print(
                f'[{STRATEGY_ID}] LONG {ticker} entry={c0:.2f} stop={stops["stop"]:.2f} '
                f'pullback_str={pullback_strength:.2f}x regime={regime_state}',
                file=sys.stderr,
            )

        n = len(signals)
        print(f'[debug] signals={n}', file=sys.stderr)
        return signals
