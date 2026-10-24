"""
S_amortized_neural_filter_stat_arb_tv2 — Amortized Neural Filtering for
Calibrated Posteriors in Statistical Arbitrage (Chang, Minkey, 2026,
arXiv:2610.25802v2).

Hypothesis: a filter trained to return a calibrated POSTERIOR (mean mu_t AND
variance sigma2_t), not just a point estimate, over each spread's latent
mean-reverting state closes most of the realized-vs-forecast gap in stat
arb — because sizing by mu_t / sigma2_t shrinks exposure toward FLAT exactly
when the estimate is least trustworthy.

Variant 2/2 — only an abstract is available, so this reading is deliberately
DIFFERENT from a plausible colleague's (variant 1 of this strategy used a
recursive scalar Kalman filter with method-of-moments calibration over a
single-factor market-residual spread):
  - "Amortized ... in one forward pass" is taken LITERALLY: the posterior is
    computed in a single closed-form Bayesian linear-regression step (a
    conjugate Normal-Inverse-Gamma fit of the AR(1) coefficient phi and
    innovation variance sigma^2), never a per-timestep recursion. This is
    the one-shot analogue of a recursive Kalman filter, not the filter
    itself.
  - The idiosyncratic spread is REGRESSION-FREE: no per-ticker beta is
    estimated. Each ticker's spread is its cumulative (log-return minus the
    cross-sectional MEDIAN log-return) — median, not mean, for robustness
    to a handful of extreme movers, and no market-model fit is needed.
  - Calibration is empirical-Bayes NIG (weakly-informative prior scaled off
    the window's own sample variance), not method-of-moments on sample
    autocovariance.
  - Rebalance cadence, lookback window, and entry threshold are each set to
    different values than variant 1 (180d window / 10d step / 15d hold /
    entry_t=1.25 here).
The paper's unambiguous spec items are kept as-is: direction_vocab
{LONG, SHORT}, minimum_universe_size=100, min_lookback_required=756 trading
days, regime_applicability {LOW_VOL, TRANSITIONING, HIGH_VOL}, and the
reversion-direction convention (mu_t > 0 implies reversion DOWN -> SHORT;
mu_t < 0 implies reversion UP -> LONG). Data: close price panel only
(engine-supplied `prices`) — no financials/options/macro.
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from strategies.safe_ops import EPS, safe_div
from src.strategies.universe_default import r1000 as universe_filter

__all__ = ['AmortizedNeuralFilterStatArbTv2']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_amortized_neural_filter_stat_arb_tv2'


def _nig_one_shot_posterior(z_win: np.ndarray, kappa0: float, a0: float, b0_frac: float) -> tuple:
    """Closed-form (single forward pass, no recursion) conjugate
    Normal-Inverse-Gamma posterior for an AR(1) fit y_t = phi*y_{t-1} + eps,
    eps ~ N(0, sigma^2), with a weak prior phi0=0 and a variance-scaled prior
    strength b0 = b0_frac * sample_variance. Returns the one-step-ahead
    predictive (mu_pred, var_pred) at the window's last observation, folding
    in BOTH parameter (phi) uncertainty and residual-noise uncertainty."""
    gamma0 = float(np.var(z_win))
    if gamma0 < EPS:
        return 0.0, EPS
    x, y = z_win[:-1], z_win[1:]
    n = len(x)
    if n < 10:
        return 0.0, EPS
    sxx = float(np.sum(x * x))
    sxy = float(np.sum(x * y))
    syy = float(np.sum(y * y))

    b0 = max(b0_frac * gamma0, EPS)
    kappa_n = kappa0 + sxx
    phi_n = safe_div(sxy, kappa_n)
    a_n = a0 + n / 2.0
    b_n = max(b0 + 0.5 * (syy - kappa_n * phi_n * phi_n), EPS)

    sigma2_mean = safe_div(b_n, max(a_n - 1.0, EPS))
    var_phi = safe_div(sigma2_mean, kappa_n)

    x_last = float(z_win[-1])
    mu_pred = phi_n * x_last
    var_pred = (x_last * x_last) * var_phi + sigma2_mean
    return float(mu_pred), float(max(var_pred, EPS))


class AmortizedNeuralFilterStatArbTv2(BaseStrategy):
    """One-shot conjugate-Bayes posterior (mu_t, sigma2_t) of regression-free,
    median-relative idiosyncratic spreads; sizes mean-reversion trades by
    mu_t/sigma2_t so conviction shrinks toward FLAT as posterior uncertainty
    rises."""

    id                = STRATEGY_ID
    name              = 'Amortized Neural Filter Stat Arb (TV2)'
    description       = ('One-shot NIG-Bayes posterior (mu_t, sigma2_t) of regression-free, '
                         'cross-sectional-median-relative idiosyncratic spreads; LONG/SHORT by '
                         'sign of mu_t, sized by mu_t/sigma2_t, gated on the posterior t-stat.')
    tier              = 2
    min_lookback      = 756          # paper's min_lookback_required
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 30

    AR_WINDOW    = 180    # window for the one-shot NIG fit (distinct from variant 1's 120)
    MIN_UNIVERSE = 100    # paper's minimum_universe_size
    ENTRY_T      = 1.25   # |mu_t / sqrt(sigma2_t)| credibility gate (distinct from variant 1's 1.00)
    BASE_SIZE    = 0.003
    KAPPA0       = 1.0    # prior precision on phi
    A0           = 2.0    # prior InvGamma shape
    B0_FRAC      = 0.05   # prior InvGamma scale, as a fraction of window sample variance

    def default_parameters(self) -> dict:
        return {'ar_window': self.AR_WINDOW, 'entry_t': self.ENTRY_T}

    def _score_universe(self, price_mat: pd.DataFrame, ar_window: int) -> list:
        """Per-ticker one-shot NIG posterior (mu_t, sigma2_t, tstat) on a
        regression-free, median-relative idiosyncratic spread."""
        log_ret = np.log(price_mat).diff().dropna()
        if log_ret.shape[0] < ar_window + 10:
            return []
        xs_median = log_ret.median(axis=1)

        out = []
        for ticker in log_ret.columns:
            idio = (log_ret[ticker] - xs_median).to_numpy()
            x = np.cumsum(idio)
            z_win = x[-ar_window:]
            z_win = z_win - z_win.mean()   # re-center: no drift carried across windows
            mu_t, sigma2_t = _nig_one_shot_posterior(z_win, self.KAPPA0, self.A0, self.B0_FRAC)
            if not np.isfinite(mu_t) or not np.isfinite(sigma2_t) or sigma2_t <= 0:
                continue
            tstat = safe_div(mu_t, float(np.sqrt(sigma2_t)))
            out.append((ticker, mu_t, sigma2_t, float(tstat)))
        return out

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or len(prices) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < self.MIN_UNIVERSE:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []

        ar_window = int(self.parameters.get('ar_window', self.AR_WINDOW))
        price_mat = prices[tickers].tail(self.min_lookback).dropna(axis=1, thresh=self.min_lookback // 2)
        if price_mat.shape[0] < ar_window + 20 or price_mat.shape[1] < self.MIN_UNIVERSE:
            print(f'[debug] signals=0 (insufficient history/universe: {price_mat.shape})', file=sys.stderr)
            return []

        scored = self._score_universe(price_mat, ar_window)
        if not scored:
            print('[debug] signals=0 (no scorable spreads)', file=sys.stderr)
            return []

        entry_t = float(self.parameters.get('entry_t', self.ENTRY_T))
        scale = self.position_scale(regime_state)
        latest_prices = price_mat.iloc[-1]
        scored.sort(key=lambda row: abs(row[3]), reverse=True)

        signals: List[Signal] = []
        for ticker, mu_t, sigma2_t, tstat in scored:
            if len(signals) >= self.MAX_SIGNALS:
                break
            if abs(tstat) < entry_t:
                continue
            px = float(latest_prices.get(ticker, np.nan))
            if np.isnan(px) or px <= 0:
                continue
            # mu_t > 0: predicted next-bar deviation sits above equilibrium ->
            # expect reversion DOWN. mu_t < 0 -> expect reversion UP.
            direction = 'SHORT' if mu_t > 0 else 'LONG'

            st = self.compute_stops_and_targets(price_mat[ticker], direction, px, regime_state=regime_state)
            conf = 'HIGH' if abs(tstat) > 2.5 else ('MED' if abs(tstat) > 1.5 else 'LOW')
            raw_size = safe_div(abs(mu_t), sigma2_t)
            size = min(self.BASE_SIZE * scale * (1.0 + np.log1p(raw_size)), self.BASE_SIZE * scale * 3.0)

            signals.append(Signal(
                ticker=ticker,
                direction=direction,
                entry_price=round(px, 4),
                stop_loss=round(float(st['stop']), 4),
                target_1=round(float(st['t1']), 4),
                target_2=round(float(st['t2']), 4),
                target_3=round(float(st['t3']), 4),
                position_size_pct=round(float(size), 6),
                confidence=conf,
                signal_params={
                    'mu_t':             round(mu_t, 6),
                    'sigma2_t':         round(sigma2_t, 6),
                    'posterior_tstat':  round(tstat, 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ---------------------------------------------------------------------------
# Regime-partitioned backtest
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import os
    import json as _json

    from backtest.quick_backtest import run_backtest_with_regime_partition

    PARQUET_ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    long_df = pd.read_parquet(f'{PARQUET_ROOT}/prices.parquet', columns=['ticker', 'date', 'close'])
    long_df['date'] = pd.to_datetime(long_df['date'])
    prices_df = long_df.pivot(index='date', columns='ticker', values='close').sort_index()

    reg_df = pd.read_parquet(f'{PARQUET_ROOT}/historical_regimes.parquet')
    reg_df['date'] = pd.to_datetime(reg_df['date'])
    reg_df = reg_df.sort_values('date')

    def _regime_on(dt):
        mask = reg_df['date'] <= dt
        return str(reg_df.loc[mask, 'regime'].iloc[-1]) if mask.any() else 'LOW_VOL'

    strategy = AmortizedNeuralFilterStatArbTv2()
    universe = list(prices_df.columns)
    HOLD_DAYS = 15   # distinct from variant 1's 10d hold
    STEP = 10        # distinct from variant 1's 5d re-score cadence

    rows = []
    for i in range(strategy.min_lookback, len(prices_df) - HOLD_DAYS, STEP):
        snap = prices_df.iloc[:i]
        dt = snap.index[-1]
        regime_s = _regime_on(dt)
        signals = strategy.generate_signals(snap, {'state': regime_s}, universe)
        exit_dt = prices_df.index[i + HOLD_DAYS]
        for s in signals:
            exit_px = prices_df[s.ticker].get(exit_dt)
            if exit_px is None or pd.isna(exit_px):
                continue
            ret = (float(exit_px) - s.entry_price) / s.entry_price
            if s.direction == 'SHORT':
                ret = -ret
            risk = abs(s.entry_price - s.stop_loss)
            rows.append({
                'strategy_id':  STRATEGY_ID,
                'signal_date':  str(dt.date()),
                'regime_state': regime_s,
                'pnl':          float(ret),
                'r_multiple':   float(ret * s.entry_price / risk) if risk > 0 else 0.0,
            })

    if not rows:
        print('No trades generated', file=sys.stderr)
        sys.exit(1)

    result = run_backtest_with_regime_partition(
        pd.DataFrame(rows), strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps(result, indent=2, default=str))
