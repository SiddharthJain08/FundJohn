"""
SPX 0DTE Opening-Range Breakout — credit spread anchored at the range extreme.
Source: https://blog.quantish.io/2025/09/09/0dte-spx-opening-range-breakouts/

Hypothesis: SPX's first-60-minute range (9:30-10:30 ET) sets directional bias;
a confirmed breakout between 10:30-noon ET predicts the day's drift, monetized
by SELLING a $15-wide 0DTE credit spread at the range extreme, held to expiry.

SPX/^GSPC have no options chain in the master parquet (see
S_ito_signature_vol_hedge) — SPY is the chain-covered proxy; both the
opening-range detection and the option leg run on SPY. Breakout up -> SELL a
put credit spread (short strike = range low, bullish). Breakout down -> SELL
a call credit spread (short strike = range high, bearish).
"""
from __future__ import annotations
import sys
from datetime import time as _time
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal, OptionSpec

__all__ = ['SpxZeroDteOpeningRangeBreakout']

INSTRUMENT_CLASS = 'option'

UNDERLYING       = 'SPY'
OR_START         = _time(9, 30)
OR_END           = _time(10, 30)
BO_END           = _time(12, 0)
MIN_RANGE_PCT    = 0.002   # opening range must be >= 0.2% of open price to trade
SPREAD_WIDTH_USD = 15.0
DTE_TARGET       = 0
MAX_SIZE         = 0.05


class SpxZeroDteOpeningRangeBreakout(BaseStrategy):
    id                = 'S_spx_0dte_opening_range_breakout'
    name              = 'SPX 0DTE Opening Range Breakout'
    description       = (
        "First-60-minute SPX range sets directional bias; a confirmed 10:30-noon "
        "breakout is monetized by selling a $15-wide 0DTE credit spread anchored "
        "at the range extreme, held to expiration for theta capture."
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 14
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'min_range_pct':    MIN_RANGE_PCT,
            'spread_width_usd': SPREAD_WIDTH_USD,
            'base_size':        0.03,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or UNDERLYING not in prices.columns:
            print('[debug] signals=0 (no SPY column)', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL') if isinstance(regime, dict) else 'LOW_VOL'
        if not self.should_run(regime_state):
            print('[debug] signals=0 (regime gate)', file=sys.stderr)
            return []

        p30 = (aux_data or {}).get('prices_30m')
        if p30 is None or getattr(p30, 'empty', True) or 'ticker' not in p30.columns:
            print('[debug] signals=0 (no prices_30m)', file=sys.stderr)
            return []

        spy = p30[p30['ticker'] == UNDERLYING].copy()
        if spy.empty:
            print('[debug] signals=0 (no SPY 30m bars)', file=sys.stderr)
            return []

        try:
            spy['datetime'] = pd.to_datetime(spy['datetime'], utc=True)
            spy = spy.sort_values('datetime')
            spy['et'] = spy['datetime'].dt.tz_convert('America/New_York')
        except Exception:
            print('[debug] signals=0 (datetime parse failed)', file=sys.stderr)
            return []

        last_date = spy['et'].dt.date.iloc[-1]
        today = spy[spy['et'].dt.date == last_date]

        or_bars = today[(today['et'].dt.time >= OR_START) & (today['et'].dt.time < OR_END)]
        if len(or_bars) < 2:
            print('[debug] signals=0 (insufficient opening-range bars)', file=sys.stderr)
            return []

        open_price = float(or_bars['open'].iloc[0])
        range_high = float(or_bars['high'].max())
        range_low  = float(or_bars['low'].min())
        if open_price <= 0:
            print('[debug] signals=0 (bad open price)', file=sys.stderr)
            return []

        range_width = range_high - range_low
        min_range_pct = float(self.parameters.get('min_range_pct', MIN_RANGE_PCT))
        if range_width < min_range_pct * open_price:
            print('[debug] signals=0 (range too narrow)', file=sys.stderr)
            return []

        bo_bars = today[(today['et'].dt.time >= OR_END) & (today['et'].dt.time < BO_END)]
        if bo_bars.empty:
            print('[debug] signals=0 (no breakout-window bars)', file=sys.stderr)
            return []

        breakout_dir, breakout_price = None, None
        for _, row in bo_bars.iterrows():
            if float(row['high']) > range_high:
                breakout_dir, breakout_price = 'up', float(row['high']); break
            if float(row['low']) < range_low:
                breakout_dir, breakout_price = 'down', float(row['low']); break
        if breakout_dir is None:
            print('[debug] signals=0 (no confirmed breakout)', file=sys.stderr)
            return []

        series = prices[UNDERLYING].dropna()
        if len(series) < 14:
            print('[debug] signals=0 (insufficient daily history for ATR)', file=sys.stderr)
            return []
        current_price = float(series.iloc[-1])
        if not (current_price == current_price and current_price > 0):
            print('[debug] signals=0 (bad last close)', file=sys.stderr)
            return []

        edge = min(abs(breakout_price - (range_high if breakout_dir == 'up' else range_low)) /
                   max(range_width, 1e-9), 1.0)
        confidence = 'HIGH' if edge > 0.5 else ('MED' if edge > 0.15 else 'LOW')

        scale     = self.position_scale(regime_state)
        base_size = float(self.parameters.get('base_size', 0.03))
        size      = min(base_size * scale * (1.0 + edge), MAX_SIZE)

        width_usd = float(self.parameters.get('spread_width_usd', SPREAD_WIDTH_USD))
        spread_width_pct = width_usd / current_price

        if breakout_dir == 'up':
            right, moneyness, stop_direction = 'put', range_low / current_price, 'LONG'
        else:
            right, moneyness, stop_direction = 'call', range_high / current_price, 'SHORT'

        stops = self.compute_stops_and_targets(
            series, stop_direction, current_price, atr_multiplier=2.0, regime_state=regime_state,
        )

        option_spec = OptionSpec(
            underlying       = UNDERLYING,
            right            = right,
            strike_rule      = 'fixed_moneyness',
            moneyness        = round(moneyness, 5),
            dte_target       = DTE_TARGET,
            structure        = 'credit_vertical',
            spread_width_pct = round(spread_width_pct, 5),
            hedge            = 'none',
            hold_to_expiry   = True,
        )

        signal = Signal(
            ticker            = UNDERLYING,
            direction         = 'SELL_VOL',
            entry_price       = current_price,
            stop_loss         = stops['stop'],
            target_1          = stops['t1'],
            target_2          = stops['t2'],
            target_3          = stops['t3'],
            position_size_pct = round(size, 4),
            confidence        = confidence,
            signal_params     = {
                'open_price':       round(open_price, 4),
                'range_high':       round(range_high, 4),
                'range_low':        round(range_low, 4),
                'range_width_pct':  round(range_width / open_price, 5),
                'breakout_dir':     breakout_dir,
                'breakout_price':   round(breakout_price, 4),
                'edge':             round(edge, 4),
                'spread_width_usd': width_usd,
                'hold_to_expiry':   True,
                'regime':           regime_state,
            },
            option_spec       = option_spec,
        )

        print('[debug] signals=1', file=sys.stderr)
        return [signal]
