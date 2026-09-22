"""
Supertrend Naive Flip (BTC-USD) — port of Zhang & Okoye (2026), "Supertrend,
flipped to death: the most-taught indicator on the internet"
(https://therefutation.com/essays/does-supertrend-work/, venue: The
Refutation — negative results only).

The paper's finding: a textbook, always-in-market Supertrend(10,3) flip
signal on BTC hourly bars has no measurable edge — it lost 92.9% over 1,239
flips vs +940% buy-and-hold, statistically indistinguishable from a
same-cadence coin flip. This is a NEGATIVE-RESULT port: we expect this
strategy to fail the promotion gate, and it exists in the manifest as a
documented refutation, not a live-alpha candidate.

Two structural deviations from the source paper, both forced by this
system's contracts rather than free interpretive choices:
  1. Daily close-only bars (no OHLC/hourly BTC ledger column is declared in
     this strategy's data_requirements), vs the paper's hourly OHLC bars.
  2. instrument_class=crypto signals are LONG/FLAT only (no crypto shorting
     rail) — the paper's SHORT leg on bearish flips is mapped to FLAT here.

Interpretation variant (2 of 2) — deliberately TEXTBOOK, i.e. the exact
implementation the source paper is refuting (the paper's whole point is that
this canonical construction is what's commonly taught and still loses):
  - Band basis is a synthetic HL2 proxy, not raw close. With only close-only
    daily bars available (no true H/L columns declared), HL2 is
    reconstructed as the midpoint of a 2-bar rolling max/min window
    (`(rolling_max_2 + rolling_min_2) / 2`) — a defensible stand-in for
    "typical price" that differs materially from tv1's raw-close basis.
  - ATR proxy is a rolling SIMPLE mean of |close-to-close diff| at
    window=ATR_PERIOD, approximating Wilder's ATR smoothing (Wilder's
    original recursive formula converges to a similarly-weighted rolling
    average; this is the standard simplification used in most public
    Supertrend ports), deliberately NOT the EWM/span convention tv1 used.
  - Full band-carry-forward/ratchet state machine — the textbook mechanism
    that makes real Supertrend "sticky": the upper band only ratchets DOWN
    (never up) while in a downtrend, and the lower band only ratchets UP
    (never down) while in an uptrend, carrying forward the prior bar's band
    when the new one would loosen it. The trend flips only when close
    crosses the *current* (ratcheted) band, matching the canonical
    algorithm most tutorials implement — this is the literal "textbook"
    construction the refutation essay is aimed at, per the source's framing
    of Supertrend as "the most-taught indicator on the internet."
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['SupertrendNaiveFlipTV2']

INSTRUMENT_CLASS = 'crypto'

TICKER       = 'BTC-USD'
ATR_PERIOD   = 10      # per source: Supertrend(10, 3)
MULTIPLIER   = 3.0
MIN_LOOKBACK = 504      # system floor for crypto backtest qualification
BASE_SIZE    = 0.08     # base gross fraction (pre regime-scale); conservative given refutation prior


class SupertrendNaiveFlipTV2(BaseStrategy):
    """Textbook always-in-market Supertrend(10,3) flip on BTC-USD; LONG on
    bullish flip, FLAT on bearish flip (no crypto short rail). Negative-result
    port — expected to fail the promotion gate per the source refutation."""

    id                = 'S_btc_supertrend_naive_flip'
    name              = 'BTC Supertrend Naive Flip (TV2)'
    description       = (
        'Textbook always-in-market Supertrend(10,3) HL2-proxy flip on BTC-USD, '
        'with band-ratchet; negative-result port of a refutation essay showing '
        'no measurable edge.'
    )
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = MIN_LOOKBACK
    instrument_class  = 'crypto'
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {'atr_period': ATR_PERIOD, 'multiplier': MULTIPLIER, 'base_size': BASE_SIZE}

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or TICKER not in prices.columns:
            print('[SupertrendNaiveFlipTV2] no BTC-USD column -> signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[SupertrendNaiveFlipTV2] should_run=False -> signals=0', file=sys.stderr)
            return []

        close = prices[TICKER].dropna()
        period = int(self.parameters.get('atr_period', ATR_PERIOD))
        mult   = float(self.parameters.get('multiplier', MULTIPLIER))
        if len(close) < max(MIN_LOOKBACK, period + 2):
            print('[SupertrendNaiveFlipTV2] insufficient history -> signals=0', file=sys.stderr)
            return []

        # HL2 proxy: midpoint of a 2-bar rolling max/min window over close —
        # a defensible "typical price" stand-in given no true H/L columns.
        roll_max = close.rolling(2, min_periods=1).max()
        roll_min = close.rolling(2, min_periods=1).min()
        hl2_proxy = (roll_max + roll_min) / 2.0

        # ATR proxy: rolling SIMPLE mean of |close-to-close diff| (window=
        # period) — approximates Wilder smoothing; deliberately NOT the EWM
        # convention used in the other porting variant.
        tr_proxy = close.diff().abs()
        atr = tr_proxy.rolling(period, min_periods=period).mean()

        basic_upper = hl2_proxy + mult * atr
        basic_lower = hl2_proxy - mult * atr

        # Textbook ratchet state machine: bands carry forward and only
        # tighten toward price, never loosen, until a flip occurs.
        final_upper = [float('nan')] * len(close)
        final_lower = [float('nan')] * len(close)
        trend = 'LONG'

        first_valid = atr.first_valid_index()
        if first_valid is None:
            print('[SupertrendNaiveFlipTV2] ATR never valid -> signals=0', file=sys.stderr)
            return []
        start_i = close.index.get_loc(first_valid)

        final_upper[start_i] = float(basic_upper.iloc[start_i])
        final_lower[start_i] = float(basic_lower.iloc[start_i])

        for i in range(start_i + 1, len(close)):
            c_today = close.iloc[i]
            bu = basic_upper.iloc[i]
            bl = basic_lower.iloc[i]
            prev_fu = final_upper[i - 1]
            prev_fl = final_lower[i - 1]
            prev_close = close.iloc[i - 1]

            # Ratchet: upper band only moves down (or stays) unless price
            # broke above the prior upper band; lower band only moves up
            # (or stays) unless price broke below the prior lower band.
            fu = bu if (bu < prev_fu or prev_close > prev_fu) else prev_fu
            fl = bl if (bl > prev_fl or prev_close < prev_fl) else prev_fl
            final_upper[i] = float(fu)
            final_lower[i] = float(fl)

            if trend == 'LONG' and c_today < fl:
                trend = 'SHORT'
            elif trend == 'SHORT' and c_today > fu:
                trend = 'LONG'
            # else: no cross -> hold prior trend

        current_price = float(close.iloc[-1])

        if trend == 'SHORT':
            # No crypto short rail -> bearish flip maps to FLAT (system
            # constraint, not a free interpretive choice).
            print('[SupertrendNaiveFlipTV2] bearish flip -> FLAT, signals=0', file=sys.stderr)
            return []

        scale    = self.position_scale(regime_state)
        pos_size = round(float(self.parameters.get('base_size', BASE_SIZE)) * scale, 4)
        if pos_size < 0.001:
            print('[SupertrendNaiveFlipTV2] pos_size<0.001 -> signals=0', file=sys.stderr)
            return []

        st = self.compute_stops_and_targets(
            close, direction='LONG', current_price=current_price, regime_state=regime_state)

        sig = Signal(
            ticker            = TICKER,
            direction         = 'LONG',
            entry_price       = current_price,
            stop_loss         = float(st['stop']),
            target_1          = float(st['t1']),
            target_2          = float(st['t2']),
            target_3          = float(st['t3']),
            position_size_pct = pos_size,
            confidence        = 'LOW',
            signal_params     = {
                'atr_period': period,
                'multiplier': mult,
                'atr_last':   round(float(atr.iloc[-1]), 4) if pd.notna(atr.iloc[-1]) else None,
                'regime':     regime_state,
                'variant':    'hl2_proxy_sma_atr_ratchet',
            },
        )
        print(f'[SupertrendNaiveFlipTV2] trend=LONG atr={float(atr.iloc[-1]):.2f} '
              f'size={pos_size} regime={regime_state}', file=sys.stderr)
        return [sig]
