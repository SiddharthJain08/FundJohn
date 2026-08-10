"""
Gold Donchian Breakout EMA Filter — 20-day Donchian channel high breakout
confirmed by EMA trend alignment (fast EMA > slow EMA), with Chandelier
ATR-based trailing stop exit. Targets GLD (SPDR Gold Shares ETP).

Source: "Insane Gold Breakout Strategy: 1.85 Profit Factor Over 28 Years"
        Trading & Investing Strategies, 2026
        https://tradinginvestingstrategies.substack.com/p/gold-breakout-strategy-28-year-backtest

Reported metrics (in-sample, 1998–2026): PF 1.85, win_rate 40.4%,
max_dd 1.38%, 104 trades (~4/year). Overfitting flag: in_sample_only.

instrument_class: etp
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['GoldDonchianBreakoutEmaFilter']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_gold_donchian_breakout_ema_filter'

GOLD_TICKER      = 'GLD'
DONCHIAN_PERIOD  = 20
EMA_FAST_PERIOD  = 20
EMA_SLOW_PERIOD  = 50
CHANDELIER_PERIOD = 22
CHANDELIER_MULT   = 3.0
BASE_POSITION_PCT = 0.10


class GoldDonchianBreakoutEmaFilter(BaseStrategy):
    """Gold breakout momentum: 20-day Donchian high breach + EMA trend alignment, Chandelier ATR stop exit."""

    id                = STRATEGY_ID
    name              = 'Gold Donchian Breakout EMA Filter'
    description       = (
        'Gold exhibits persistent breakout momentum when a 20-day Donchian channel '
        'breach is confirmed by EMA trend alignment, with Chandelier stops capturing '
        'extended trend moves while limiting drawdown.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 60
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'donchian_period':   DONCHIAN_PERIOD,
            'ema_fast_period':   EMA_FAST_PERIOD,
            'ema_slow_period':   EMA_SLOW_PERIOD,
            'chandelier_period': CHANDELIER_PERIOD,
            'chandelier_mult':   CHANDELIER_MULT,
            'pos_size_frac':     BASE_POSITION_PCT,
            'ticker':            GOLD_TICKER,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        """Emit LONG on GLD when Donchian 20-day breakout + EMA uptrend confirmed."""
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        ticker = self.parameters.get('ticker', GOLD_TICKER)
        if ticker not in prices.columns:
            print(f'[{STRATEGY_ID}] {ticker} not in price panel', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        series = prices[ticker].dropna()

        donchian_p   = int(self.parameters.get('donchian_period', DONCHIAN_PERIOD))
        ema_fast_p   = int(self.parameters.get('ema_fast_period', EMA_FAST_PERIOD))
        ema_slow_p   = int(self.parameters.get('ema_slow_period', EMA_SLOW_PERIOD))
        chandelier_p = int(self.parameters.get('chandelier_period', CHANDELIER_PERIOD))
        chandelier_m = float(self.parameters.get('chandelier_mult', CHANDELIER_MULT))

        min_bars = max(donchian_p, ema_slow_p, chandelier_p) + 5
        if len(series) < min_bars:
            print(f'[{STRATEGY_ID}] insufficient bars {len(series)} < {min_bars}', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        current_close = float(series.iloc[-1])

        # Donchian breakout: close > highest close of prior donchian_p bars
        prior = series.iloc[-(donchian_p + 1):-1]
        if prior.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []
        prev_high = float(prior.max())
        donchian_breakout = current_close > prev_high

        # EMA trend alignment: fast EMA > slow EMA
        ema_fast = float(series.ewm(span=ema_fast_p, adjust=False).mean().iloc[-1])
        ema_slow = float(series.ewm(span=ema_slow_p, adjust=False).mean().iloc[-1])
        ema_trend_up = ema_fast > ema_slow

        if not (donchian_breakout and ema_trend_up):
            print(
                f'[{STRATEGY_ID}] no entry: breakout={donchian_breakout} ema_up={ema_trend_up}',
                file=sys.stderr,
            )
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Chandelier stop: rolling_high(chandelier_p) - mult * ATR(chandelier_p)
        rolling_high = float(series.iloc[-chandelier_p:].max())
        atr_raw = series.diff().abs().rolling(chandelier_p).mean().iloc[-1]
        atr_val = float(atr_raw) if (pd.notna(atr_raw) and float(atr_raw) > 0) else current_close * 0.02
        chandelier_stop = rolling_high - chandelier_m * atr_val

        # Baseline ATR stop + targets from base class
        stops = self.compute_stops_and_targets(
            series,
            direction='LONG',
            current_price=current_close,
            regime_state=regime_state,
        )
        # Use Chandelier stop if tighter (higher value = closer to price = more protective)
        stop_loss = float(round(max(chandelier_stop, stops['stop']), 4))

        scale    = self.position_scale(regime_state)
        pos_size = round(float(self.parameters.get('pos_size_frac', BASE_POSITION_PCT)) * scale, 4)

        breakout_pct = (current_close - prev_high) / prev_high if prev_high > 0 else 0.0
        if breakout_pct > 0.01:
            confidence = 'HIGH'
        elif breakout_pct > 0.003:
            confidence = 'MED'
        else:
            confidence = 'LOW'

        signal = Signal(
            ticker            = ticker,
            direction         = 'LONG',
            entry_price       = float(round(current_close, 4)),
            stop_loss         = stop_loss,
            target_1          = stops['t1'],
            target_2          = stops['t2'],
            target_3          = stops['t3'],
            position_size_pct = pos_size,
            confidence        = confidence,
            signal_params     = {
                'donchian_20d_high': float(round(prev_high, 4)),
                'breakout_pct':      float(round(breakout_pct, 4)),
                'ema_fast':          float(round(ema_fast, 4)),
                'ema_slow':          float(round(ema_slow, 4)),
                'chandelier_stop':   float(round(chandelier_stop, 4)),
                'atr':               float(round(atr_val, 4)),
                'regime':            regime_state,
                'scale':             scale,
            },
        )

        print(
            f'[{STRATEGY_ID}] LONG {ticker} entry={current_close:.2f} '
            f'stop={stop_loss:.2f} breakout_pct={breakout_pct:.3f} '
            f'ema_fast={ema_fast:.2f} ema_slow={ema_slow:.2f} regime={regime_state}',
            file=sys.stderr,
        )
        print('[debug] signals=1', file=sys.stderr)
        return [signal]
