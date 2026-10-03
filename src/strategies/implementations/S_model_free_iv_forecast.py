"""
Model-Free Implied Volatility Forecast — VRP harvesting on SPX index options,
conditioned on model-free implied variance (MFIV) rather than Black-Scholes IV.

Source: Jiang & Tian (2005), "The Model-Free Implied Volatility and Its
Information Content", Review of Financial Studies. DOI: 10.1093/rfs/hhi027

Hypothesis: MFIV (Britten-Jones & Neuberger 2000, extended to jump-diffusion)
subsumes all forecasting information in BS IV and trailing realized vol — the
spread between MFIV and subsequently realized vol (the variance risk premium,
VRP) is therefore a cleaner, model-free timing signal for SPX vol exposure
than a BS-IV-based measure.

Implementation note: SPX/^GSPC carry no option chain in the master parquet;
SPY is the tradable, chain-covered proxy of the same underlying beta (see
S_ito_signature_vol_hedge and backtest.vol_index.OPTION_UNDERLYING_BETA for
precedent). `mfiv_30d` on the options_aggregates_enriched panel (options
surface v3, spec 2026-09-06 A.3) IS the model-free implied variance the paper
describes — estimated directly from the observed strike/price grid, not a
BS-IV average — so we read it instead of `iv30` and fall back to `iv30` only
when `mfiv_30d` isn't populated for a given day (thin-chain days).

Signal rule:
  rv20      = trailing 20-day realized vol of SPY, computed from prices
              (forward-realized vol is unobservable at signal time; trailing
              is the standard tractable proxy used across this vol-premium
              family of strategies).
  vrp       = mfiv_30d (or iv30 fallback) - rv20
  threshold = rolling percentile band of the panel's own vrp_history
              (IV-history minus HV20-history, last 8 sessions) when at least
              5 points are available; else a fixed absolute band.
  vrp > upper_threshold  -> SELL_VOL (short SPY straddle, delta-hedged): vol
                             rich relative to its own model-free forecast.
  vrp < lower_threshold  -> BUY_VOL  (long SPY straddle, delta-hedged): vol
                             cheap relative to its own model-free forecast.
  else                   -> no signal.
"""
from __future__ import annotations
import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal, OptionSpec

__all__ = ['ModelFreeIvForecast']

INSTRUMENT_CLASS = 'option'

UNDERLYING      = 'SPY'
RV_WINDOW       = 20       # trading days for trailing realized vol
MIN_RV          = 0.03     # skip if rv20 below this (data-quality floor)
FIXED_UPPER     = 0.04     # fallback absolute vrp band (no history available)
FIXED_LOWER     = -0.04
PCTL_UPPER      = 85       # percentile band when vrp_history is long enough
PCTL_LOWER      = 15
MIN_HISTORY     = 5
DTE_TARGET      = 30
HOLD_DAYS       = 21


class ModelFreeIvForecast(BaseStrategy):
    id                = 'S_model_free_iv_forecast'
    name              = 'Model-Free IV Forecast'
    description       = 'MFIV-conditioned VRP harvesting on SPY options: sell/buy vol when the model-free implied-vs-realized spread breaks its own trailing band (Jiang & Tian 2005).'
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
        if len(series) < RV_WINDOW + 2:
            print('[debug] signals=0 (insufficient history)', file=sys.stderr)
            return signals

        current_price = float(series.iloc[-1])
        if not (current_price == current_price and current_price > 0):
            print('[debug] signals=0 (bad last price)', file=sys.stderr)
            return signals

        log_rets = series.pct_change().dropna().iloc[-RV_WINDOW:]
        if len(log_rets) < RV_WINDOW:
            print('[debug] signals=0 (window too short)', file=sys.stderr)
            return signals

        rv20 = float((log_rets ** 2).mean()) ** 0.5 * (252 ** 0.5)
        if rv20 < MIN_RV:
            print('[debug] signals=0 (rv20 below floor)', file=sys.stderr)
            return signals

        opts_map = (aux_data or {}).get('options', {})
        opts = opts_map.get(UNDERLYING)
        if not opts:
            print('[debug] signals=0 (no SPY options data)', file=sys.stderr)
            return signals

        mfiv = opts.get('mfiv_30d')
        mfiv_source = 'mfiv_30d'
        if mfiv is None:
            mfiv = opts.get('iv30')
            mfiv_source = 'iv30_fallback'
        if mfiv is None:
            print('[debug] signals=0 (no mfiv_30d or iv30)', file=sys.stderr)
            return signals

        vrp = float(mfiv) - rv20

        vrp_history = opts.get('vrp_history') or []
        if len(vrp_history) >= MIN_HISTORY:
            hist = sorted(float(v) for v in vrp_history if v is not None)
            n = len(hist)
            upper = hist[min(int(n * PCTL_UPPER / 100), n - 1)]
            lower = hist[min(int(n * PCTL_LOWER / 100), n - 1)]
            upper = max(upper, FIXED_UPPER * 0.5)
            lower = min(lower, FIXED_LOWER * 0.5)
        else:
            upper, lower = FIXED_UPPER, FIXED_LOWER

        scale = self.position_scale(regime_state)

        if vrp > upper:
            direction = 'SELL_VOL'
            edge = min((vrp - upper) / 0.06, 1.0)
        elif vrp < lower:
            direction = 'BUY_VOL'
            edge = min((lower - vrp) / 0.06, 1.0)
        else:
            print(f'[debug] signals=0 (vrp={vrp:.4f} inside [{lower:.4f},{upper:.4f}])', file=sys.stderr)
            return signals

        confidence = 'HIGH' if edge >= 0.6 else ('MED' if edge >= 0.3 else 'LOW')
        size = min(0.015 * scale * (1.0 + edge), 0.04)

        stops = self.compute_stops_and_targets(
            series, 'SHORT' if direction == 'SELL_VOL' else 'LONG',
            current_price, atr_multiplier=2.0, regime_state=regime_state,
        )

        option_spec = OptionSpec(
            underlying    = UNDERLYING,
            right         = 'call',
            strike_rule   = 'atm',
            dte_target    = DTE_TARGET,
            structure     = 'straddle',
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
                'mfiv':         round(float(mfiv), 4),
                'mfiv_source':  mfiv_source,
                'rv20':         round(rv20, 4),
                'vrp':          round(vrp, 4),
                'band_upper':   round(upper, 4),
                'band_lower':   round(lower, 4),
                'hold_days':    HOLD_DAYS,
            },
            option_spec       = option_spec,
        ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
