"""
Sharpe Stability Optimization (variant 2) — ranks/weights a long-only equity
sleeve by the temporal CONSISTENCY of Sharpe ratio across overlapping
subperiods, not its point-estimate level. Thesis (Roman, 2026, blogging Bajo
Traver & Rodriguez-Dominguez): a single trailing Sharpe cannot distinguish a
name with persistent, repeatable risk-adjusted skill from one that got there
via one or two lucky episodes.

Interpretation choices (paper is ambiguous on subperiod construction, the
exact SSR functional form, and selection/weighting mechanics — this is
deliberately variant 2 of 2, diverging from a colleague's plausible reading
on every one of those axes):

  - Subperiods = the blog's OWN illustrative window taken literally: 21
    trading days (one calendar month), stepped every 5 trading days (weekly)
    across the trailing 504-day (2y) data window carried for the min-lookback
    requirement. This produces ~90 overlapping monthly-Sharpe estimates per
    name instead of ~22 quarterly ones — a finer-grained, higher-frequency
    read on "temporal consistency" that reacts to regime drift within the
    2y window rather than smoothing over it with quarter-length windows.
  - SSR = mean(subperiod Sharpes) - PENALTY_K * std(subperiod Sharpes) +
    SKEW_BONUS * skew(subperiod Sharpes) — a mean-minus-penalty scoring form
    WITH an explicit skewness tilt, not a ratio form. The source material's
    own formula_tokens list "skewness" and "kurtosis" alongside "dispersion"
    as discriminative inputs beyond the Sharpe ratio and the Ulcer
    Performance Index; a pure mean/std ratio (variant 1's reading) discards
    that higher-moment information entirely. This form also stays
    numerically well-behaved near std=0 (no ratio blow-up), so selection is
    done directly on the SSR score's magnitude rather than requiring a
    rank-only fallback.
  - Selection + weighting is SCORE-MAGNITUDE-based, not rank-based: names
    with positive SSR get a cross-sectional z-score of SSR, and z-scores are
    clipped at zero and normalized to sum to 1 — a softer, continuous
    reading of "overweight high-SSR assets" that lets a runaway-consistent
    name pull more capital than its rank alone would imply, rather than
    variant 1's flat linear-rank ladder. This is a cross-sectional z-score
    weighting, not a covariance/mean-variance optimizer, so it does not run
    afoul of the project's covariance-optimization restriction.
  - Active in ALL FOUR canonical regimes (all-weather), not a 3-of-4 subset —
    a signal built purely on a name's own historical Sharpe-of-Sharpes
    consistency is, on the paper's own framing, a robustness measure that
    should be regime-agnostic almost by construction; CRISIS periods are
    exactly when distinguishing "persistent skill" names from "episodic
    outperformance" names should matter most, not when the signal should be
    switched off.
  - Weekly rebalance (every 5-trading-day step, matched to the subperiod
    stride), not monthly — the underlying signal now refreshes every stride
    of the rolling window (5 trading days), so a monthly cadence would sit
    on stale scores for most of the intervening rebalance period.

Source: https://portfoliooptimizer.io/blog/the-sharpe-stability-ratio-evaluating-the-sharpe-ratio-temporal-consistency/
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['SharpeStabilityOptimizationTV2']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_sharpe_stability_optimization'

LOOKBACK       = 504   # trailing 2y, per strategy_spec min_lookback_required
SUBPERIOD_LEN  = 21    # one calendar month (trading days) - blog's own example
STRIDE         = 5     # weekly step between overlapping subperiods
MIN_SUBPERIODS = 40    # need at least this many overlapping Sharpe estimates
MIN_UNIVERSE   = 30    # per strategy_spec minimum_universe_size
PENALTY_K      = 0.5   # dispersion penalty weight in the SSR score
SKEW_BONUS     = 0.25  # tilt toward right-skewed (fewer bad-tail) consistency


class SharpeStabilityOptimizationTV2(BaseStrategy):
    """Long-only equity sleeve ranked by a mean-minus-penalty-plus-skew
    Sharpe Stability score over weekly-stepped, monthly-window overlapping
    subperiod Sharpes (variant 2 — diverges from variant 1 on window length,
    score form, weighting mechanic, regime scope, and rebalance cadence).

    Source: https://portfoliooptimizer.io/blog/the-sharpe-stability-ratio-evaluating-the-sharpe-ratio-temporal-consistency/
    """

    id                = STRATEGY_ID
    name              = 'Sharpe Stability Optimization (TV2)'
    description       = (
        'Ranks the equity universe by a mean-minus-dispersion-plus-skew '
        'Sharpe Stability score computed over weekly-stepped, monthly-window '
        'overlapping Sharpe ratios, and weights the positive-score names by '
        'clipped cross-sectional z-score, active in all four regimes.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = LOOKBACK
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {
            'subperiod_len':  SUBPERIOD_LEN,
            'stride':         STRIDE,
            'penalty_k':      PENALTY_K,
            'skew_bonus':     SKEW_BONUS,
            'sleeve_gross':   0.50,   # total sleeve gross before regime scale
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Weekly rebalance: every 5th trading day, or a regime-flip day.
        idx = prices.index
        if len(idx) < 5:
            print('[debug] signals=0', file=sys.stderr)
            return []
        week_boundary = (len(idx) % 5 == 0)
        if not week_boundary and not self.cadence_reset(regime):
            print('[debug] signals=0', file=sys.stderr)
            return []

        subperiod_len = int(self.parameters.get('subperiod_len', SUBPERIOD_LEN))
        stride        = int(self.parameters.get('stride', STRIDE))
        penalty_k     = float(self.parameters.get('penalty_k', PENALTY_K))
        skew_bonus    = float(self.parameters.get('skew_bonus', SKEW_BONUS))

        candidates_universe = [t for t in universe if t in prices.columns] if universe else list(prices.columns)
        if len(candidates_universe) < MIN_UNIVERSE:
            print(f'[debug] signals=0 ({len(candidates_universe)} tickers < {MIN_UNIVERSE})', file=sys.stderr)
            return []

        scored: dict[str, dict] = {}
        for ticker in candidates_universe:
            series = prices[ticker].dropna()
            if len(series) < LOOKBACK:
                continue
            window = series.iloc[-LOOKBACK:]
            rets = window.pct_change().dropna()
            if len(rets) < subperiod_len + stride * (MIN_SUBPERIODS - 1):
                continue

            sub_sharpes = []
            start = 0
            n = len(rets)
            while start + subperiod_len <= n:
                sub = rets.iloc[start:start + subperiod_len]
                sub_std = float(sub.std())
                if sub_std > 0:
                    sub_sharpe = float(sub.mean()) / sub_std * np.sqrt(252.0)
                    sub_sharpes.append(sub_sharpe)
                start += stride

            if len(sub_sharpes) < MIN_SUBPERIODS:
                continue

            arr = np.asarray(sub_sharpes)
            mean_sharpe = float(arr.mean())
            std_sharpe  = float(arr.std(ddof=1))
            if mean_sharpe <= 0:
                continue  # not persistent skill if the average subperiod Sharpe is non-positive
            skew_sharpe = float(pd.Series(arr).skew())
            if np.isnan(skew_sharpe):
                skew_sharpe = 0.0

            ssr_score = mean_sharpe - penalty_k * std_sharpe + skew_bonus * skew_sharpe
            if ssr_score <= 0:
                continue

            try:
                current_price = float(series.iloc[-1])
            except (ValueError, IndexError):
                continue
            if not (current_price > 0):
                continue

            scored[ticker] = {
                'ssr_score':    ssr_score,
                'mean_sharpe':  mean_sharpe,
                'std_sharpe':   std_sharpe,
                'skew_sharpe':  skew_sharpe,
                'n_subperiods': len(sub_sharpes),
                'price':        current_price,
            }

        if not scored:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Cross-sectional z-score weighting on the SSR score (magnitude-
        # based, not rank-based): names with above-average scores pull more
        # weight than a flat rank ladder would give them.
        scores = np.array([v['ssr_score'] for v in scored.values()])
        mu, sigma = float(scores.mean()), float(scores.std(ddof=0))
        if sigma <= 0:
            z_scores = {t: 1.0 for t in scored}
        else:
            z_scores = {t: max(0.0, (v['ssr_score'] - mu) / sigma) for t, v in scored.items()}

        eligible = [(t, z) for t, z in z_scores.items() if z > 0]
        if not eligible:
            print('[debug] signals=0', file=sys.stderr)
            return []
        eligible.sort(key=lambda kv: kv[1], reverse=True)
        eligible = eligible[:self.MAX_SIGNALS]

        total_z = float(sum(z for _, z in eligible))
        scale        = self.position_scale(regime_state)
        sleeve_gross = float(self.parameters.get('sleeve_gross', 0.50))
        n_eligible   = len(eligible)

        signals = []
        for i, (ticker, z) in enumerate(eligible):
            info = scored[ticker]
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue
            current_price = info['price']
            direction = 'LONG'
            stops = self.compute_stops_and_targets(
                series,
                direction=direction,
                current_price=current_price,
                regime_state=regime_state,
            )

            weight = z / total_z
            pos_size_frac = round(weight * sleeve_gross * scale, 4)

            tercile = n_eligible / 3.0
            if i < tercile:
                confidence = 'HIGH'
            elif i < 2 * tercile:
                confidence = 'MED'
            else:
                confidence = 'LOW'

            signals.append(Signal(
                ticker            = ticker,
                direction         = direction,
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = pos_size_frac,
                confidence        = confidence,
                signal_params     = {
                    'ssr_score':    round(info['ssr_score'], 4),
                    'mean_sharpe':  round(info['mean_sharpe'], 4),
                    'std_sharpe':   round(info['std_sharpe'], 4),
                    'skew_sharpe':  round(info['skew_sharpe'], 4),
                    'n_subperiods': info['n_subperiods'],
                    'z_score':      round(z, 4),
                    'weight':       round(weight, 4),
                    'regime':       regime_state,
                    'scale':        scale,
                    'rebalance':    True,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
