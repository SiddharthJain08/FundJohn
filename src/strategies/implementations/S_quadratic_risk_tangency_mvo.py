"""
Quadratic Risk Tangency MVO — diagonal generalization of the Markowitz
tangency portfolio.

Source: https://arxiv.org/abs/2608.24449 — Gasparavičius & Grigutis (2026),
"Generalizing Markowitz Portfolio Optimization by a Quadratic Risk Measure".

Thesis: the paper replaces the covariance matrix Sigma in classical MVO with
an arbitrary symmetric positive-definite quadratic risk matrix Q (plus a
linear penalty term), giving a closed-form tangency portfolio
w* ∝ Q^-1 (mu - rf*1).

IMPLEMENTATION NOTE (deliberate simplification): estimating a full N x N
Q on a ~500-name universe from ~1-2 years of daily bars is a badly
underdetermined inverse problem (N^2/2 free parameters vs a few hundred
observations) — exactly the failure mode the coding standard for this repo
flags ("never use covariance-based optimization unless >= 3x more
observations than assets"). We instead take Q **diagonal**: each name's own
regularized (shrunk-to-cross-sectional-mean) variance, with all off-diagonal
risk terms set to zero. Under a diagonal Q the closed-form tangency solution
collapses to a per-asset scalar:

    w_i ∝ (mu_i - rf) / q_i

which is still a faithful instance of the paper's generalized framework (Q
need not be the sample covariance — here it is a regularized diagonal
proxy for it, echoing the "covariance regularization" variant in §3) while
remaining numerically stable and estimable with a single ticker's own
history. mu_i is proxied by cross-sectional momentum (6-month total return,
annualized); q_i is realized variance (3-month, annualized) shrunk toward
the universe-median variance to damp single-name noise; rf is the 3-month
T-bill yield from macro.parquet. Top-decile scores go LONG, bottom-decile
go SHORT — a dollar-neutral-ish cross-sectional tangency book rather than a
single fully-invested portfolio, since the platform contract emits
per-ticker signals rather than one N-vector of weights.
"""
from __future__ import annotations

import math
import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['QuadraticRiskTangencyMVO']

INSTRUMENT_CLASS = 'equity'

STRATEGY_ID    = 'S_quadratic_risk_tangency_mvo'
MOM_LOOKBACK   = 126   # trading days (~6 months) — expected-return (mu) proxy
VOL_LOOKBACK   = 63    # trading days (~3 months) — diagonal risk (q) proxy
MIN_LOOKBACK   = 150   # warmup before first signal
MIN_ASSETS     = 40    # minimum scored names to form a reliable decile split
SHRINK_LAMBDA  = 0.5   # 0=full shrink to universe-median var, 1=no shrink
RF_FALLBACK    = 0.03  # annualized, used when macro DGS3MO is unavailable
RF_SERIES      = 'DGS3MO'
TOTAL_ALLOC    = 0.50  # pre-regime-scale gross allocation, per side (long/short)


def _rf_from_macro(macro, index: pd.DatetimeIndex) -> float:
    """Latest 3-month T-bill yield (annualized, decimal) as-of the last date
    in `index`. Falls back to RF_FALLBACK when macro data is missing —
    mirrors the pattern in S_ast_fed_model.py."""
    if not macro or RF_SERIES not in macro:
        return RF_FALLBACK
    ser = macro[RF_SERIES]
    if ser is None or len(ser) == 0:
        return RF_FALLBACK
    ser = pd.Series(ser).dropna()
    if ser.empty:
        return RF_FALLBACK
    if not isinstance(ser.index, pd.DatetimeIndex):
        ser.index = pd.to_datetime(ser.index)
    ser = ser.sort_index()
    as_of = index[-1] if len(index) else ser.index[-1]
    ser = ser[ser.index <= as_of]
    if ser.empty:
        return RF_FALLBACK
    val = float(ser.iloc[-1])
    # DGS3MO is reported in percent (e.g. 5.25 == 5.25%)
    return val / 100.0 if val > 1.0 else val


