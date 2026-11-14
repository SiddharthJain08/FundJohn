"""
S_garch_copula_cvar_scenario — ARMA-GARCH / Student-t Copula CVaR Scenario Allocation

Source: Mensah, Jaffri, Appiah, Lindquist, Rachev, Fabozzi (2026), "Dynamic
Scenario-Based Portfolio Optimization for Multi-Asset ETFs" (arXiv:2611.16540v1).

Hypothesis: conditional-scenario risk models (per-asset ARMA-GARCH vol +
Student-t copula dependence) for mean-variance/CVaR portfolio optimization do
not reliably beat simple historical moving-window estimation once
turnover/rebalancing constraints are accounted for — the target-adjustment
(turnover) bound dominates realized outcomes more than the return/risk-model
choice. This implementation still builds the literal conditional-scenario
machinery (that is the paper's unambiguous method), but treats the paper's
own finding as a design constraint: the turnover/target-adjustment limit is
first-class, not an afterthought, and the scenario model is kept as light as
the house toolchain allows rather than maximally faithful.

Variant 1/2 — abstract-only extraction (no appendix with exact ETF list,
GARCH order, copula df, CVaR level, turnover bound or rebalance cadence), so
every numeric/structural choice below is this implementation's defensible
reading, deliberately different from a colleague's plausible reading:
  - 30-ETF universe spans equity (broad + sector + developed/EM), rates/
    credit, commodities and currency — a colleague could as defensibly pick
    a sector-heavy or equity-only 30, but a scenario-copula model earns its
    keep most where cross-asset tail dependence is least Gaussian (rates vs.
    commodities vs. credit), so this variant deliberately spans asset
    classes rather than concentrating in equities.
  - GARCH is implemented as a fixed-decay (lambda=0.94 RiskMetrics) EWMA
    conditional-variance recursion, not an MLE-fit GARCH(1,1) — the `arch`
    package is outside the allowed import set and a from-scratch per-asset
    MLE fitter for 30 names is out of scope for a 200-line signal generator;
    EWMA is the standard practitioner approximation to GARCH(1,1) and is
    used here as the "conditional variance" leg. A colleague could as
    defensibly use a trailing realized-vol window instead — this variant
    deliberately uses the EWMA recursion because it is the piece of the
    paper's method ("GARCH") that a flat trailing window does NOT capture
    (vol clustering / fast decay toward recent shocks).
  - "ARMA" mean model is deliberately AR(0) (trailing-mean drift only, no
    autoregressive term) — the abstract names ARMA-GARCH without specifying
    order; daily ETF return autocorrelation is weak enough that the drift
    term dominates any AR(1) coefficient, so this variant spends its
    complexity budget on the GARCH/copula legs instead.
  - Student-t copula: df=6 (fixed, moderately fat-tailed — a colleague could
    as defensibly use df=4 or df=10); dependence estimated from the
    correlation of GARCH-standardized residuals over a 504-session window
    (=min_lookback_required), T_SCENARIOS=500 Monte Carlo draws per
    rebalance, simulated as mu_i + sigma_cond_i * t_draw_i where t_draw is a
    unit-variance multivariate-t draw sharing one chi-square mixing variable
    across assets (the mechanism that creates copula-style joint tail
    dependence beyond the correlation matrix alone).
  - CVaR optimization: `lib.convex.min_evar_weights` (long-only; EVaR
    Chernoff-bounds and upper-bounds CVaR, the same convex surrogate already
    adopted fleet-wide for this risk family) on the scenario LOSS matrix at
    z=0.95 — long-only was chosen over "limited long-short" (the paper
    allows either) because the only in-repo convex CVaR/EVaR helper is
    long-only by construction (`lib.convex` never exposes a long-short
    variant, and strategy files may not call cvxpy directly).
  - Turnover/target-adjustment bound: the paper's own headline finding is
    that this bound — not the return-model choice — dominates outcomes, so
    it is implemented explicitly rather than left to position-sizing
    defaults: this month's raw EVaR-optimal weights are clipped to within
    +/-5pp per asset of LAST rebalance's raw EVaR-optimal weights (recomputed
    statelessly off a trailing window shifted back ~21 sessions, since
    generate_signals carries no persisted state across calls), then
    renormalized to sum to 1. A colleague could as defensibly use a 10pp
    bound or a turnover-budget (sum of |delta|) constraint instead of a
    per-asset box constraint.
  - Rebalance cadence: MONTHLY (first trading day of each calendar month),
    matching `min_lookback_required=504` as the rolling estimation window.
The paper's unambiguous spec items are kept as-is: min_lookback_required=504
trading days, minimum_universe_size=30 (relaxed to a 20-of-30 coverage floor
for live data-gap tolerance — see MIN_ASSETS), instrument_class='etp'. Data:
close-price panel only (engine-supplied `prices`) — returns and
realized_vol are both "computed" per the spec's data_requirements, not
separate stored columns.
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from lib.convex import min_evar_weights

__all__ = ['GarchCopulaCvarScenario']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_garch_copula_cvar_scenario'

ETF_UNIVERSE = [
    'SPY', 'QQQ', 'IWM', 'DIA', 'EFA', 'EEM', 'VNQ', 'XLF', 'XLE', 'XLK',
    'XLV', 'XLI', 'XLU', 'XLB', 'TLT', 'IEF', 'SHY', 'LQD', 'HYG', 'TIP',
    'EMB', 'BND', 'AGG', 'GLD', 'SLV', 'USO', 'UNG', 'DBC', 'DBA', 'UUP',
]

LAMBDA_EWMA   = 0.94   # RiskMetrics GARCH-proxy decay
T_DF          = 6      # Student-t copula degrees of freedom
Z_LEVEL       = 0.95   # EVaR/CVaR confidence level
T_SCENARIOS   = 500    # Monte Carlo scenario draws per rebalance
TURNOVER_CAP  = 0.05   # max per-asset target-weight change per rebalance
REBAL_SHIFT   = 21     # ~1 trading month, for the stateless "previous rebalance" proxy


def _ewma_cond_var(sq_rets: pd.DataFrame, lam: float = LAMBDA_EWMA) -> pd.DataFrame:
    """RiskMetrics-style GARCH-proxy conditional variance recursion (causal)."""
    return sq_rets.ewm(alpha=(1.0 - lam), adjust=False).mean()


def _garch_copula_scenarios(returns: pd.DataFrame, rng: np.random.Generator,
                             df: int = T_DF, n_scen: int = T_SCENARIOS) -> np.ndarray | None:
    """Simulate an (n_scen x N) matrix of scenario returns: per-asset ARMA(0)-
    GARCH drift/vol forecast + a Student-t copula correlation structure drawn
    from GARCH-standardized residuals [pseudocode: fit ARMA-GARCH -> draw
    joint scenarios via Student-t copula]."""
    cond_var = _ewma_cond_var(returns.pow(2))
    sigma = np.sqrt(cond_var.clip(lower=1e-10))
    std_resid = (returns / sigma.shift(1)).dropna(how='any')
    if len(std_resid) < 60 or std_resid.shape[1] < 2:
        return None
    corr = np.corrcoef(std_resid.values, rowvar=False)
    if not np.all(np.isfinite(corr)):
        return None
    corr = (corr + corr.T) / 2.0
    eigvals = np.linalg.eigvalsh(corr)
    if eigvals.min() < -1e-8:
        corr = corr + np.eye(corr.shape[0]) * (abs(eigvals.min()) + 1e-6)
    try:
        chol = np.linalg.cholesky(corr)
    except np.linalg.LinAlgError:
        return None

    n = corr.shape[0]
    g = rng.standard_normal(size=(n_scen, n)) @ chol.T
    chi2 = rng.chisquare(df, size=n_scen)
    t_draws = g / np.sqrt(chi2 / df)[:, None]
    t_draws *= np.sqrt((df - 2.0) / df)   # normalize to unit variance

    mu_vec = returns.mean().values
    sigma_vec = np.sqrt(cond_var.iloc[-1].clip(lower=1e-10)).values
    scenario_ret = mu_vec[None, :] + sigma_vec[None, :] * t_draws
    return scenario_ret


def _raw_evar_weights(prices_window: pd.DataFrame, tickers: list,
                       rng: np.random.Generator) -> np.ndarray | None:
    """One rebalance's raw (pre-turnover-cap) minimum-EVaR weights from a
    GARCH+Student-t-copula scenario set."""
    returns = prices_window[tickers].pct_change().dropna(how='all')
    if len(returns) < 60:
        return None
    scenarios = _garch_copula_scenarios(returns, rng)
    if scenarios is None:
        return None
    losses = -scenarios
    w = min_evar_weights(losses, z=Z_LEVEL)
    return w


def _month_boundary(idx: pd.DatetimeIndex) -> bool:
    if len(idx) < 2:
        return False
    d1, d0 = pd.Timestamp(idx[-1]), pd.Timestamp(idx[-2])
    return d1.month != d0.month


class GarchCopulaCvarScenario(BaseStrategy):
    """LONG-only minimum-EVaR (CVaR-surrogate) monthly allocator across a
    fixed 30-ETF multi-asset universe, using ARMA(0)-GARCH conditional vol +
    a Student-t copula to build return scenarios, with target weights capped
    to a per-asset turnover bound vs. the prior rebalance (Mensah et al.
    2026)."""

    id                = STRATEGY_ID
    name              = 'GARCH-Copula CVaR Scenario Allocation'
    description       = ('LONG-only minimum-EVaR allocation across 30 cross-asset ETFs from '
                         'ARMA-GARCH + Student-t copula return scenarios, turnover-capped monthly '
                         'rebalance (Mensah et al. 2026).')
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = 504          # paper's min_lookback_required
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = len(ETF_UNIVERSE)

    MIN_ASSETS   = 20   # 20-of-30 coverage floor (relaxed from minimum_universe_size=30 for data gaps)
    MAX_WEIGHT   = 0.15
    HOLD_DAYS    = 21

    def default_parameters(self) -> dict:
        return {'z': Z_LEVEL, 'turnover_cap': TURNOVER_CAP, 'max_weight': self.MAX_WEIGHT}

    def _target_weights(self, prices_sub: pd.DataFrame, tickers: list) -> pd.Series | None:
        turnover_cap = float(self.parameters.get('turnover_cap', TURNOVER_CAP))
        rng = np.random.default_rng(42)

        raw = _raw_evar_weights(prices_sub, tickers, rng)
        if raw is None:
            return None

        prev_window = prices_sub.iloc[:-REBAL_SHIFT]
        prev_raw = None
        if len(prev_window) >= self.min_lookback:
            prev_raw = _raw_evar_weights(prev_window.tail(self.min_lookback), tickers,
                                          np.random.default_rng(42))
        if prev_raw is None or prev_raw.shape != raw.shape:
            prev_raw = raw   # first rebalance in sample: nothing to cap against yet

        delta = np.clip(raw - prev_raw, -turnover_cap, turnover_cap)
        target = prev_raw + delta
        target = np.maximum(target, 0.0)
        total = target.sum()
        if total < 1e-8:
            return None
        target = target / total
        return pd.Series(target, index=tickers)

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

        tickers = [t for t in ETF_UNIVERSE if t in prices.columns]
        if len(tickers) < self.MIN_ASSETS:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []

        prices_sub = prices[tickers].dropna(axis=1, how='any').tail(self.min_lookback + REBAL_SHIFT + 1)
        tickers = [t for t in tickers if t in prices_sub.columns]
        if len(tickers) < self.MIN_ASSETS or len(prices_sub) < self.min_lookback:
            print(f'[debug] signals=0 (insufficient history: rows={len(prices_sub)} cols={len(tickers)})',
                  file=sys.stderr)
            return []

        if not (_month_boundary(prices_sub.index) or self.cadence_reset(regime)):
            print('[debug] signals=0 (not a monthly rebalance day)', file=sys.stderr)
            return []

        try:
            weights = self._target_weights(prices_sub, tickers)
        except Exception as exc:
            print(f'[debug] signals=0 exc={exc}', file=sys.stderr)
            return []
        if weights is None:
            print('[debug] signals=0 (scenario optimizer unresolved)', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)
        max_weight = float(self.parameters.get('max_weight', self.MAX_WEIGHT))
        last_row = prices_sub.iloc[-1]

        signals: List[Signal] = []
        for ticker in tickers:
            w = float(weights.get(ticker, 0.0))
            if w < 0.01:
                continue
            price = float(last_row[ticker])
            if not (price == price) or price <= 0:
                continue
            series = prices_sub[ticker].dropna()
            stops = self.compute_stops_and_targets(series, 'LONG', price, regime_state=regime_state)
            pos = round(min(w, max_weight) * scale, 4)
            confidence = 'HIGH' if w > 0.10 else ('MED' if w > 0.04 else 'LOW')
            signals.append(Signal(
                ticker=ticker, direction='LONG', entry_price=price,
                stop_loss=stops['stop'], target_1=stops['t1'], target_2=stops['t2'], target_3=stops['t3'],
                position_size_pct=pos, confidence=confidence,
                signal_params={
                    'evar_weight':  round(w, 4),
                    'regime_scale': round(scale, 3),
                    'hold_days':    self.HOLD_DAYS,
                },
            ))

        signals = signals[:self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ── Standalone regime-partitioned backtest ────────────────────────────────────

def _run_backtest(prices_path: str, regimes_path: str,
                   start: str = '2017-01-01', end: str = '2025-12-31') -> pd.DataFrame:
    """Monthly walk-forward over `ETF_UNIVERSE`, returning a trades_df for
    regime partitioning (weighted-average realized return across the
    rebalanced book, one row per rebalance date)."""
    import os
    empty = pd.DataFrame(columns=['strategy_id', 'signal_date', 'regime_state', 'pnl', 'r_multiple'])
    if not os.path.exists(prices_path):
        return empty

    long_df = pd.read_parquet(prices_path, columns=['ticker', 'date', 'close'])
    long_df = long_df[long_df['ticker'].isin(ETF_UNIVERSE)]
    long_df['date'] = pd.to_datetime(long_df['date'])
    prices_df = long_df.pivot(index='date', columns='ticker', values='close').sort_index().loc[start:end]

    regimes: dict[str, str] = {}
    if os.path.exists(regimes_path):
        reg_df = pd.read_parquet(regimes_path)
        reg_df['date'] = pd.to_datetime(reg_df['date'])
        for _, row in reg_df.iterrows():
            regimes[str(row['date'].date())] = str(row['regime'])

    strategy = GarchCopulaCvarScenario()
    tickers = [t for t in ETF_UNIVERSE if t in prices_df.columns]
    hold_days = strategy.HOLD_DAYS
    records = []

    for i in range(strategy.min_lookback + REBAL_SHIFT, len(prices_df) - hold_days):
        snap = prices_df.iloc[:i + 1]
        dt = snap.index[-1]
        if not _month_boundary(snap.index):
            continue
        regime_state = regimes.get(str(dt.date()), 'LOW_VOL')
        sigs = strategy.generate_signals(snap, {'state': regime_state}, tickers)
        if not sigs:
            continue
        exit_dt = prices_df.index[i + hold_days]
        pnl_num, w_total = 0.0, 0.0
        for s in sigs:
            exit_px = prices_df[s.ticker].get(exit_dt)
            if exit_px is None or pd.isna(exit_px):
                continue
            ret = (float(exit_px) - s.entry_price) / s.entry_price
            w = s.signal_params['evar_weight']
            pnl_num += ret * w
            w_total += w
        if w_total < 1e-8:
            continue
        avg_pnl = pnl_num / w_total
        records.append({
            'strategy_id':  STRATEGY_ID,
            'signal_date':  str(dt.date()),
            'regime_state': regime_state,
            'pnl':          float(avg_pnl),
            'r_multiple':   float(avg_pnl / 0.02) if avg_pnl != 0 else 0.0,
        })

    return pd.DataFrame(records)


if __name__ == '__main__':
    import json
    import os
    from backtest.quick_backtest import run_backtest_with_regime_partition

    PARQUET_ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    trades_df = _run_backtest(
        prices_path=f'{PARQUET_ROOT}/prices.parquet',
        regimes_path=f'{PARQUET_ROOT}/historical_regimes.parquet',
    )
    print(f'Backtest produced {len(trades_df)} trades', file=sys.stderr)
    result = run_backtest_with_regime_partition(
        trades_df,
        strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(json.dumps(result, indent=2, default=str))
