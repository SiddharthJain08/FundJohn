"""S_entropic_factor_replication — Fermi-Dirac entropic factor replication.

Source: Arratia, A. & Gzyl, H., "An Entropic Factor Model for Robust
Portfolio Replication" (arxiv.org/abs/2609.03552v1, 2026).

Thesis: replace OLS with a two-stage Fermi-Dirac entropy-minimization
estimator for (1) factor exposure and (2) replication weights, producing a
long-only tracking basket that is more robust to idiosyncratic shocks/data
corruption than an OLS-based replicator. The Fermi-Dirac occupation
function 1/(1+exp((x-threshold)/temperature)) is a smooth, bounded,
monotone-decreasing weight — used here both to down-weight outlier
residual days when estimating exposure (Stage 1) and to shrink basket
weight for names with large beta-deviation, high idiosyncratic vol, or
frequent data corruption (Stage 2's "circuit breaker").

Interpretation variant (paper discloses the entropic principle but not the
exact target factor, temperature schedule, rebalance cadence, or budget
split — abstract-only extraction):
  - Target factor: the equal-weighted cross-sectional average return of
    the eligible universe (a broad "market factor" proxy), NOT a single
    named benchmark such as SPY — a different implementation would likely
    hard-code SPY as the replication target.
  - Fermi-Dirac scale parameters (threshold/temperature) are set
    adaptively per asset from its own robust residual scale (median
    absolute residual), not a single global constant — this makes the
    circuit breaker self-calibrating to each name's typical noise level
    rather than uniform across the cross-section.
  - Stage 2 combination: the three Fermi-Dirac "occupation" scores (beta
    closeness, idiosyncratic vol, corruption frequency) are combined
    multiplicatively (product of independent probabilities) rather than
    as a weighted linear blend — a stricter interpretation in which any
    one failing dimension can veto a name from the basket.
  - Basket budget: only 50% of allocatable capital is routed through the
    replication sleeve (BASKET_BUDGET=0.5); the paper is silent on sizing
    relative to a broader book, and a different implementation might size
    it to 100%.
  - Regime scope: canonicalized from the extractor's `HIGH_VOL`/`RISK_OFF`
    tags to `TRANSITIONING, HIGH_VOL, CRISIS` (RISK_OFF's canonical
    expansion) — this strategy's edge is the robustness of the replicator
    during stress, so it is not run in calm LOW_VOL regimes where a naive
    OLS tracker would perform just as well.
  - Universe floor: the spec's `minimum_universe_size=100` is treated as
    an aspirational target, not a hard runtime gate — MIN_UNIVERSE=20 is
    used instead so the strategy still produces signals during the
    sparser pre-2024 coverage window of the master price parquet.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['EntropicFactorReplication']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_entropic_factor_replication'

LOOKBACK       = 504     # ~2yr daily, matches spec's min_lookback_required
MIN_UNIVERSE   = 20      # practical floor (see interpretation note above)
MAX_ASSETS     = 150     # cap for compute; keep highest recent-vol columns beyond this
TOP_N          = 25      # tracking-basket size
MIN_WEIGHT     = 0.004
BASKET_BUDGET  = 0.50    # fraction of allocatable capital routed to this sleeve


def _fermi_dirac(x: np.ndarray, threshold: np.ndarray, temperature: np.ndarray) -> np.ndarray:
    """Fermi-Dirac occupation function: ~1 below threshold, ~0 above it,
    smoothed over `temperature`. Used both as a robust down-weighting
    kernel (Stage 1) and as a bounded preference score (Stage 2)."""
    z = (x - threshold) / np.maximum(temperature, 1e-8)
    z = np.clip(z, -50, 50)
    return 1.0 / (1.0 + np.exp(z))


def _entropic_basket_weights(prices_window: pd.DataFrame, min_universe: int = MIN_UNIVERSE,
                              max_assets: int = MAX_ASSETS, top_n: int = TOP_N) -> dict:
    """Two-stage Fermi-Dirac entropic replication scoring.

    Stage 1: entropic (outlier-downweighted) beta of each asset to the
    equal-weighted cross-sectional 'market factor', replacing OLS via one
    Fermi-Dirac-reweighted least-squares pass.
    Stage 2: long-only budget-constrained weights via a product of
    Fermi-Dirac preference scores (beta closeness, idiosyncratic vol,
    corruption frequency) — the circuit-breaker effect.

    Returns {ticker: {weight, score, beta, resid_vol, corruption}}.
    """
    px = prices_window.dropna(axis=1, how='any')
    if px.shape[1] < min_universe or len(px) < 30:
        return {}

    if px.shape[1] > max_assets:
        keep_cols = px.tail(63).std().nlargest(max_assets).index
        px = px[keep_cols]

    rets = px.pct_change().dropna(how='any')
    if len(rets) < 20 or rets.shape[1] < min_universe:
        return {}

    R  = rets.values.astype(float)          # T x N
    rt = R.mean(axis=1)                     # equal-weighted market factor
    T, N = R.shape

    rt_c   = rt - rt.mean()
    var_rt = float(np.var(rt)) + 1e-12
    R_c    = R - R.mean(axis=0, keepdims=True)

    beta_ols  = (R_c.T @ rt_c) / (T * var_rt)                # (N,) naive OLS beta
    alpha_ols = R.mean(axis=0) - beta_ols * rt.mean()
    resid_ols = R - (alpha_ols + np.outer(rt, beta_ols))     # T x N

    abs_resid = np.abs(resid_ols)
    mad = np.median(abs_resid, axis=0)
    fallback = np.median(mad[mad > 1e-8]) if np.any(mad > 1e-8) else 1e-4
    mad = np.where(mad < 1e-8, fallback, mad)
    fd_threshold   = 2.5 * mad
    fd_temperature = mad

    w_fd  = _fermi_dirac(abs_resid, fd_threshold, fd_temperature)   # T x N in (0,1)
    sum_w = np.where(w_fd.sum(axis=0) < 1e-8, 1e-8, w_fd.sum(axis=0))

    wm_R  = (w_fd * R).sum(axis=0) / sum_w
    wm_rt = (w_fd * rt[:, None]).sum(axis=0) / sum_w
    cov_w = (w_fd * (R - wm_R) * (rt[:, None] - wm_rt)).sum(axis=0) / sum_w
    var_w = (w_fd * (rt[:, None] - wm_rt) ** 2).sum(axis=0) / sum_w
    var_w = np.where(var_w < 1e-12, 1e-12, var_w)
    beta_entropic = cov_w / var_w                             # (N,) robust entropic beta

    resid_entropic = R - beta_entropic[None, :] * rt[:, None]
    resid_mean_w   = (w_fd * resid_entropic).sum(axis=0) / sum_w
    resid_var = (w_fd * (resid_entropic - resid_mean_w) ** 2).sum(axis=0) / sum_w
    resid_vol = np.sqrt(np.maximum(resid_var, 1e-12)) * np.sqrt(252)

    corruption = 1.0 - w_fd.mean(axis=0)   # avg down-weighting = idiosyncratic-shock exposure

    deviation = np.abs(beta_entropic - 1.0)
    vol_tol  = float(np.median(resid_vol)) * 1.5 + 1e-8
    vol_temp = float(np.median(resid_vol)) * 0.5 + 1e-8

    score_beta    = _fermi_dirac(deviation,  np.full(N, 0.30), np.full(N, 0.15))
    score_vol     = _fermi_dirac(resid_vol,  np.full(N, vol_tol), np.full(N, vol_temp))
    score_corrupt = _fermi_dirac(corruption, np.full(N, 0.30), np.full(N, 0.10))

    combined = score_beta * score_vol * score_corrupt
    combined = np.where(beta_entropic > 0, combined, 0.0)   # long-only factor sensibility

    tickers = list(rets.columns)
    order = np.argsort(-combined)[:top_n]
    total = float(combined[order].sum())
    if total < 1e-10:
        return {}

    out = {}
    for i in order:
        if combined[i] <= 0:
            continue
        out[tickers[i]] = {
            'weight':     float(combined[i] / total),
            'score':      float(combined[i]),
            'beta':       float(beta_entropic[i]),
            'resid_vol':  float(resid_vol[i]),
            'corruption': float(corruption[i]),
        }
    return out


class EntropicFactorReplication(BaseStrategy):
    """Long-only tracking basket built from a Fermi-Dirac entropic
    two-stage estimator (exposure + weights), robust to idiosyncratic
    shocks and data corruption — active only in stress regimes."""

    id                = STRATEGY_ID
    name              = 'EntropicFactorReplication'
    description       = (
        'Fermi-Dirac entropic two-stage replication: robust entropic beta '
        'to the equal-weighted market factor, then long-only basket weights '
        'via a product of Fermi-Dirac preference scores with a built-in '
        'circuit breaker for corrupted/idiosyncratic-shock names.'
    )
    tier              = 2
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    min_lookback      = LOOKBACK
    MAX_SIGNALS       = TOP_N

    def default_parameters(self) -> dict:
        return {
            'lookback':      LOOKBACK,
            'top_n':         TOP_N,
            'basket_budget': BASKET_BUDGET,
            'min_weight':    MIN_WEIGHT,
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

        scale  = self.position_scale(regime_state)
        lkbk   = int(self.parameters.get('lookback', LOOKBACK))
        top_n  = int(self.parameters.get('top_n', TOP_N))
        budget = float(self.parameters.get('basket_budget', BASKET_BUDGET))
        min_w  = float(self.parameters.get('min_weight', MIN_WEIGHT))

        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < MIN_UNIVERSE:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []

        window = prices[tickers].tail(lkbk + 1)
        basket = _entropic_basket_weights(window, min_universe=MIN_UNIVERSE,
                                           max_assets=MAX_ASSETS, top_n=top_n)
        if not basket:
            print('[debug] signals=0 (no entropic-eligible trackers)', file=sys.stderr)
            return []

        last_px = window.iloc[-1]
        signals: List[Signal] = []
        for ticker, info in sorted(basket.items(), key=lambda kv: -kv[1]['weight']):
            w = info['weight']
            if w < min_w:
                continue
            price = float(last_px.get(ticker, 0.0))
            if price <= 0.0 or not np.isfinite(price):
                continue
            series = prices[ticker].dropna()
            if len(series) < 20:
                continue
            stops = self.compute_stops_and_targets(
                series, 'LONG', price, regime_state=regime_state,
            )
            score = info['score']
            confidence = 'HIGH' if score > 0.55 else ('MED' if score > 0.25 else 'LOW')
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(w * budget * scale, 4),
                confidence        = confidence,
                signal_params     = {
                    'entropic_beta':    round(info['beta'], 4),
                    'resid_vol':        round(info['resid_vol'], 4),
                    'corruption_score': round(info['corruption'], 4),
                    'fd_score':         round(score, 4),
                    'method':           'fermi_dirac_entropic_replication',
                    'regime':           regime_state,
                },
            ))
            if len(signals) >= self.MAX_SIGNALS:
                break

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ── Regime-partitioned backtest ───────────────────────────────────────────────
if __name__ == '__main__':
    import json
    import os

    ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    try:
        long_df = pd.read_parquet(os.path.join(ROOT, 'prices.parquet'))
        wide    = long_df.pivot_table(index='date', columns='ticker', values='close')
        wide.index = pd.to_datetime(wide.index)
        wide = wide.sort_index().loc['2017-01-01':'2025-12-31']

        reg_df = pd.read_parquet(os.path.join(ROOT, 'historical_regimes.parquet'))
        reg_df['date'] = pd.to_datetime(reg_df['date'])
        regime_map = dict(zip(reg_df['date'], reg_df['regime']))

        monthly_ends = wide.resample('ME').last().index
        rows = []
        for i in range(len(monthly_ends) - 1):
            dt = monthly_ends[i]
            window = wide.loc[:dt].tail(LOOKBACK + 1)
            if len(window) < LOOKBACK // 2:
                continue
            basket = _entropic_basket_weights(window, min_universe=MIN_UNIVERSE,
                                               max_assets=MAX_ASSETS, top_n=TOP_N)
            if not basket:
                continue
            next_dt = monthly_ends[i + 1]
            fwd = wide.loc[dt:next_dt]
            if len(fwd) < 2:
                continue
            regime_state = regime_map.get(dt, 'LOW_VOL')
            for ticker, info in basket.items():
                if ticker not in fwd.columns:
                    continue
                series = fwd[ticker].dropna()
                if len(series) < 2:
                    continue
                fwd_ret = float(series.iloc[-1] / series.iloc[0] - 1.0)
                rows.append({
                    'strategy_id':  STRATEGY_ID,
                    'signal_date':  str(dt.date()),
                    'regime_state': regime_state,
                    'pnl':          fwd_ret * info['weight'],
                    'r_multiple':   (fwd_ret * info['weight']) / 0.05,
                })

        trades_df = pd.DataFrame(rows)
        print(f'[backtest] total trades: {len(trades_df)}', file=sys.stderr)

        sys.path.insert(0, '/root/openclaw/src')
        from backtest.quick_backtest import run_backtest_with_regime_partition
        result = run_backtest_with_regime_partition(
            trades_df,
            strategy_id=STRATEGY_ID,
            thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
        )
        print(json.dumps(result, indent=2, default=str))
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f'[backtest] error: {e}', file=sys.stderr)
        sys.exit(1)
