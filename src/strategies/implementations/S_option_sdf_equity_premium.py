"""
Option-Implied Stochastic Discount Factor — Equity Premium Forecast.

Source: Shiraya, Yamakami & Yamazaki (2026), "Estimating the Stochastic
Discount Factor from Option Prices and Predicting the Equity Premium".
arXiv:2607.08500v3.

Hypothesis: a volatility-scaled SDF recovered from SPX/SPY option prices
exhibits a non-monotonic hump-to-W-shape pricing-kernel feature (more
weight on both tails than a lognormal kernel, rationalized under
stochastic volatility with a constant market price of risk). The forward
equity risk premium implied by that kernel subsumes the classic Martin
(2017) model-free variance bound (ERP >= SVIX^2) plus the risk-neutral
skew / tail-asymmetry information the bound ignores, and should forecast
SPY excess returns out-of-sample better than the variance-only bound.

Implementation note: this strategy has no network/filesystem access, so
rather than re-fitting a risk-neutral density from a raw strike grid it
consumes the moments the options-surface pipeline already fits daily off
the SPY smile (src/strategies/options_surface.py v3, spec 2026-09-06 A.3):
`mfiv_30d` (model-free implied variance — the SVIX^2 / Martin-bound input),
`rn_skew_30d` / `rn_kurt_30d` (BKM risk-neutral moments) and
`rn_p_dn10_30d` / `rn_p_up10_30d` (RN tail probabilities). Those are the
SDF's sufficient statistics for this signal.

Signal rule:
  rv20         = trailing 20-day realized vol of SPY (from prices).
  vrp          = mfiv_30d (iv30 fallback) - rv20. Classic variance-risk-
                 premium / Martin-bound input.
  kernel_scale = 1 + SKEW_W*max(-rn_skew_30d, 0)
                   + TAIL_W*max(rn_p_dn10_30d - rn_p_up10_30d, 0),
                 clipped to [1, KERNEL_SCALE_CAP]. Amplifies the premium
                 when the risk-neutral density is left-skewed / downside-
                 tail-heavy — the W-shape pricing-kernel stress a vol-only
                 bound misses.
  erp_t        = vrp * kernel_scale   (vol-scaled SDF equity-premium forecast)
  z_t          = (erp_t - mean) / std of erp_t's own trailing distribution,
                 reconstructed by scaling the panel's vrp_history by the
                 SAME kernel_scale (a positive scalar, so percentile rank
                 is invariant to it — kernel_scale only moves confidence /
                 size, not the LONG/SHORT boundary), when >= MIN_HISTORY
                 points are available; else erp_t / FIXED_SCALE.
  z_t > DEADBAND   -> LONG SPY   (elevated forecasted premium)
  z_t < -DEADBAND  -> SHORT SPY  (depressed/negative forecasted premium)
  else             -> no signal
"""
from __future__ import annotations
import statistics
import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['OptionSdfEquityPremium']

INSTRUMENT_CLASS = 'equity'

UNDERLYING       = 'SPY'
RV_WINDOW        = 20
MIN_RV           = 0.03
MIN_HISTORY      = 5
FIXED_SCALE      = 0.03
DEADBAND         = 0.15
SKEW_W           = 0.5
TAIL_W           = 0.5
KERNEL_SCALE_CAP = 2.0


class OptionSdfEquityPremium(BaseStrategy):
    id                = 'S_option_sdf_equity_premium'
    name              = 'Option-Implied SDF Equity Premium'
    description       = 'Vol-scaled, risk-neutral-skew-adjusted SDF forecast of the forward SPY equity premium (Shiraya, Yamakami & Yamazaki 2026); extends the Martin variance bound with RN skew/tail asymmetry.'
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
        if len(series) < max(self.min_lookback, RV_WINDOW + 2):
            print('[debug] signals=0 (insufficient history)', file=sys.stderr)
            return signals

        current_price = float(series.iloc[-1])
        if not (current_price == current_price and current_price > 0):
            print('[debug] signals=0 (bad last price)', file=sys.stderr)
            return signals

        rets = series.pct_change().dropna().iloc[-RV_WINDOW:]
        if len(rets) < RV_WINDOW:
            print('[debug] signals=0 (window too short)', file=sys.stderr)
            return signals
        rv20 = float((rets ** 2).mean()) ** 0.5 * (252 ** 0.5)
        if rv20 < MIN_RV:
            print('[debug] signals=0 (rv20 below floor)', file=sys.stderr)
            return signals

        opts_map = (aux_data or {}).get('options', {})
        opts = opts_map.get(UNDERLYING)
        if not opts:
            print('[debug] signals=0 (no SPY options data)', file=sys.stderr)
            return signals

        mfiv = opts.get('mfiv_30d')
        if mfiv is None:
            mfiv = opts.get('iv30')
        if mfiv is None:
            print('[debug] signals=0 (no mfiv_30d or iv30)', file=sys.stderr)
            return signals
        vrp = float(mfiv) - rv20

        rn_skew = opts.get('rn_skew_30d')
        rn_dn10 = opts.get('rn_p_dn10_30d')
        rn_up10 = opts.get('rn_p_up10_30d')
        skew_term = max(-float(rn_skew), 0.0) if rn_skew is not None else 0.0
        tail_term = (max(float(rn_dn10) - float(rn_up10), 0.0)
                     if (rn_dn10 is not None and rn_up10 is not None) else 0.0)
        kernel_scale = min(1.0 + SKEW_W * skew_term + TAIL_W * tail_term, KERNEL_SCALE_CAP)

        erp = vrp * kernel_scale

        hist_raw = [float(v) for v in (opts.get('vrp_history') or []) if v is not None]
        if len(hist_raw) >= MIN_HISTORY:
            scaled_hist = [v * kernel_scale for v in hist_raw]
            mean = statistics.mean(scaled_hist)
            stdev = statistics.pstdev(scaled_hist) or FIXED_SCALE
            z = (erp - mean) / stdev
        else:
            z = erp / FIXED_SCALE

        scale = self.position_scale(regime_state)

        if z > DEADBAND:
            direction = 'LONG'
            edge = min(z / 2.0, 1.0)
        elif z < -DEADBAND:
            direction = 'SHORT'
            edge = min(-z / 2.0, 1.0)
        else:
            print(f'[debug] signals=0 (z={z:.3f} inside deadband)', file=sys.stderr)
            return signals

        confidence = 'HIGH' if edge >= 0.6 else ('MED' if edge >= 0.3 else 'LOW')
        size = min(0.02 * scale * (1.0 + edge), 0.05)

        stops = self.compute_stops_and_targets(
            series, direction, current_price, atr_multiplier=2.0, regime_state=regime_state,
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
                'mfiv':         round(float(mfiv), 4),
                'rv20':         round(rv20, 4),
                'vrp':          round(vrp, 4),
                'kernel_scale': round(kernel_scale, 4),
                'erp':          round(erp, 4),
                'z':            round(z, 4),
            },
            features = {
                'rn_skew_30d':   round(float(rn_skew), 4) if rn_skew is not None else None,
                'rn_p_dn10_30d': round(float(rn_dn10), 4) if rn_dn10 is not None else None,
                'rn_p_up10_30d': round(float(rn_up10), 4) if rn_up10 is not None else None,
            },
        ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
