"""S_entropic_factor_replication (variant 2) — Fermi-Dirac entropic factor
replication, tracking a single named benchmark.

Source: Arratia, A. & Gzyl, H., "An Entropic Factor Model for Robust
Portfolio Replication" (arxiv.org/abs/2609.03552v1, 2026).

Thesis: same underlying paper as variant 1 — replace OLS with a two-stage
Fermi-Dirac entropy-minimization estimator for (1) factor exposure and (2)
replication weights, producing a long-only tracking basket that is more
robust to idiosyncratic shocks/data corruption than an OLS-based
replicator. The abstract discloses the entropic principle but not the
exact target factor, FD scale calibration, score-combination rule,
rebalance cadence, or budget split — this variant deliberately diverges
from a colleague's plausible reading on every one of those axes:

  - Target factor: a single named benchmark (SPY), read literally as
    "portfolio replication" — i.e. tracking one designated target index,
    not a synthetic equal-weighted cross-sectional average. SPY is
    excluded from the candidate basket itself.
  - Fermi-Dirac scale calibration: ONE GLOBAL threshold/temperature pair
    derived from the pooled cross-sectional residual distribution (not
    a per-asset self-calibrated scale). This reads the "circuit breaker"
    as a single stress detector applied uniformly to the whole panel —
    a name is only flagged as corrupted/outlier if its residual is large
    relative to the *market-wide* noise floor that day, not relative to
    its own idiosyncratic history.
  - Stage 2 combination: a WEIGHTED LINEAR BLEND of the three Fermi-Dirac
    preference scores (beta-closeness, idio-vol, corruption), not a
    product. A name failing on one dimension is penalized, not vetoed
    outright — a softer reading of "entropic weighting endogenously
    shrinks" than a strict AND-gate.
  - Basket budget: 100% of allocatable capital is routed through the
    sleeve (BASKET_BUDGET=1.0) — the paper positions this as the primary
    replication strategy, not a partial sub-sleeve.
  - Universe floor: the spec's `minimum_universe_size=100` is enforced as
    a hard runtime gate (MIN_UNIVERSE=100), the literal reading of the
    extractor's field, rather than a relaxed practical floor.
  - Regime scope: canonicalized narrowly to `HIGH_VOL, CRISIS` only — the
    extractor's `HIGH_VOL`/`RISK_OFF` tags read as the two acute-stress
    states, excluding `TRANSITIONING` (which is model uncertainty, not
    yet the realized tail-risk / data-corruption environment the paper's
    circuit breaker is built for).
  - Rebalance cadence (backtest harness only): quarterly walk-forward
    windows rather than monthly, on the theory that an entropic
    replication basket meant to survive stress regimes should not be
    churned every month.
  - Missing-data handling: a coverage-threshold filter (>=90% non-null
    over the lookback, gaps forward/back-filled) rather than a strict
    zero-NaN requirement — the paper's whole premise is robustness to
    sparse/corrupted inputs, so pre-filtering the universe down to only
    perfectly-clean tickers would defeat the point of the estimator.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['EntropicFactorReplicationTV2']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_entropic_factor_replication'
BENCHMARK        = 'SPY'

LOOKBACK       = 504     # ~2yr daily, matches spec's min_lookback_required
MIN_UNIVERSE   = 100     # literal reading of spec's minimum_universe_size
MAX_ASSETS     = 200     # compute cap; keep highest recent-vol columns beyond this
TOP_N          = 30      # tracking-basket size
MIN_WEIGHT     = 0.003
BASKET_BUDGET  = 1.00    # fraction of allocatable capital routed to this sleeve

# Stage-2 linear-blend weights (beta closeness, idio-vol, corruption)
SCORE_W = (0.40, 0.30, 0.30)


def _fermi_dirac(x: np.ndarray, threshold: float, temperature: float) -> np.ndarray:
    """Fermi-Dirac occupation function: ~1 below threshold, ~0 above it,
    smoothed over `temperature`. A single GLOBAL (threshold, temperature)
    pair is used across the whole panel (variant-2 calibration choice)."""
    z = (x - threshold) / max(temperature, 1e-8)
    z = np.clip(z, -50, 50)
    return 1.0 / (1.0 + np.exp(z))


def _entropic_basket_weights(prices_window: pd.DataFrame, benchmark: str = BENCHMARK,
                              min_universe: int = MIN_UNIVERSE, max_assets: int = MAX_ASSETS,
                              top_n: int = TOP_N) -> dict:
    """Two-stage Fermi-Dirac entropic replication scoring against a single
    named benchmark, with globally-calibrated FD scale parameters and a
    linear-blend Stage 2 combination rule.

    Returns {ticker: {weight, score, beta, resid_vol, corruption}}.
    """
    # Coverage-threshold filter + fill, not a strict dropna: requiring zero
    # missing days across the whole lookback would discard nearly every
    # name and contradicts the paper's own thesis (the entropic estimator
    # is supposed to be robust to sparse/corrupted data, not dependent on
    # its absence). Keep any column with >=90% non-null coverage and fill
    # the remaining small gaps.
    coverage = prices_window.notna().mean()
    keep_cols = coverage[coverage >= 0.90].index
    px = prices_window[keep_cols].ffill().bfill()
    if benchmark not in px.columns:
        return {}
    candidates = [c for c in px.columns if c != benchmark]
    if len(candidates) < min_universe or len(px) < 30:
        return {}

    if len(candidates) > max_assets:
        keep = px[candidates].tail(63).std().nlargest(max_assets).index.tolist()
        candidates = keep

    cols = [benchmark] + candidates
    rets = px[cols].pct_change().dropna(how='any')
    if len(rets) < 20 or (rets.shape[1] - 1) < min_universe:
        return {}

    rt = rets[benchmark].values.astype(float)          # T,  benchmark returns
    R  = rets[candidates].values.astype(float)          # T x N
    T, N = R.shape

    rt_c   = rt - rt.mean()
    var_rt = float(np.var(rt)) + 1e-12
    R_c    = R - R.mean(axis=0, keepdims=True)

    beta_ols  = (R_c.T @ rt_c) / (T * var_rt)                 # (N,) naive OLS beta to SPY
    alpha_ols = R.mean(axis=0) - beta_ols * rt.mean()
    resid_ols = R - (alpha_ols + np.outer(rt, beta_ols))      # T x N

    # --- GLOBAL Fermi-Dirac calibration (pooled across whole panel) ---
    abs_resid = np.abs(resid_ols)
    global_mad = float(np.median(abs_resid))
    if global_mad < 1e-8:
        global_mad = 1e-4
    fd_threshold   = 2.0 * global_mad
    fd_temperature = 1.0 * global_mad

    w_fd  = _fermi_dirac(abs_resid, fd_threshold, fd_temperature)   # T x N in (0,1)
    sum_w = np.where(w_fd.sum(axis=0) < 1e-8, 1e-8, w_fd.sum(axis=0))

    wm_R  = (w_fd * R).sum(axis=0) / sum_w
    wm_rt = (w_fd * rt[:, None]).sum(axis=0) / sum_w
    cov_w = (w_fd * (R - wm_R) * (rt[:, None] - wm_rt)).sum(axis=0) / sum_w
    var_w = (w_fd * (rt[:, None] - wm_rt) ** 2).sum(axis=0) / sum_w
    var_w = np.where(var_w < 1e-12, 1e-12, var_w)
    beta_entropic = cov_w / var_w                              # (N,) globally-calibrated entropic beta

    resid_entropic = R - beta_entropic[None, :] * rt[:, None]
    resid_mean_w   = (w_fd * resid_entropic).sum(axis=0) / sum_w
    resid_var = (w_fd * (resid_entropic - resid_mean_w) ** 2).sum(axis=0) / sum_w
    resid_vol = np.sqrt(np.maximum(resid_var, 1e-12)) * np.sqrt(252)

    corruption = 1.0 - w_fd.mean(axis=0)   # avg down-weighting = idiosyncratic-shock exposure

    deviation = np.abs(beta_entropic - 1.0)
    vol_tol  = float(np.median(resid_vol)) * 1.5 + 1e-8
    vol_temp = float(np.median(resid_vol)) * 0.5 + 1e-8

    score_beta    = _fermi_dirac(deviation,  0.30, 0.15)
    score_vol     = _fermi_dirac(resid_vol,  vol_tol, vol_temp)
    score_corrupt = _fermi_dirac(corruption, 0.30, 0.10)

    w_beta, w_vol, w_corrupt = SCORE_W
    combined = w_beta * score_beta + w_vol * score_vol + w_corrupt * score_corrupt
    combined = np.where(beta_entropic > 0, combined, 0.0)   # long-only factor sensibility

    order = np.argsort(-combined)[:top_n]
    total = float(combined[order].sum())
    if total < 1e-10:
        return {}

    out = {}
    for i in order:
        if combined[i] <= 0:
            continue
        ticker = candidates[i]
        out[ticker] = {
            'weight':     float(combined[i] / total),
            'score':      float(combined[i]),
            'beta':       float(beta_entropic[i]),
            'resid_vol':  float(resid_vol[i]),
            'corruption': float(corruption[i]),
        }
    return out


class EntropicFactorReplicationTV2(BaseStrategy):
    """Long-only tracking basket built from a Fermi-Dirac entropic
    two-stage estimator against a single named benchmark (SPY), with
    globally-calibrated FD scale and a linear-blend Stage 2 score —
    active only in acute-stress regimes."""

    id                = STRATEGY_ID
    name              = 'EntropicFactorReplicationTV2'
    description       = (
        'Fermi-Dirac entropic two-stage replication of SPY: globally-'
        'calibrated entropic beta, then long-only basket weights via a '
        'linear blend of Fermi-Dirac preference scores with a built-in '
        'circuit breaker for corrupted/idiosyncratic-shock names.'
    )
    tier              = 2
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['HIGH_VOL', 'CRISIS']
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

        if BENCHMARK not in prices.columns:
            print('[debug] signals=0 (benchmark SPY missing)', file=sys.stderr)
            return []

        scale  = self.position_scale(regime_state)
        lkbk   = int(self.parameters.get('lookback', LOOKBACK))
        top_n  = int(self.parameters.get('top_n', TOP_N))
        budget = float(self.parameters.get('basket_budget', BASKET_BUDGET))
        min_w  = float(self.parameters.get('min_weight', MIN_WEIGHT))

        candidates = [t for t in universe if t in prices.columns and t != BENCHMARK]
        if len(candidates) < MIN_UNIVERSE:
            print(f'[debug] signals=0 (universe too small: {len(candidates)})', file=sys.stderr)
            return []

        cols = [BENCHMARK] + candidates
        window = prices[cols].tail(lkbk + 1)
        basket = _entropic_basket_weights(window, benchmark=BENCHMARK,
                                           min_universe=MIN_UNIVERSE,
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
                    'method':           'fermi_dirac_entropic_replication_spy_target',
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

        quarter_ends = wide.resample('QE').last().index
        rows = []
        for i in range(len(quarter_ends) - 1):
            dt = quarter_ends[i]
            window = wide.loc[:dt].tail(LOOKBACK + 1)
            if len(window) < LOOKBACK // 2:
                continue
            basket = _entropic_basket_weights(window, benchmark=BENCHMARK,
                                               min_universe=MIN_UNIVERSE,
                                               max_assets=MAX_ASSETS, top_n=TOP_N)
            if not basket:
                continue
            next_dt = quarter_ends[i + 1]
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
