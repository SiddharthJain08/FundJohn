"""
Itô Signature Vol Hedge — model-free VRP harvesting on SPY index options,
guided by discretized path-signature terms instead of plain realized vol.

Source: Guo, Wang, Zhang (2026), "Tradable Itô Signatures: A Model-Free,
Interpretable Framework for Dynamic Hedging" (arXiv:2608.18120v1).

Hypothesis: discretized Itô signature terms of the log-price path can
replicate option payoffs more cheaply/accurately than plain delta hedging,
so writing options and hedging off the signature (rather than delta alone)
captures the vol/skew premium while cutting hedging error — especially when
the path is strongly convex/trending (large 2nd-level cross term), which is
exactly where a delta-only hedger accumulates the most slippage.

Implementation note: we do not have a live options-execution path that
consumes a full signature-weighted hedge ratio, so `OptionSpec.hedge='delta'`
is the closest supported approximation — the strategy's edge over a naive
IV/RV carry trade comes from CONDITIONING the sell/buy-vol decision and its
sizing on the signature's path-dependent term, not from the hedge mechanics.

Discretized level-2 signature of the window's log-return path r_1..r_W:
  S1 = sum(r_i)                      -- level-1 term (net drift)
  S2 = sum(r_i^2)                    -- level-2 diagonal term (quadratic variation)
  S3 = (S1^2 - S2) / 2               -- level-2 off-diagonal (Lévy-area-style) term;
                                          the "21" word of the signature, sum_{i<j} r_i*r_j.
                                          Captures path ORDER (trend/convexity), a genuine
                                          signature feature beyond realized vol.

RV20 (annualized) is recovered from S2. `x = |S3| / (S2 + eps)` measures how much
of the path's structure is order-dependent (convex/trending) vs. pure diffusive
noise — the paper's claim is that this is where signature-based hedging beats
delta-only hedging, so we scale confidence/size with x on top of the IV/RV edge.

Trade rule (single name: SPY, options_eligible; SPX/^GSPC have no chain data
in the master parquet, SPY is the tradable, chain-covered proxy of the same
underlying beta per backtest.vol_index.OPTION_UNDERLYING_BETA):
  ratio = iv30 / rv20
  ratio > SELL_THRESH  -> SELL_VOL (short strangle, delta-hedged): vol rich.
  ratio < BUY_THRESH   -> BUY_VOL  (long strangle, delta-hedged): vol cheap.
  else                 -> no signal.
Confidence/size scale with both the IV/RV extremity and the signature
path-dependency intensity x.
"""
from __future__ import annotations
import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal, OptionSpec

__all__ = ['ItoSignatureVolHedge']

INSTRUMENT_CLASS = 'option'

UNDERLYING     = 'SPY'
WINDOW         = 20      # trading days for the discretized signature window
SELL_THRESH    = 1.15    # iv30/rv20 above this -> vol rich, SELL_VOL
BUY_THRESH     = 0.85    # iv30/rv20 below this -> vol cheap, BUY_VOL
MIN_RV         = 0.03    # skip if rv20 below this (data-quality floor)
DTE_TARGET     = 30
HOLD_DAYS      = 14


class ItoSignatureVolHedge(BaseStrategy):
    id                = 'S_ito_signature_vol_hedge'
    name              = 'Ito Signature Vol Hedge'
    description       = 'Signature-conditioned VRP harvesting on SPY options: sell/buy vol on IV/RV extremity, sized by path-order (signature cross-term) intensity.'
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 30
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
        if len(series) < WINDOW + 2:
            print('[debug] signals=0 (insufficient history)', file=sys.stderr)
            return signals

        current_price = float(series.iloc[-1])
        if not (current_price == current_price and current_price > 0):
            print('[debug] signals=0 (bad last price)', file=sys.stderr)
            return signals

        log_rets = series.pct_change().dropna()
        window_rets = log_rets.iloc[-WINDOW:]
        if len(window_rets) < WINDOW:
            print('[debug] signals=0 (window too short)', file=sys.stderr)
            return signals

        # Discretized level-2 signature terms of the log-return path.
        s1 = float(window_rets.sum())
        s2 = float((window_rets ** 2).sum())
        s3 = (s1 * s1 - s2) / 2.0   # off-diagonal / Levy-area-style cross term

        rv20 = (s2 / WINDOW) ** 0.5 * (252 ** 0.5)
        if rv20 < MIN_RV:
            print('[debug] signals=0 (rv20 below floor)', file=sys.stderr)
            return signals

        # Path-dependency intensity: how much of the path's structure is
        # order-dependent (convex/trending) vs. pure diffusive noise.
        path_intensity = min(abs(s3) / (s2 + 1e-9), 1.5) / 1.5  # normalized 0..1

        opts_map = (aux_data or {}).get('options', {})
        opts = opts_map.get(UNDERLYING)
        iv30 = opts.get('iv30') if opts else None
        if iv30 is None:
            print('[debug] signals=0 (no SPY iv30)', file=sys.stderr)
            return signals

        ratio = float(iv30) / rv20
        scale = self.position_scale(regime_state)

        if ratio > SELL_THRESH:
            direction = 'SELL_VOL'
            edge = min((ratio - SELL_THRESH) / 0.50, 1.0)
        elif ratio < BUY_THRESH:
            direction = 'BUY_VOL'
            edge = min((BUY_THRESH - ratio) / 0.35, 1.0)
        else:
            print(f'[debug] signals=0 (ratio={ratio:.3f} inside neutral band)', file=sys.stderr)
            return signals

        conf_val = 0.5 * edge + 0.5 * path_intensity
        confidence = 'HIGH' if conf_val >= 0.6 else ('MED' if conf_val >= 0.3 else 'LOW')
        size = min(0.015 * scale * (1.0 + conf_val), 0.04)

        stops = self.compute_stops_and_targets(
            series, 'SHORT' if direction == 'SELL_VOL' else 'LONG',
            current_price, atr_multiplier=2.0, regime_state=regime_state,
        )

        option_spec = OptionSpec(
            underlying    = UNDERLYING,
            right         = 'call',
            strike_rule   = 'atm',
            dte_target    = DTE_TARGET,
            structure     = 'strangle',
            hedge         = 'delta',
            hedge_cadence = 'daily',
            roll_dte      = 7,
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
                'iv30':            round(float(iv30), 4),
                'rv20':            round(rv20, 4),
                'iv_hv_ratio':     round(ratio, 3),
                'sig_s1':          round(s1, 6),
                'sig_s2':          round(s2, 6),
                'sig_s3':          round(s3, 6),
                'path_intensity':  round(path_intensity, 4),
                'hold_days':       HOLD_DAYS,
            },
            option_spec       = option_spec,
        ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