class QuadraticRiskTangencyMVO(BaseStrategy):
    """Cross-sectional tangency-style long/short: score = (mu - rf) / q,
    where q is a shrunk diagonal (per-asset) risk proxy standing in for the
    paper's generalized quadratic risk matrix Q. Top decile LONG, bottom
    decile SHORT, rebalanced monthly.
    Source: https://arxiv.org/abs/2608.24449
    """

    id                = STRATEGY_ID
    name              = 'Quadratic Risk Tangency MVO'
    description       = (
        'Diagonal-Q generalized-Markowitz tangency score (momentum mu, '
        'shrunk realized-variance q, macro risk-free rate): LONG top decile, '
        'SHORT bottom decile, rebalanced monthly.'
    )
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = MIN_LOOKBACK
    # Regime-partitioned backtest (2398 monthly long+short legs, 2017-2025
    # SP500 panel) shows positive avg-R in LOW_VOL / TRANSITIONING / CRISIS
    # (0.75 / 0.13 / 2.49) but negative avg-R in HIGH_VOL (-1.92) — the
    # dollar-neutral-ish decile spread does not hold up when the whole cross
    # section is being repriced together. None of the four regimes clear the
    # 0.5-Sharpe auto-promotion threshold on this window; restricting to the
    # sign-positive regimes per the fundjohn:backtest-plumb convention rather
    # than claiming the paper's untested "closed-form, all-conditions" framing.
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'CRISIS']
    MAX_SIGNALS       = 50

    def default_parameters(self) -> dict:
        return {
            'mom_lookback':  MOM_LOOKBACK,
            'vol_lookback':  VOL_LOOKBACK,
            'min_assets':    MIN_ASSETS,
            'shrink_lambda': SHRINK_LAMBDA,
            'total_alloc':   TOTAL_ALLOC,
        }

    def _is_month_boundary(self, prices: pd.DataFrame) -> bool:
        if not isinstance(prices.index, pd.DatetimeIndex) or len(prices) < 2:
            return True
        return prices.index[-1].month != prices.index[-2].month

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

        if not (self._is_month_boundary(prices) or self.cadence_reset(regime)):
            print('[debug] signals=0', file=sys.stderr)
            return []

        if len(prices) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        mom_lb   = int(self.parameters.get('mom_lookback', MOM_LOOKBACK))
        vol_lb   = int(self.parameters.get('vol_lookback', VOL_LOOKBACK))
        min_ast  = int(self.parameters.get('min_assets', MIN_ASSETS))
        shrink   = float(self.parameters.get('shrink_lambda', SHRINK_LAMBDA))
        alloc    = float(self.parameters.get('total_alloc', TOTAL_ALLOC))

        candidates = [t for t in universe if t in prices.columns] if universe else list(prices.columns)
        if not candidates:
            print('[debug] signals=0', file=sys.stderr)
            return []

        macro = (aux_data or {}).get('macro')
        rf = _rf_from_macro(macro, prices.index)

        mus:   dict = {}
        vars_: dict = {}
        px:    dict = {}
        for ticker in candidates:
            series = prices[ticker].dropna()
            if len(series) < mom_lb + 1 or len(series) < vol_lb + 1:
                continue
            current_price = float(series.iloc[-1])
            past_price = float(series.iloc[-mom_lb - 1])
            if current_price <= 0 or past_price <= 0:
                continue
            mu = (current_price / past_price) ** (252.0 / mom_lb) - 1.0
            log_rets = (series / series.shift(1)).apply(math.log).dropna().iloc[-vol_lb:]
            if log_rets.empty:
                continue
            var = float(log_rets.std()) ** 2 * 252.0
            if not (math.isfinite(mu) and math.isfinite(var)) or var <= 0:
                continue
            mus[ticker] = mu
            vars_[ticker] = var
            px[ticker] = current_price

        if len(mus) < min_ast:
            print('[debug] signals=0', file=sys.stderr)
            return []

        median_var = sorted(vars_.values())[len(vars_) // 2]

        scores: dict = {}
        qs:     dict = {}
        for ticker, mu in mus.items():
            q = shrink * vars_[ticker] + (1.0 - shrink) * median_var
            if q <= 0:
                continue
            qs[ticker] = q
            scores[ticker] = (mu - rf) / q

        if len(scores) < min_ast:
            print('[debug] signals=0', file=sys.stderr)
            return []

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        n_side = max(1, min(self.MAX_SIGNALS // 2, len(ranked) // 10))
        longs  = ranked[:n_side]
        shorts = ranked[-n_side:]

        scale = self.position_scale(regime_state)

        def _weights(group):
            mags = [abs(s) for _, s in group]
            total = sum(mags)
            if total <= 0:
                n = len(group)
                return {t: 1.0 / n for t, _ in group}
            return {t: abs(s) / total for t, s in group}

        long_w  = _weights(longs)
        short_w = _weights(shorts)

        long_mags  = sorted(abs(s) for _, s in longs)
        short_mags = sorted(abs(s) for _, s in shorts)

        def _confidence(mag: float, mags: list) -> str:
            if not mags:
                return 'MED'
            lo = mags[len(mags) // 3]
            hi = mags[(2 * len(mags)) // 3]
            if mag >= hi:
                return 'HIGH'
            if mag <= lo:
                return 'LOW'
            return 'MED'

        signals: List[Signal] = []

        for ticker, score in longs:
            series = prices[ticker].dropna()
            current_price = px[ticker]
            stops = self.compute_stops_and_targets(
                series, direction='LONG', current_price=current_price, regime_state=regime_state,
            )
            w = long_w[ticker]
            pos_size = min(round(w * alloc * scale, 4), 1.0)
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = pos_size,
                confidence        = _confidence(abs(score), long_mags),
                signal_params     = {
                    'mu':     round(mus[ticker], 4),
                    'q':      round(qs[ticker], 6),
                    'rf':     round(rf, 4),
                    'score':  round(score, 4),
                    'weight': round(w, 4),
                    'regime': regime_state,
                    'scale':  round(scale, 4),
                },
                features          = {'tangency_score': round(score, 4)},
            ))

        for ticker, score in shorts:
            series = prices[ticker].dropna()
            current_price = px[ticker]
            stops = self.compute_stops_and_targets(
                series, direction='SHORT', current_price=current_price, regime_state=regime_state,
            )
            w = short_w[ticker]
            pos_size = min(round(w * alloc * scale, 4), 1.0)
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'SHORT',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = pos_size,
                confidence        = _confidence(abs(score), short_mags),
                signal_params     = {
                    'mu':     round(mus[ticker], 4),
                    'q':      round(qs[ticker], 6),
                    'rf':     round(rf, 4),
                    'score':  round(score, 4),
                    'weight': round(w, 4),
                    'regime': regime_state,
                    'scale':  round(scale, 4),
                },
                features          = {'tangency_score': round(score, 4)},
            ))

        signals = signals[: self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ---------------------------------------------------------------------------
# Regime-partitioned backtest
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import json as _json
    import numpy as np
    from backtest.unified_backtest import load_prices_panels, load_regimes

    prices_df, _bars = load_prices_panels()
    reg_series = load_regimes()

    log_rets = (prices_df / prices_df.shift(1)).apply(np.log)
    monthly_idx = pd.Series(prices_df.index, index=prices_df.index).groupby(
        prices_df.index.to_period('M')
    ).last().values
    monthly_idx = pd.DatetimeIndex(monthly_idx)

    RF_STANDALONE = 0.03  # fixed proxy — macro parquet not wired into this standalone loader

    rows = []
    for i in range(1, len(monthly_idx) - 1):
        reb_date  = monthly_idx[i]
        next_date = monthly_idx[i + 1]

        px_hist = prices_df.loc[:reb_date]
        if len(px_hist) < MOM_LOOKBACK + 1:
            continue
        mom_window = px_hist.iloc[-MOM_LOOKBACK - 1:]
        mu = (mom_window.iloc[-1] / mom_window.iloc[0]) ** (252.0 / MOM_LOOKBACK) - 1.0

        vol_window = log_rets.loc[:reb_date].iloc[-VOL_LOOKBACK:]
        if len(vol_window) < 20:
            continue
        var = (vol_window.std() ** 2) * 252.0

        valid = mu.index.intersection(var.index)
        mu = mu[valid].replace([np.inf, -np.inf], np.nan).dropna()
        var = var[valid].replace([np.inf, -np.inf], np.nan)
        var = var[(var > 0) & var.notna()]
        common = mu.index.intersection(var.index)
        mu, var = mu[common], var[common]
        if len(mu) < MIN_ASSETS:
            continue

        median_var = var.median()
        q = SHRINK_LAMBDA * var + (1 - SHRINK_LAMBDA) * median_var
        score = (mu - RF_STANDALONE) / q
        score = score.replace([np.inf, -np.inf], np.nan).dropna()
        if len(score) < MIN_ASSETS:
            continue

        ranked = score.sort_values(ascending=False)
        n_side = max(1, min(25, len(ranked) // 10))
        longs  = ranked.iloc[:n_side]
        shorts = ranked.iloc[-n_side:]

        long_w  = longs.abs() / longs.abs().sum()
        short_w = shorts.abs() / shorts.abs().sum()

        entry_prices = prices_df.loc[reb_date]
        exit_prices  = prices_df.loc[next_date]

        prior_regimes = reg_series[reg_series.index <= reb_date]
        rstate = str(prior_regimes.iloc[-1]) if not prior_regimes.empty else 'LOW_VOL'

        for ticker, w in long_w.items():
            ep, xp = entry_prices.get(ticker), exit_prices.get(ticker)
            if ep is None or xp is None or pd.isna(ep) or pd.isna(xp) or float(ep) <= 0:
                continue
            pnl = (float(xp) - float(ep)) / float(ep)
            rows.append({'strategy_id': STRATEGY_ID, 'signal_date': reb_date, 'regime_state': rstate,
                         'pnl': pnl * float(w), 'r_multiple': round(pnl / 0.02, 4)})

        for ticker, w in short_w.items():
            ep, xp = entry_prices.get(ticker), exit_prices.get(ticker)
            if ep is None or xp is None or pd.isna(ep) or pd.isna(xp) or float(ep) <= 0:
                continue
            pnl = (float(ep) - float(xp)) / float(ep)
            rows.append({'strategy_id': STRATEGY_ID, 'signal_date': reb_date, 'regime_state': rstate,
                         'pnl': pnl * float(w), 'r_multiple': round(pnl / 0.02, 4)})

    trades_df = pd.DataFrame(rows)
    print(f'[backtest] {len(trades_df)} trades', file=sys.stderr)

    from backtest.quick_backtest import run_backtest_with_regime_partition
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps(result, indent=2, default=str))
