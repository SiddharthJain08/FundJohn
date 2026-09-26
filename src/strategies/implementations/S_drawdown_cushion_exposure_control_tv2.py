"""S_drawdown_cushion_exposure_control (variant 2) — per-name drawdown-cushion
exposure control, applied cross-sectionally across the equity universe.

Source: Hsieh, C.-H., "On Control of Drawdown: Robust Invariance and
Optimality" (arxiv.org/abs/2609.23272v1, 2026).

Thesis: same underlying paper as variant 1 — scale risky-asset exposure by
the distance ("cushion") between a running peak and a prescribed max-
drawdown floor, using a monotonic closed-form approximation of the paper's
Bellman-optimal drawdown-modulated policy. Only the abstract was extracted,
so the exact peak convention, exposure functional form, leverage bound, and
scope (single proxy vs. multi-asset) are all unspecified. This variant
deliberately diverges from a colleague's plausible reading on every one of
those axes:

  - Scope: the spec's own novelty claim is a characterization for "multi-
    asset stochastic systems" (self_reported_novelty) — read literally
    here as license to apply the SAME per-asset control INDEPENDENTLY,
    name-by-name, across the default equity universe (each ticker's own
    price series stands in as its own "NAV"), rather than collapsing the
    whole book to one aggregate market-proxy NAV control.
  - Peak convention: an EXPANDING since-inception running maximum
    (`cummax()`), the literal reading of the pseudocode's
    "running_max(NAV)" — not a rolling trailing window. A name that has
    fully recovered from an old peak is still held to that peak until it
    is exceeded, per the paper's robust-invariance framing (the floor is
    defined off the true historical high-water mark, not a decaying one).
  - Exposure functional form: a LINEAR ramp (gamma=1) of the normalized
    cushion — the literal "monotonic increasing... bounded" reading of
    the pseudocode with no added convexity assumption — rather than a
    convex power-law ramp.
  - Leverage bound: the paper explicitly benchmarks the modulated policy
    against a "fixed-leverage policy" at the *same* drawdown limit, which
    only makes for an interesting comparison if leverage above 1x is on
    the table. LEVERAGE_CAP=1.25 (modest, capped leverage at full cushion)
    rather than a long-only cap of 1.0.
  - De minimis gate: skip a name only when its RESULTING POSITION SIZE
    rounds to below a dollar-fraction floor, not when its raw exposure
    fraction is below an arbitrary threshold — the cushion/exposure signal
    itself is still considered "live" at small values, it just isn't
    worth a ticket once translated into portfolio-weight terms.
  - Confidence bucketing: keyed off the cushion itself (the paper's
    central risk object — distance to the drawdown floor) rather than off
    the derived exposure fraction.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['DrawdownCushionExposureControlTV2']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_drawdown_cushion_exposure_control'

MIN_HISTORY_BARS  = 504    # data-sufficiency gate, matches spec's min_lookback_required
MAX_DD_LIMIT      = 0.20   # prescribed drawdown limit the policy must never breach
LEVERAGE_CAP      = 1.25   # modest leverage allowed at full cushion (vs. fixed-leverage baseline)
EXPOSURE_GAMMA    = 1.0    # literal linear ramp — no convexity assumption
BASE_SIZE         = 0.03   # per-name base portfolio fraction at full exposure
MAX_PER_NAME      = 0.06   # per-name position cap
MIN_TICKET        = 0.01   # de minimis position-size floor (in portfolio-fraction terms)
MAX_SIGNALS       = 25     # cap on number of names carried per cycle


def _exposure_fraction(cushion: pd.Series) -> pd.Series:
    """Linear approximation of the Bellman-optimal exposure policy:
    normalized cushion clipped to [0, 1], scaled up to LEVERAGE_CAP."""
    normalized = (cushion / MAX_DD_LIMIT).clip(lower=0.0, upper=1.0)
    return LEVERAGE_CAP * normalized.pow(EXPOSURE_GAMMA)


def _cushion_and_exposure(nav: pd.Series) -> tuple:
    peak     = nav.cummax()                       # expanding since-inception high
    floor    = (1.0 - MAX_DD_LIMIT) * peak
    cushion  = (nav - floor) / nav
    exposure = _exposure_fraction(cushion)
    return cushion, exposure


class DrawdownCushionExposureControlTV2(BaseStrategy):
    """
    Per-name drawdown-cushion exposure control: each ticker's own price
    series is treated as its own "NAV" and scaled by a linear closed-form
    approximation of a Bellman-optimal drawdown-modulated policy, applied
    cross-sectionally across the equity universe rather than to a single
    aggregate market proxy. Source: Hsieh (2026), arXiv:2609.23272.
    """

    id                = STRATEGY_ID
    name              = 'DrawdownCushionExposureControlTV2'
    description       = (
        'Cross-sectional drawdown-cushion exposure control: per-ticker '
        'position sized by a linear closed-form approximation of a '
        'Bellman-optimal drawdown-modulated policy against an expanding '
        'since-inception peak, with modest leverage headroom at full '
        'cushion.'
    )
    tier              = 2
    instrument_class  = INSTRUMENT_CLASS
    signal_frequency  = 'daily'
    min_lookback      = MIN_HISTORY_BARS + 10
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = MAX_SIGNALS

    def default_parameters(self) -> dict:
        return {
            'max_dd_limit':  MAX_DD_LIMIT,
            'leverage_cap':  LEVERAGE_CAP,
            'base_size':     BASE_SIZE,
            'min_ticket':    MIN_TICKET,
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
            print(f'[debug] {STRATEGY_ID}: signals=0 (regime={regime_state} excluded)', file=sys.stderr)
            return []

        scale     = self.position_scale(regime_state)
        base_size = float(self.parameters.get('base_size', BASE_SIZE))
        min_tick  = float(self.parameters.get('min_ticket', MIN_TICKET))

        candidates = [t for t in universe if t in prices.columns]
        scored = []
        for ticker in candidates:
            nav = prices[ticker].dropna()
            if len(nav) < MIN_HISTORY_BARS + 10:
                continue
            cushion, exposure = _cushion_and_exposure(nav)
            latest_exposure = float(exposure.iloc[-1]) if not exposure.empty else float('nan')
            latest_cushion  = float(cushion.iloc[-1]) if not cushion.empty else float('nan')
            if np.isnan(latest_exposure) or np.isnan(latest_cushion):
                continue
            price = float(nav.iloc[-1])
            if price <= 0:
                continue
            pos_size = round(base_size * latest_exposure * scale, 4)
            pos_size = min(pos_size, MAX_PER_NAME)
            if pos_size < min_tick:
                continue
            scored.append((ticker, price, latest_cushion, latest_exposure, pos_size, nav))

        if not scored:
            print(f'[debug] {STRATEGY_ID}: signals=0 (no name cleared the de minimis ticket floor)', file=sys.stderr)
            return []

        scored.sort(key=lambda row: -row[3])
        signals: List[Signal] = []
        for ticker, price, latest_cushion, latest_exposure, pos_size, nav in scored[:self.MAX_SIGNALS]:
            st = self.compute_stops_and_targets(
                nav, direction='LONG', current_price=price, regime_state=regime_state,
            )
            confidence = 'HIGH' if latest_cushion > 0.14 else ('MED' if latest_cushion > 0.06 else 'LOW')
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = price,
                stop_loss         = float(st['stop']),
                target_1          = float(st['t1']),
                target_2          = float(st['t2']),
                target_3          = float(st['t3']),
                position_size_pct = pos_size,
                confidence        = confidence,
                signal_params     = {
                    'cushion':           round(latest_cushion, 4),
                    'exposure_fraction': round(latest_exposure, 4),
                    'max_dd_limit':      MAX_DD_LIMIT,
                    'leverage_cap':      LEVERAGE_CAP,
                    'regime':            regime_state,
                },
            ))

        print(f'[debug] {STRATEGY_ID}: signals={len(signals)}', file=sys.stderr)
        return signals


def run_regime_backtest():
    """
    Offline regime-partitioned backtest for the lifecycle promotion gate.
    Applies the per-name cushion/exposure policy across a broad slice of the
    price panel (every column with sufficient history), then calls
    run_backtest_with_regime_partition() so eligible_regimes_proposed is
    available for candidate->staging promotion.
    """
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

    from backtest.quick_backtest import run_backtest_with_regime_partition

    base = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'master')
    prices_path = os.path.join(base, 'prices.parquet')
    regime_path = os.path.join(base, 'historical_regimes.parquet')

    raw    = pd.read_parquet(prices_path, columns=['ticker', 'date', 'close'])
    prices = raw.pivot(index='date', columns='ticker', values='close')
    prices.index = pd.to_datetime(prices.index)
    prices.sort_index(inplace=True)

    reg = pd.read_parquet(regime_path)
    reg.index = pd.to_datetime(reg.index if reg.index.name else reg.get('date', reg.index))
    regime_col = next((c for c in ('regime_state', 'state', 'regime') if c in reg.columns), None)

    eligible = [c for c in prices.columns if prices[c].notna().sum() >= MIN_HISTORY_BARS + 10]
    eligible = eligible[:100]   # compute cap for the offline harness

    rows = []
    for ticker in eligible:
        nav = prices[ticker].dropna()
        cushion, exposure = _cushion_and_exposure(nav)
        nav_fwd = nav.pct_change().shift(-1)
        for signal_date, exp in exposure.items():
            if np.isnan(exp):
                continue
            fwd_ret = float(nav_fwd.get(signal_date, 0.0) or 0.0)
            pnl = fwd_ret * float(exp)
            r   = pnl / max(abs(fwd_ret), 1e-6) if fwd_ret != 0.0 else 0.0
            regime_state = 'LOW_VOL'
            if regime_col and signal_date in reg.index:
                regime_state = str(reg.loc[signal_date, regime_col])
            rows.append({
                'strategy_id': STRATEGY_ID,
                'signal_date': str(signal_date.date()),
                'regime_state': regime_state,
                'pnl': pnl,
                'r_multiple': r,
            })

    trades_df = pd.DataFrame(rows)
    if trades_df.empty:
        print('[backtest] no trades generated', file=sys.stderr)
        return None

    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(
        f'[backtest] eligible_regimes_proposed={result.get("eligible_regimes_proposed")}',
        file=sys.stderr,
    )
    return result


if __name__ == '__main__':
    run_regime_backtest()
