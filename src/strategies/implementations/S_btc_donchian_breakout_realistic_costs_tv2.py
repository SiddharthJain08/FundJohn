"""
BTC Donchian Breakout, Realistic Costs (TV2) — port of "A Bitcoin Trend
System Made $5,380 on Unseen Years. We Still Won't Trade It."
(yuratrading.com/blogs/btc-donchian-trend, 2026).

Source finding: a classic Donchian-channel breakout on BTC still cleared
+$5,380 over 4 out-of-sample years once the authors swapped the data
vendor's quoted $50-115 spread for a realistic ~$5 broker spread, but it
failed 3 of their own quality checks and carried a ~30% realistic
drawdown, so they declined to trade it live.

Interpretation variant (2 of 2) — the paper's pseudocode
(`upper=rolling_max(high,N); lower=rolling_min(low,N); close>upper[t-1]->LONG;
close<lower[t-1]->SHORT/EXIT`) leaves the channel period N, the
entry/confirmation rule, and the "realistic cost" treatment unspecified.
This variant deliberately lands on DIFFERENT choices than variant 1 on
every axis that paper left open:

  - N = 55 (Turtle System-2 slow channel), not variant 1's 20-day fast
    window — the other half of the classic two-system Turtle pairing.
  - Entry requires CONFIRM_BARS=3 consecutive closes above the rolling
    upper band before a LONG fires, instead of flipping state on the
    single breakout bar. Rationale: "realistic" execution can't react on
    the exact print that crosses the band (order lag, the vendor-vs-$5-
    spread gap the source paper is built around), so this variant treats
    the confirmation delay itself as part of the realistic-cost framing,
    not a separate bps filter. Exit stays single-bar (close below the
    rolling lower band) — fast risk-off, slow risk-on is the deliberately
    asymmetric choice here.
  - No indefinite state machine: unlike variant 1 (which walks the whole
    history and lets a trend persist until the opposite band breach,
    however long ago), this variant re-derives LONG/FLAT fresh from only
    the trailing CONFIRM_BARS+1 window every call. A multi-year-old
    breakout with no recent confirmation does not keep the book long.
  - "Realistic costs" is encoded as a SIZING haircut, not an entry veto:
    variant 1 filtered entries behind a flat 10bps breakout-margin buffer.
    This variant instead estimates a realistic ATR-based round-trip cost
    fraction and compares it to how far the confirmed breakout has run;
    a thin edge relative to realistic cost shrinks position_size_pct
    toward a floor rather than suppressing the signal outright — the
    system still goes long on a marginal breakout, just smaller, which is
    closer to how a desk would actually size a "fails our own quality
    checks" edge than refusing to trade it.
  - instrument_class=crypto has no short rail, so the paper's lower-band
    SHORT leg is mapped to FLAT (forced by the system contract, not a
    free interpretive choice) — identical treatment to variant 1 and the
    other BTC ports in this tree.
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal
from strategies.indicators import atr as _atr

__all__ = ['BtcDonchianBreakoutRealisticCostsTv2']

INSTRUMENT_CLASS = 'crypto'
STRATEGY_ID      = 'S_btc_donchian_breakout_realistic_costs_tv2'

TICKER             = 'BTC-USD'
CHANNEL_PERIOD      = 55      # Turtle System-2 slow entry window
CONFIRM_BARS         = 3      # consecutive confirmed closes required to enter LONG
MIN_LOOKBACK         = 504    # system floor for crypto backtest qualification
BASE_SIZE            = 0.08   # base gross fraction (pre regime-scale, pre cost haircut)
COST_ATR_MULT        = 2.0    # ATR multiplier approximating a realistic round-trip cost
MIN_SIZE_FACTOR      = 0.25   # floor on the cost-haircut sizing multiplier


class BtcDonchianBreakoutRealisticCostsTv2(BaseStrategy):
    """Donchian(55) breakout on BTC-USD close requiring 3 consecutive
    confirmed closes above the upper band before entering LONG (fast
    single-bar exit below the lower band); no persistent state machine,
    position is re-derived from the trailing confirmation window each
    call. "Realistic costs" is modeled as an ATR-based sizing haircut on
    thin breakouts rather than an entry filter. No crypto short rail."""

    id                = STRATEGY_ID
    name              = 'BTC Donchian Breakout (Realistic Costs, TV2)'
    description       = (
        'Donchian(55) breakout on BTC-USD close requiring 3-bar confirmation before '
        'LONG entry, single-bar exit below the lower band; realistic-cost ATR sizing haircut.'
    )
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = MIN_LOOKBACK
    instrument_class  = 'crypto'
    active_in_regimes = ['HIGH_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'channel_period': CHANNEL_PERIOD,
            'confirm_bars':   CONFIRM_BARS,
            'base_size':      BASE_SIZE,
            'cost_atr_mult':  COST_ATR_MULT,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or TICKER not in prices.columns:
            print('[BtcDonchianBreakoutRealisticCostsTv2] no BTC-USD column -> signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[BtcDonchianBreakoutRealisticCostsTv2] should_run=False -> signals=0', file=sys.stderr)
            return []

        close   = prices[TICKER].dropna()
        period  = int(self.parameters.get('channel_period', CHANNEL_PERIOD))
        confirm = int(self.parameters.get('confirm_bars', CONFIRM_BARS))
        if len(close) < max(MIN_LOOKBACK, period + confirm + 1):
            print('[BtcDonchianBreakoutRealisticCostsTv2] insufficient history -> signals=0', file=sys.stderr)
            return []

        upper      = close.rolling(period, min_periods=period).max()
        lower      = close.rolling(period, min_periods=period).min()
        upper_prev = upper.shift(1)
        lower_prev = lower.shift(1)

        # Fast exit: if the most recent close breached the lower band, flat.
        if pd.notna(lower_prev.iloc[-1]) and close.iloc[-1] < lower_prev.iloc[-1]:
            print('[BtcDonchianBreakoutRealisticCostsTv2] lower-band breach -> FLAT', file=sys.stderr)
            return []

        # Slow entry: require the trailing `confirm` closes to each sit
        # above their own rolling upper band (no persistence beyond this
        # window — an old, unconfirmed breakout does not carry forward).
        tail_close = close.tail(confirm)
        tail_upper = upper_prev.tail(confirm)
        if tail_upper.isna().any():
            print('[BtcDonchianBreakoutRealisticCostsTv2] confirmation window not fully valid -> signals=0', file=sys.stderr)
            return []
        confirmed = bool((tail_close.values > tail_upper.values).all())
        if not confirmed:
            print('[BtcDonchianBreakoutRealisticCostsTv2] confirmation not met -> signals=0', file=sys.stderr)
            return []

        current_price = float(close.iloc[-1])
        entry_band    = float(upper_prev.iloc[-1])

        # Realistic-cost sizing haircut: compare the confirmed breakout's
        # run beyond the band to an ATR-based realistic round-trip cost
        # estimate. Thin edges shrink toward the floor instead of being
        # vetoed outright.
        atr_series = _atr(close, 14, method='close_diff', fallback_pct=0.02)
        atr_val    = float(atr_series.iloc[-1]) if pd.notna(atr_series.iloc[-1]) else current_price * 0.02
        cost_atr_mult = float(self.parameters.get('cost_atr_mult', COST_ATR_MULT))
        realistic_cost_frac = (atr_val * cost_atr_mult) / current_price if current_price > 0 else 0.0

        edge_frac = (current_price - entry_band) / entry_band if entry_band > 0 else 0.0
        if realistic_cost_frac <= 0:
            size_factor = 1.0
        else:
            size_factor = max(MIN_SIZE_FACTOR, min(1.0, edge_frac / realistic_cost_frac))

        scale    = self.position_scale(regime_state)
        pos_size = round(float(self.parameters.get('base_size', BASE_SIZE)) * scale * size_factor, 4)
        if pos_size < 0.001:
            print('[BtcDonchianBreakoutRealisticCostsTv2] pos_size<0.001 -> signals=0', file=sys.stderr)
            return []

        st = self.compute_stops_and_targets(
            close, direction='LONG', current_price=current_price, regime_state=regime_state)

        if size_factor >= 0.75:
            confidence = 'HIGH'
        elif size_factor >= MIN_SIZE_FACTOR + 0.01:
            confidence = 'MED'
        else:
            confidence = 'LOW'

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
                'channel_period':        period,
                'confirm_bars':          confirm,
                'edge_frac':             round(edge_frac, 5),
                'realistic_cost_frac':   round(realistic_cost_frac, 5),
                'size_factor':           round(size_factor, 4),
                'upper_band':            round(float(upper.iloc[-1]), 2) if pd.notna(upper.iloc[-1]) else None,
                'lower_band':            round(float(lower.iloc[-1]), 2) if pd.notna(lower.iloc[-1]) else None,
                'regime':                regime_state,
                'variant':               'confirmed_entry_atr_cost_sizing',
            },
        )
        print(f'[BtcDonchianBreakoutRealisticCostsTv2] trend=LONG edge={edge_frac:.5f} '
              f'cost={realistic_cost_frac:.5f} size_factor={size_factor:.3f} '
              f'size={pos_size} regime={regime_state}', file=sys.stderr)
        return [sig]
