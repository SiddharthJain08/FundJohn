"""S_worst_case_es_robust_portfolio_tv2 — market-neutral cross-sectional
sleeve ranked by a standardized worst-case expected-shortfall (ES)
coefficient under a mean-anchored KL-divergence ambiguity set around a
Gaussian reference, with a single global (not per-asset) ambiguity radius.

Source: http://arxiv.org/abs/2610.29652v2 "Distributions or Quantiles? A
Convex Variational Theory of Worst-Case Risk" (Gao, Li, Wang, Zhang, 2026).

Thesis: a plug-in mean/ES estimate ignores estimation error in the fitted
return distribution. Gao et al. give an exact convex-variational projection
that turns a mean-anchored KL ambiguity set into a single standardized
worst-case-risk coefficient: for a Gaussian reference N(mu, sigma^2) and a
KL-ball of radius epsilon, the Donsker-Varadhan identity gives
sup_{Q: KL(Q||P)<=eps} E_Q[loss] = -mu + sigma*sqrt(2*epsilon) (closed form,
inf_t{t*sigma^2/2 + eps/t}). Stacking that KL-inflation term onto the
standard Gaussian ES_alpha multiplier c_alpha = phi(z_alpha)/(1-alpha) gives
a standardized worst-case-ES score per name: WCES_i = -mu_i + sigma_i *
(c_alpha + sqrt(2*epsilon)). The repo bans covariance-based optimizers at
this universe size (no 3x-observations guarantee across 100+ names), so
this is translated into a single-period, per-asset cross-sectional score
and the paper's SOCP allocation is proxied by a rank-based long/short
spread rather than a solved covariance optimization.

Interpretation variant 2 of 2. The abstract/pseudocode are unambiguous that
the risk measure is a worst-case ES under a mean-anchored KL ambiguity set
around a Gaussian reference, calibrated per rebalance — kept identical to
variant 1. Every free choice below is deliberately resolved the OTHER way
from variant 1:
  - ES confidence level alpha = 0.99 (a deeper tail-stress level, matching
    the paper's own framing of worst-case ES as a tail-stress bound, rather
    than variant 1's conventional CVaR/ES level of 0.95).
  - KL radius epsilon is a SINGLE FIXED GLOBAL SCALAR (EPSILON = 0.15),
    the structural special case the paper itself flags for a
    distribution-free ambiguity budget shared by every name in the
    universe — not variant 1's decision-/asset-dependent radius keyed off
    each ticker's own realized-vol instability. Every name is stressed by
    the same ambiguity budget; only its own (mu, sigma) differ.
  - Selection/weighting is MARKET-NEUTRAL LONG/SHORT (direction_vocab
    explicitly permits SHORT): long the top decile (lowest worst-case ES /
    most robust) and short the bottom decile (highest worst-case ES /
    least robust under model misspecification), rather than variant 1's
    long-only top-quintile tilt. A worst-case-ES ranking is a relative
    robustness signal and a dollar-neutral spread isolates that relative
    read from net market beta, which the paper's "robust vs. plug-in"
    framing does not itself rule out.
  - Rebalance cadence is MONTHLY (first trading day of each calendar
    month), not weekly — a slower recalibration cadence for the Gaussian
    reference, defensible since a global (not per-ticker) ambiguity radius
    does not need the faster refresh variant 1 used to track idiosyncratic
    vol-instability drift.
  - Mean/vol estimation over the lookback is EXPONENTIALLY WEIGHTED (decay
    halflife = 126 sessions, ~6m) rather than variant 1's flat
    equal-weighted average over the same 756-session window — recent
    observations get more say in the Gaussian reference, which is one of
    the paper's own stated motivations for treating the reference as
    estimation-error-prone in the first place.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List
from scipy.stats import norm

from strategies.base import BaseStrategy, Signal

__all__ = ['WorstCaseEsRobustPortfolioTv2']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_worst_case_es_robust_portfolio_tv2'

ALPHA          = 0.99       # ES confidence level (deeper tail than variant 1's 0.95)
EPSILON        = 0.15       # SINGLE fixed global KL-radius (not per-asset)
HALFLIFE       = 126        # EWMA halflife (sessions) for mu/sigma estimation
LOOKBACK       = 756        # spec min_lookback_required (~3y trading days)
MIN_UNIVERSE   = 100        # spec minimum_universe_size
DECILE_FRAC    = 0.10       # top/bottom decile long/short legs
GROSS_EXPOSURE = 0.90       # total book weight, split across both legs
BASE_SIZE_CAP  = 0.05       # per-name position_size_pct ceiling


def _c_alpha(alpha: float = ALPHA) -> float:
    z = norm.ppf(alpha)
    return float(norm.pdf(z) / (1.0 - alpha))


def _worst_case_es_scores(returns: pd.DataFrame, epsilon: float = EPSILON,
                           halflife: float = HALFLIFE) -> pd.Series:
    """Per-ticker standardized worst-case-ES score (higher = more robust),
    using an EWMA Gaussian reference and a single global KL radius."""
    mu = returns.ewm(halflife=halflife, min_periods=max(2, int(halflife / 2))).mean().iloc[-1]
    sigma = returns.ewm(halflife=halflife, min_periods=max(2, int(halflife / 2))).std().iloc[-1]
    sigma = sigma.clip(lower=1e-8)
    c_alpha = _c_alpha()
    stress = np.sqrt(2.0 * epsilon)   # global, identical across all tickers
    wces = -mu + sigma * (c_alpha + stress)   # standardized worst-case ES (loss units)
    return -wces   # flip sign: higher score = more robust / lower worst-case ES


def _is_monthly_rebalance_day(idx: pd.DatetimeIndex, dt: pd.Timestamp) -> bool:
    pos = idx.get_loc(dt)
    if pos == 0:
        return True
    return idx[pos - 1].month != dt.month


class WorstCaseEsRobustPortfolioTv2(BaseStrategy):
    """Market-neutral top/bottom-decile long/short sleeve ranked by a
    standardized worst-case-ES score under a mean-anchored, globally-fixed
    KL ambiguity set around an EWMA Gaussian reference (Gao, Li, Wang,
    Zhang 2026). Interpretation variant 2 of 2."""

    id                = STRATEGY_ID
    name              = 'WorstCaseEsRobustPortfolioTv2'
    description       = (
        'Market-neutral long/short decile sleeve ranked by a standardized '
        'worst-case-ES coefficient (EWMA Gaussian reference, single global '
        'KL radius), monthly rebalance. Variant 2 of 2.'
    )
    tier              = 2
    min_lookback      = LOOKBACK
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    instrument_class  = INSTRUMENT_CLASS
    MAX_SIGNALS       = 40

    def default_parameters(self) -> dict:
        return {'alpha': ALPHA, 'epsilon': EPSILON, 'gross_exposure': GROSS_EXPOSURE}

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0 (no price data)', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0 (regime gated)', file=sys.stderr)
            return []

        last_date = prices.index[-1]
        if not (_is_monthly_rebalance_day(prices.index, last_date) or self.cadence_reset(regime)):
            print('[debug] signals=0 (not a monthly rebalance day)', file=sys.stderr)
            return []

        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < MIN_UNIVERSE:
            print(f'[debug] signals=0 (universe {len(tickers)} < {MIN_UNIVERSE})', file=sys.stderr)
            return []

        price_data = prices[tickers].ffill().dropna(how='all')
        if len(price_data) < LOOKBACK + 1:
            print(f'[debug] signals=0 (need {LOOKBACK + 1} rows, got {len(price_data)})', file=sys.stderr)
            return []

        window = price_data.iloc[-LOOKBACK:]
        valid_counts = window.notna().sum()
        common = valid_counts[valid_counts >= LOOKBACK * 0.9].index.tolist()
        if len(common) < MIN_UNIVERSE:
            print(f'[debug] signals=0 (valid {len(common)} < {MIN_UNIVERSE})', file=sys.stderr)
            return []

        returns = window[common].pct_change().dropna(how='all')
        epsilon = float(self.parameters.get('epsilon', EPSILON))
        score = _worst_case_es_scores(returns, epsilon=epsilon).dropna()
        if len(score) < MIN_UNIVERSE:
            print(f'[debug] signals=0 (scored {len(score)} < {MIN_UNIVERSE})', file=sys.stderr)
            return []

        ranked = score.sort_values(ascending=False)
        n_leg = max(1, int(len(ranked) * DECILE_FRAC))
        long_leg = ranked.iloc[:n_leg]
        short_leg = ranked.iloc[-n_leg:]

        half_cap = max(1, self.MAX_SIGNALS // 2)
        long_leg = long_leg.iloc[:half_cap]
        short_leg = short_leg.iloc[:half_cap]

        gross = float(self.parameters.get('gross_exposure', GROSS_EXPOSURE))
        per_leg_gross = gross / 2.0
        scale = self.position_scale(regime_state)
        latest = price_data.iloc[-1]

        long_median = float(long_leg.median())
        short_median = float(short_leg.median())

        def _build_leg(leg: pd.Series, direction: str, leg_median: float) -> List[Signal]:
            out: List[Signal] = []
            if leg.empty:
                return out
            spread = (leg.max() - leg.min()) if direction == 'LONG' else (leg.max() - leg.min())
            weights = (leg - leg.min()).abs() if direction == 'LONG' else (leg.max() - leg).abs()
            w_sum = float(weights.sum())
            for ticker, s in leg.items():
                px = float(latest.get(ticker, 0.0))
                if px <= 0 or not np.isfinite(px):
                    continue
                w = (float(weights[ticker]) / w_sum) if w_sum > 1e-10 else (1.0 / len(leg))
                size = round(min(w * per_leg_gross * scale, BASE_SIZE_CAP), 4)
                if size <= 0:
                    continue
                is_extreme = (s > leg_median) if direction == 'LONG' else (s < leg_median)
                confidence = 'HIGH' if is_extreme else 'MED'
                stops = self.compute_stops_and_targets(
                    price_data[ticker].dropna(), direction=direction, current_price=px,
                    regime_state=regime_state,
                )
                out.append(Signal(
                    ticker            = ticker,
                    direction         = direction,
                    entry_price       = round(px, 4),
                    stop_loss         = float(stops['stop']),
                    target_1          = float(stops['t1']),
                    target_2          = float(stops['t2']),
                    target_3          = float(stops['t3']),
                    position_size_pct = size,
                    confidence        = confidence,
                    signal_params     = {
                        'wces_score': round(float(s), 6),
                        'alpha':      ALPHA,
                        'epsilon':    epsilon,
                        'regime':     regime_state,
                        'leg':        direction,
                    },
                ))
            return out

        signals: List[Signal] = []
        signals.extend(_build_leg(long_leg, 'LONG', long_median))
        signals.extend(_build_leg(short_leg, 'SHORT', short_median))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ── Regime-partitioned backtest ───────────────────────────────────────────────
if __name__ == '__main__':
    import json
    import os

    from backtest.quick_backtest import run_backtest_with_regime_partition

    ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    HOLD_DAYS = 21   # monthly rebalance -> ~1 month hold

    try:
        long_df = pd.read_parquet(os.path.join(ROOT, 'prices.parquet'))
        wide = long_df.pivot_table(index='date', columns='ticker', values='close')
        wide.index = pd.to_datetime(wide.index)
        wide = wide.sort_index().loc['2017-01-01':'2025-12-31'].ffill()

        reg_df = pd.read_parquet(os.path.join(ROOT, 'historical_regimes.parquet'))
        reg_df['date'] = pd.to_datetime(reg_df['date'])
        regime_map = dict(zip(reg_df['date'], reg_df['regime']))

        valid_cols = wide.columns[wide.notna().sum() >= LOOKBACK + HOLD_DAYS + 5]
        wide = wide[valid_cols]
        rows = []

        idx = LOOKBACK
        while idx + HOLD_DAYS < len(wide):
            dt = wide.index[idx]
            is_month_start = (idx == LOOKBACK) or (wide.index[idx - 1].month != dt.month)
            if is_month_start:
                window = wide.iloc[idx - LOOKBACK:idx]
                counts = window.notna().sum()
                common = counts[counts >= LOOKBACK * 0.9].index.tolist()
                if len(common) >= MIN_UNIVERSE:
                    returns = window[common].pct_change().dropna(how='all')
                    score = _worst_case_es_scores(returns).dropna()
                    if len(score) >= MIN_UNIVERSE:
                        ranked = score.sort_values(ascending=False)
                        n_leg = max(1, int(len(ranked) * DECILE_FRAC))
                        longs = ranked.index[:n_leg].tolist()
                        shorts = ranked.index[-n_leg:].tolist()
                        regime_state = regime_map.get(dt, 'LOW_VOL')
                        for t in longs:
                            p0, p1 = wide[t].iloc[idx], wide[t].iloc[idx + HOLD_DAYS]
                            if pd.notna(p0) and pd.notna(p1) and p0 > 0:
                                pnl = (p1 - p0) / p0
                                rows.append({'strategy_id': STRATEGY_ID, 'signal_date': str(dt.date()),
                                             'regime_state': regime_state, 'pnl': pnl, 'r_multiple': pnl / 0.05})
                        for t in shorts:
                            p0, p1 = wide[t].iloc[idx], wide[t].iloc[idx + HOLD_DAYS]
                            if pd.notna(p0) and pd.notna(p1) and p0 > 0:
                                pnl = (p0 - p1) / p0
                                rows.append({'strategy_id': STRATEGY_ID, 'signal_date': str(dt.date()),
                                             'regime_state': regime_state, 'pnl': pnl, 'r_multiple': pnl / 0.05})
            idx += 1

        trades_df = pd.DataFrame(rows)
        if trades_df.empty:
            print(json.dumps({'error': 'no trades generated'}))
        else:
            result = run_backtest_with_regime_partition(
                trades_df, strategy_id=STRATEGY_ID,
                thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
            )
            print(json.dumps(result, default=str, indent=2))
    except Exception as e:
        import traceback
        print(json.dumps({'error': str(e), 'traceback': traceback.format_exc()}))
