"""
BTC Donchian Breakout, Realistic Costs — port of "A Bitcoin Trend System Made
$5,380 on Unseen Years. We Still Won't Trade It." (yuratrading.com/blogs/
btc-donchian-trend, 2026).

Source finding: a classic Donchian-channel breakout on BTC still cleared
+$5,380 over 4 out-of-sample years once the authors swapped the data
vendor's quoted $50-115 spread for a realistic ~$5 broker spread, but it
failed 3 of their own quality checks and carried a ~30% realistic
drawdown, so they declined to trade it live.

Interpretation variant (1 of 2) — the paper's pseudocode
(`upper=rolling_max(high,N); lower=rolling_min(low,N); close>upper[t-1]-> LONG;
close<lower[t-1]-> SHORT/EXIT`) leaves the channel period N and the realistic
cost treatment unspecified. Deliberate choices for THIS variant (a colleague
porting the other variant should land on different numbers):
  - N = 20 (Turtle System-1 fast entry window), not the slower 55-day
    channel a colleague might reach for.
  - No true BTC OHLC column is declared in this system's data requirements
    (only close), so the channel is built directly off rolling max/min of
    CLOSE itself rather than synthesizing an HL2-style high/low proxy (the
    choice `S_btc_supertrend_naive_flip_tv2` made for its own indicator) —
    a plainer, differently-biased stand-in for the paper's true-range
    channel.
  - "Realistic costs" is encoded as a basis-point breakout margin filter
    rather than a backtest-only commission line: a breakout only fires if
    it clears the prior band by more than `COST_BUFFER_BPS` (10bps,
    representing a conservative round-trip spread+taker-fee estimate).
    A fixed dollar figure (the paper's $5) is not reusable across this
    system's 2017-2025 backtest window, over which BTC has moved roughly
    two orders of magnitude in price, so the buffer is expressed as a
    price-relative fraction instead.
  - instrument_class=crypto has no short rail, so the paper's lower-band
    SHORT leg is mapped to FLAT (forced by the system contract, not a free
    interpretive choice) — identical treatment to the other BTC ports in
    this tree.
  - Channel state (LONG vs FLAT) persists across bars between breakouts —
    a Donchian system is inherently a trend-following state machine, not a
    same-bar-only crossing signal.
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['BtcDonchianBreakoutRealisticCosts']

INSTRUMENT_CLASS = 'crypto'

TICKER          = 'BTC-USD'
CHANNEL_PERIOD  = 20       # Turtle System-1 fast entry window
COST_BUFFER_BPS = 0.0010   # 10bps realistic round-trip spread+fee buffer
MIN_LOOKBACK    = 504      # system floor for crypto backtest qualification
BASE_SIZE       = 0.08     # base gross fraction (pre regime-scale)


class BtcDonchianBreakoutRealisticCosts(BaseStrategy):
    """Donchian(20) breakout on BTC-USD close, gated by a realistic-cost
    breakout margin filter; LONG on upper breakout, FLAT on lower breakout
    (no crypto short rail). Port of a paper that found the edge survives a
    realistic broker spread but still fails most of the authors' own
    quality checks."""

    id                = 'S_btc_donchian_breakout_realistic_costs'
    name              = 'BTC Donchian Breakout (Realistic Costs)'
    description       = (
        'Donchian(20) channel breakout on BTC-USD close with a 10bps realistic-cost '
        'breakout margin filter; LONG above the upper band, FLAT below the lower band.'
    )
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = MIN_LOOKBACK
    instrument_class  = 'crypto'
    active_in_regimes = ['HIGH_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'channel_period':  CHANNEL_PERIOD,
            'cost_buffer_bps': COST_BUFFER_BPS,
            'base_size':       BASE_SIZE,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or TICKER not in prices.columns:
            print('[BtcDonchianBreakoutRealisticCosts] no BTC-USD column -> signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[BtcDonchianBreakoutRealisticCosts] should_run=False -> signals=0', file=sys.stderr)
            return []

        close  = prices[TICKER].dropna()
        period = int(self.parameters.get('channel_period', CHANNEL_PERIOD))
        buffer = float(self.parameters.get('cost_buffer_bps', COST_BUFFER_BPS))
        if len(close) < max(MIN_LOOKBACK, period + 2):
            print('[BtcDonchianBreakoutRealisticCosts] insufficient history -> signals=0', file=sys.stderr)
            return []

        upper = close.rolling(period, min_periods=period).max()
        lower = close.rolling(period, min_periods=period).min()
        upper_prev = upper.shift(1)
        lower_prev = lower.shift(1)

        first_valid = upper_prev.first_valid_index()
        if first_valid is None:
            print('[BtcDonchianBreakoutRealisticCosts] channel never valid -> signals=0', file=sys.stderr)
            return []
        start_i = close.index.get_loc(first_valid)

        # Stateful trend walk: a breakout flips the trend and it persists
        # until the opposite band is cleared (net of the cost buffer).
        trend = 'FLAT'
        last_margin = 0.0
        for i in range(start_i, len(close)):
            c_today = close.iloc[i]
            up_prev = upper_prev.iloc[i]
            lo_prev = lower_prev.iloc[i]
            if pd.isna(up_prev) or pd.isna(lo_prev):
                continue
            if c_today > up_prev * (1.0 + buffer):
                trend = 'LONG'
                last_margin = float((c_today / up_prev) - 1.0)
            elif c_today < lo_prev * (1.0 - buffer):
                trend = 'FLAT'
                last_margin = float(1.0 - (c_today / lo_prev))
            # else: inside both buffered bands -> hold prior trend

        if trend != 'LONG':
            print('[BtcDonchianBreakoutRealisticCosts] trend=FLAT -> signals=0', file=sys.stderr)
            return []

        current_price = float(close.iloc[-1])
        scale    = self.position_scale(regime_state)
        pos_size = round(float(self.parameters.get('base_size', BASE_SIZE)) * scale, 4)
        if pos_size < 0.001:
            print('[BtcDonchianBreakoutRealisticCosts] pos_size<0.001 -> signals=0', file=sys.stderr)
            return []

        st = self.compute_stops_and_targets(
            close, direction='LONG', current_price=current_price, regime_state=regime_state)

        confidence = 'HIGH' if last_margin > 3 * buffer else ('MED' if last_margin > buffer else 'LOW')

        sig = Signal(
            ticker            = TICKER,
            direction         = 'LONG',
            entry_price       = current_price,
            stop_loss         = float(st['stop']),
            target_1          = float(st['t1']),
            target_2          = float(st['t2']),
            target_3          = float(st['t3']),
            position_size_pct = pos_size,
            confidence        = confidence,
            signal_params     = {
                'channel_period':  period,
                'cost_buffer_bps': buffer,
                'breakout_margin': round(last_margin, 5),
                'upper_band':      round(float(upper.iloc[-1]), 2) if pd.notna(upper.iloc[-1]) else None,
                'lower_band':      round(float(lower.iloc[-1]), 2) if pd.notna(lower.iloc[-1]) else None,
                'regime':          regime_state,
                'variant':         'close_only_channel_cost_buffer',
            },
        )
        print(f'[BtcDonchianBreakoutRealisticCosts] trend=LONG margin={last_margin:.5f} '
              f'size={pos_size} regime={regime_state}', file=sys.stderr)
        return [sig]
