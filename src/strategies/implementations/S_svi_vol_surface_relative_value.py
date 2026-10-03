"""
Arbitrage-Free SVI Volatility Surfaces — Gatheral & Jacquier (2013).
Source: https://doi.org/10.1080/14697688.2013.819986

Hypothesis: an arbitrage-free SVI fit of the SPX implied-vol smile gives a
smooth fair-value curve per expiry; strikes whose quoted IV deviates
materially from that fitted curve are mispriced relative to the rest of the
smile and should mean-revert toward it as quotes re-calibrate.

Implementation note: a full per-strike SVI calibration (a,b,rho,m,sigma via
nonlinear least squares) needs the raw strike/price grid, which is not
carried into `aux_data` — strategies only see the scalar smile summary from
`options_aggregates_enriched.parquet` (src/strategies/options_surface.py).
This is a tractable reduction consistent with the sibling vol-surface
strategies in this book (S_option_implied_tail_premium,
S_model_free_iv_forecast): the SVI curve's "fair" level at the money is
`iv30` itself, and `surface_premium` (vega-weighted wing IV minus ATM IV) is
exactly the wing-vs-ATM residual a 3-point (25d put / ATM / 25d call) SVI fit
would produce. We treat `surface_premium` as the quoted-vs-fitted deviation
the paper describes and mean-revert it against bands calibrated from the
historical SPY options_aggregates_enriched distribution (mean .0158,
std .0096; n=59 sessions, 2026 vintage). `rr_25d_30d` (the skew/risk-reversal
leg) is folded in as a secondary conviction signal, calibrated the same way
(mean .0465, std .0115). SPX/^GSPC carries no option chain in the master
parquet, so SPY is the tradable, chain-covered underlying proxy (same
precedent as S_model_free_iv_forecast / S_ito_signature_vol_hedge).

Signal rule (daily):
  premium_z = (surface_premium - 0.0158) / 0.0096
  skew_z    = (rr_25d_30d      - 0.0465) / 0.0115
  edge      = (|premium_z| + |skew_z|) / 2
  premium_z >  1.0  -> SELL_VOL (short strangle): wings rich vs the fitted
                        ATM-anchored curve, expect convergence down.
  premium_z < -1.0  -> BUY_VOL  (long strangle):  wings cheap vs the fitted
                        curve, expect convergence up.
  else              -> no signal (inside the fair band).
  Delta-hedged daily; 30 DTE target, rolled at 7 DTE remaining (SVI's
  "re-calibration" horizon is short-dated).
"""
from __future__ import annotations
import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal, OptionSpec

__all__ = ['SviVolSurfaceRelativeValue']

INSTRUMENT_CLASS = 'option'

UNDERLYING   = 'SPY'
MEAN_PREMIUM = 0.0158    # calibrated: SPY surface_premium historical mean
STD_PREMIUM  = 0.0096    # calibrated: SPY surface_premium historical std
MEAN_RR      = 0.0465    # calibrated: SPY rr_25d_30d historical mean
STD_RR       = 0.0115    # calibrated: SPY rr_25d_30d historical std
Z_UPPER      = 1.0
Z_LOWER      = -1.0
DTE_TARGET   = 30
ROLL_DTE     = 7
HOLD_DAYS    = 21


class SviVolSurfaceRelativeValue(BaseStrategy):
    id                = 'S_svi_vol_surface_relative_value'
    name              = 'SVI Vol Surface Relative Value'
    description       = ('Arbitrage-free-SVI-style wing-vs-ATM surface_premium mean reversion on SPY '
                          'options, delta-hedged strangle, skew (rr_25d_30d) as secondary conviction '
                          '(Gatheral & Jacquier 2013).')
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 255
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 1

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        signals: List[Signal] = []
        if prices is None or prices.empty or UNDERLYING not in prices.columns:
            print('[debug] signals=0 (no SPY column)', file=sys.stderr)
            return signals

        regime_state = regime.get('state', 'LOW_VOL') if isinstance(regime, dict) else 'LOW_VOL'
        if not self.should_run(regime_state):
            print('[debug] signals=0 (regime gate)', file=sys.stderr)
            return signals

        series = prices[UNDERLYING].dropna()
        if len(series) < 2:
            print('[debug] signals=0 (insufficient history)', file=sys.stderr)
            return signals

        current_price = float(series.iloc[-1])
        if not (current_price == current_price and current_price > 0):
            print('[debug] signals=0 (bad last price)', file=sys.stderr)
            return signals

        opts_map = (aux_data or {}).get('options', {})
        opts = opts_map.get(UNDERLYING)
        if not opts:
            print('[debug] signals=0 (no SPY options data)', file=sys.stderr)
            return signals

        iv30 = opts.get('iv30')
        premium = opts.get('surface_premium')
        if premium is None:
            put25, call25 = opts.get('iv_25d_put_30d'), opts.get('iv_25d_call_30d')
            if put25 is not None and call25 is not None and iv30 is not None:
                premium = (float(put25) + float(call25)) / 2.0 - float(iv30)
        if premium is None:
            print('[debug] signals=0 (no surface_premium or wing fallback)', file=sys.stderr)
            return signals

        rr = opts.get('rr_25d_30d')
        if rr is None:
            put25, call25 = opts.get('iv_25d_put_30d'), opts.get('iv_25d_call_30d')
            if put25 is not None and call25 is not None:
                rr = float(call25) - float(put25)
        rr = float(rr) if rr is not None else MEAN_RR

        premium_z = (float(premium) - MEAN_PREMIUM) / STD_PREMIUM
        skew_z = (rr - MEAN_RR) / STD_RR
        edge = (abs(premium_z) + abs(skew_z)) / 2.0

        if premium_z > Z_UPPER:
            direction = 'SELL_VOL'
        elif premium_z < Z_LOWER:
            direction = 'BUY_VOL'
        else:
            print(f'[debug] signals=0 (premium_z={premium_z:.2f} inside [{Z_LOWER},{Z_UPPER}])', file=sys.stderr)
            return signals

        scale = self.position_scale(regime_state)
        confidence = 'HIGH' if edge >= 1.5 else ('MED' if edge >= 0.8 else 'LOW')
        size = min(0.015 * scale * (1.0 + min(edge, 1.0)), 0.04)

        stops = self.compute_stops_and_targets(
            series, 'SHORT' if direction == 'SELL_VOL' else 'LONG',
            current_price, atr_multiplier=2.0, regime_state=regime_state,
        )

        option_spec = OptionSpec(
            underlying    = UNDERLYING,
            right         = 'call',
            strike_rule   = 'target_delta',
            target_delta  = 0.25,
            dte_target    = DTE_TARGET,
            structure     = 'strangle',
            hedge         = 'delta',
            hedge_cadence = 'daily',
            roll_dte      = ROLL_DTE,
        )

        signals.append(Signal(
            ticker            = UNDERLYING,
            direction         = direction,
            entry_price       = current_price,
            stop_loss         = stops['stop'],
            target_1          = stops['t1'],
            target_2          = stops['t2'],
            target_3          = stops['t3'],
            position_size_pct = round(size, 4),
            confidence        = confidence,
            signal_params     = {
                'surface_premium': round(float(premium), 4),
                'premium_z':       round(premium_z, 3),
                'rr_25d_30d':      round(rr, 4),
                'skew_z':          round(skew_z, 3),
                'hold_days':       HOLD_DAYS,
            },
            option_spec       = option_spec,
        ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
