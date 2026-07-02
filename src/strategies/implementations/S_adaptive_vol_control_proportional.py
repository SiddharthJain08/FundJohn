"""
Adaptive Volatility Control — Proportional (closed-loop) feedback rule.

Implements the proportional-control vol-targeting model from:
  Devanathan et al. 2026 — "Single-Asset Adaptive Leveraged Volatility Control"
  arXiv:2603.01298v2

Algorithm (§2):
  Each day compute the log tracking error between EWMA realized vol and the
  15% annualized target; apply a proportional control law with gain g=55 and
  first-order IIR smoothing θ=0.6 to produce a weight adjustment κ_t; clip
  the running IVV weight to [0, 1.5].

  σ_realized_t  = EWMA_vol(returns, halflife=126) × √252
  e_t           = log(σ_realized_t / σ_target)
  raw_ctrl      = clip(-g × e_t, κ_min, κ_max)
  κ_t           = (1-θ) × raw_ctrl + θ × κ_{t-1}
  w_risky_t     = clip(w_risky_{t-1} + κ_t, 0, L)   # L=1.5

When w_risky_t > 0 emit LONG IVV; otherwise no signal.
IVV = iShares Core S&P 500 ETF (primary); SPY is the fallback proxy.

State: candidate only. Do NOT promote without operator sign-off.
"""
from __future__ import annotations

import math
import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['AdaptiveVolControlProportional']

INSTRUMENT_CLASS = 'etp'

STRATEGY_ID = 'S_adaptive_vol_control_proportional'

# Proportional-control hyper-parameters (Table 1 / §2)
SIGMA_TARGET  = 0.15   # annualised target volatility
G             = 55.0   # proportional gain
THETA         = 0.6    # first-order IIR smoothing on κ
KAPPA_MIN     = -1.0   # lower clip on control signal
KAPPA_MAX     =  1.0   # upper clip on control signal
LEVERAGE_CAP  =  1.5   # maximum risky-asset weight
EWMA_HALFLIFE = 126    # trading-day halflife ≈ 6-month memory
MIN_LOOKBACK  = 252    # rows required before first signal

PRIMARY_TICKER  = 'IVV'
FALLBACK_TICKER = 'SPY'


class AdaptiveVolControlProportional(BaseStrategy):
    """Proportional-control vol-targeting on IVV/SPY ETP.

    Adjusts the IVV allocation daily so that 126-day EWMA realized volatility
    tracks a 15% annualised target via a feedback gain-and-smooth control law.
    Position size equals the model weight w_risky, further scaled by the
    current regime multiplier and capped at 1.5× notional.
    """

    id                = STRATEGY_ID
    name              = 'Adaptive Vol Control Proportional'
    description       = (
        'Proportional-control (closed-loop feedback) rule on log-vol tracking '
        'error targets 15% annualised vol on IVV/SPY; weight clipped to [0, 1.5].'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = MIN_LOOKBACK
    # Active in all regimes: vol-targeting is most useful during uncertainty;
    # the leverage cap (1.5) bounds downside in CRISIS.
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'sigma_target':  SIGMA_TARGET,
            'g':             G,
            'theta':         THETA,
            'kappa_min':     KAPPA_MIN,
            'kappa_max':     KAPPA_MAX,
            'leverage_cap':  LEVERAGE_CAP,
            'ewma_halflife': EWMA_HALFLIFE,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        # ── 1. Resolve ticker ─────────────────────────────────────────────────
        if PRIMARY_TICKER in prices.columns:
            ticker = PRIMARY_TICKER
        elif FALLBACK_TICKER in prices.columns:
            ticker = FALLBACK_TICKER
        else:
            print(
                f'[{STRATEGY_ID}] neither IVV nor SPY in prices — returning []',
                file=sys.stderr,
            )
            return []

        series = prices[ticker].dropna()
        if len(series) < MIN_LOOKBACK:
            print(
                f'[{STRATEGY_ID}] only {len(series)} rows < {MIN_LOOKBACK} — returning []',
                file=sys.stderr,
            )
            return []

        # ── 2. Load parameters ────────────────────────────────────────────────
        p            = self.parameters
        sigma_target = float(p.get('sigma_target',  SIGMA_TARGET))
        g            = float(p.get('g',              G))
        theta        = float(p.get('theta',          THETA))
        kappa_min    = float(p.get('kappa_min',      KAPPA_MIN))
        kappa_max    = float(p.get('kappa_max',      KAPPA_MAX))
        lev_cap      = float(p.get('leverage_cap',   LEVERAGE_CAP))
        halflife     = int(p.get('ewma_halflife',    EWMA_HALFLIFE))

        # ── 3. EWMA realised vol (annualised) ─────────────────────────────────
        returns  = series.pct_change().dropna()
        if len(returns) < halflife:
            print(
                f'[{STRATEGY_ID}] insufficient return history ({len(returns)}) — returning []',
                file=sys.stderr,
            )
            return []

        ewma_var = returns.ewm(halflife=halflife).var()
        # annualise; guard against non-positive variance early in the series
        ewma_vol_arr = [
            math.sqrt(max(float(v) * 252, 1e-12))
            for v in ewma_var.values
        ]

        # ── 4. Proportional-control weight recursion ──────────────────────────
        kappa_prev = 0.0
        w_risky    = 1.0  # start fully invested; feedback dials it from there

        for sv in ewma_vol_arr:
            if not math.isfinite(sv) or sv <= 0:
                continue
            e_t        = math.log(sv / sigma_target)
            raw_ctrl   = max(kappa_min, min(kappa_max, -g * e_t))
            kappa_t    = (1.0 - theta) * raw_ctrl + theta * kappa_prev
            w_risky    = max(0.0, min(lev_cap, w_risky + kappa_t))
            kappa_prev = kappa_t

        latest_vol = ewma_vol_arr[-1] if ewma_vol_arr else sigma_target

        # ── 5. No position if feedback drove weight to zero ───────────────────
        if w_risky <= 0.0:
            print(
                f'[{STRATEGY_ID}] signals=0 w_risky={w_risky:.4f} — below zero, no position',
                file=sys.stderr,
            )
            return []

        # ── 6. Regime-scale and build signal ──────────────────────────────────
        current_price = float(series.iloc[-1])
        scale         = self.position_scale(regime_state)
        # apply regime scale but respect leverage cap
        pos_size      = round(min(w_risky * scale, lev_cap), 4)

        stops = self.compute_stops_and_targets(
            series,
            direction='LONG',
            current_price=current_price,
            regime_state=regime_state,
        )

        tracking_err = abs(latest_vol / sigma_target - 1.0)
        if tracking_err < 0.10:
            confidence = 'HIGH'
        elif tracking_err < 0.30:
            confidence = 'MED'
        else:
            confidence = 'LOW'

        signal = Signal(
            ticker            = ticker,
            direction         = 'LONG',
            entry_price       = current_price,
            stop_loss         = stops['stop'],
            target_1          = stops['t1'],
            target_2          = stops['t2'],
            target_3          = stops['t3'],
            position_size_pct = pos_size,
            confidence        = confidence,
            signal_params     = {
                'w_risky':       round(w_risky, 4),
                'ewma_vol':      round(latest_vol, 4),
                'sigma_target':  sigma_target,
                'tracking_err':  round(tracking_err, 4),
                'regime':        regime_state,
                'scale':         scale,
            },
        )

        print(
            f'[{STRATEGY_ID}] signals=1 ticker={ticker} w_risky={w_risky:.4f} '
            f'ewma_vol={latest_vol:.4f} entry={current_price:.2f} regime={regime_state}',
            file=sys.stderr,
        )
        return [signal]
