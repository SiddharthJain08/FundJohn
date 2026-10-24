"""
Arbitrage-Free Implied Volatility Surface Reconstruction via Additive
Martingales — Luo, Lin, Luo, Minh, Zhang, Liu (2026).
Source: http://arxiv.org/abs/2610.25671v1

Hypothesis: a no-arbitrage-consistent IV surface fitted from (possibly
sparse) option quotes reveals which listed quotes deviate from fair value;
dealers cannot instantly re-price the whole surface after each sparse quote
update, so those deviations persist long enough to trade.

Implementation note: the full additive-martingale surface fit (static
no-arbitrage constraints embedded directly into the reconstruction) needs
the raw strike/price grid, which strategies never see — only the scalar
smile summary from `options_aggregates_enriched.parquet`
(src/strategies/options_surface.py) is carried into `aux_data`. That summary
already contains `mfiv_30d`, a model-free (surface-integral) variance swap
vol computed off the fitted RN density across the *whole* smile — a
no-arbitrage-consistent "fair" vol in the same sense the paper's additive
martingale surface is, since it enforces the convexity constraints a static
arbitrage-free surface must satisfy. We treat `iv30` (the single quoted ATM
IV) as the "sparse listed quote" and `mfiv_30d` as the fitted
surface-consistent fair value; their gap is exactly the deviation the paper
describes. Model-free IV is a known few-points-above-ATM quantity by
construction (wing convexity under Jensen's inequality), so the deviation is
z-scored against a prior centered on that known bias rather than zero
(precedent: S_svi_vol_surface_relative_value calibrates `surface_premium`
the same way). `mf_tail_premium_30d` (excess tail mass vs lognormal on the
fitted RN density) is folded in as secondary conviction — a surface with
fat fitted tails is itself evidence the ATM quote and the wings are being
priced by different, stale information sets.

Signal rule (daily):
  dev        = iv30 - mfiv_30d
  dev_z      = (dev - MEAN_DEV) / STD_DEV
  tail_z     = (mf_tail_premium_30d or 0.0) / STD_TAIL
  edge       = (|dev_z| + |tail_z|) / 2
  dev_z >  1.0  -> SELL_VOL (short strangle): quoted ATM IV rich vs the
                    no-arb-consistent full-surface fair vol.
  dev_z < -1.0  -> BUY_VOL  (long strangle):  quoted ATM IV cheap vs the
                    no-arb-consistent full-surface fair vol.
  else          -> no signal (inside the fair band).
  Delta-hedged daily; 30 DTE target, rolled at 7 DTE remaining (matches the
  horizon over which a sparse quote gets re-priced).
"""
from __future__ import annotations
import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal, OptionSpec
from src.strategies.universe_default import options_eligible_only as universe_filter

__all__ = ['IvSurfaceNoArbRelativeValue']

INSTRUMENT_CLASS = 'option'

UNDERLYING  = 'SPY'
MEAN_DEV    = -0.012   # prior: mfiv runs a few vol points above ATM BS iv (wing convexity)
STD_DEV     = 0.010    # prior dispersion of iv30 - mfiv_30d
STD_TAIL    = 0.008    # prior dispersion of mf_tail_premium_30d
Z_UPPER     = 1.0
Z_LOWER     = -1.0
DTE_TARGET  = 30
ROLL_DTE    = 7
HOLD_DAYS   = 21


class IvSurfaceNoArbRelativeValue(BaseStrategy):
    id                = 'S_iv_surface_no_arb_relative_value'
    name              = 'IV Surface No-Arb Relative Value'
    description       = ('Additive-martingale-style no-arbitrage surface fair value (mfiv_30d) vs the '
                          'quoted ATM iv30 on SPY options, delta-hedged strangle, fitted-tail richness '
                          '(mf_tail_premium_30d) as secondary conviction (Luo et al. 2026).')
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
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
        mfiv30 = opts.get('mfiv_30d')
        if iv30 is None or mfiv30 is None:
            print('[debug] signals=0 (no iv30/mfiv_30d)', file=sys.stderr)
            return signals

        dev = float(iv30) - float(mfiv30)
        dev_z = (dev - MEAN_DEV) / STD_DEV

        tail = opts.get('mf_tail_premium_30d')
        tail = float(tail) if tail is not None else 0.0
        tail_z = tail / STD_TAIL

        edge = (abs(dev_z) + abs(tail_z)) / 2.0

        if dev_z > Z_UPPER:
            direction = 'SELL_VOL'
        elif dev_z < Z_LOWER:
            direction = 'BUY_VOL'
        else:
            print(f'[debug] signals=0 (dev_z={dev_z:.2f} inside [{Z_LOWER},{Z_UPPER}])', file=sys.stderr)
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
                'iv30':             round(float(iv30), 4),
                'mfiv_30d':         round(float(mfiv30), 4),
                'deviation':        round(dev, 4),
                'dev_z':            round(dev_z, 3),
                'mf_tail_premium':  round(tail, 4),
                'tail_z':           round(tail_z, 3),
                'hold_days':        HOLD_DAYS,
            },
            option_spec       = option_spec,
        ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
