"""S_diversified_risk_perception_es_tv2 — long-only cross-sectional tilt toward
names with the lowest Diversified-Risk-Perception Expected Shortfall (DRP-ES),
where DRP-ES aggregates ES across several DIFFERENT loss-distribution models
via the entropic (log-sum-exp) risk aggregation, not a softmax-weighted
average.

Source: http://arxiv.org/abs/2611.11803v1 "Diversified-Risk-Perception Risk
Measures and Portfolio Selection" (Liu, Liu, Shen, 2026).

Thesis: a single ES estimator can be badly wrong under model
misspecification; aggregating ES across several risk models with a function
that explicitly upweights whichever model is currently most pessimistic
yields a more robust tail-risk score than trusting one model. The repo bans
covariance-based optimizers at this universe size, so the paper's "solve
portfolio weights to minimize DRP_ES(x)" step is proxied by a per-asset
DRP-ES score used for cross-sectional selection, never a solved covariance
optimization.

Interpretation variant 2 of 2. The abstract/pseudocode are unambiguous that
the measure aggregates ES across several risk models via an exponential,
loss-upweighting function. Every free choice below is a DIFFERENT defensible
resolution than variant 1's (softmax-weighted-average, K=3 models, ALPHA=
0.975, bottom-30% inverse-risk-weighted, monthly, TRANSITIONING/HIGH_VOL/
CRISIS):
  - K = 2 risk models: (1) historical-simulation ES, (2) a parametric
    Student-t ES whose degrees of freedom are fit per-name from sample
    excess kurtosis (method-of-moments nu = 6/kurt + 4). A fitted Student-t
    already captures fat tails directly, so a third, separate heavy-tail
    model (as variant 1's Cornish-Fisher) is read as redundant rather than
    required — the spec's "e.g." list is a set of examples, not a mandated
    cardinality.
  - ES confidence level ALPHA = 0.95 (the conventional CVaR convention,
    rather than variant 1's deeper 0.975 cut).
  - Aggregation is the ENTROPIC / log-sum-exp risk measure — a literal
    reading of "exponential aggregation function" as the standard
    convex risk-aggregation operator (this is also the family for which
    the paper's §4 sensitivity bounds are classically derived via Holder's
    inequality, not an ad hoc softmax):
        DRP_ES = (1/GAMMA) * log( mean_k( exp(GAMMA * ES_k) ) )
    As GAMMA -> infinity this limits to max_k(ES_k) (the single worst
    model dominates); as GAMMA -> 0 it limits to the plain mean of the
    models. GAMMA = 3.0 sits between those extremes.
  - Selection is LONG-ONLY (direction_vocab permits only LONG): take a
    FIXED top quintile (lowest 20%) of DRP-ES, EQUAL-weighted — a direct
    reading of "asset diversification" as naive (1/N) diversification
    across the safest names, rather than variant 1's continuous
    inverse-risk weighting.
  - Rebalance cadence is WEEKLY, reacting faster to shifting tail risk
    than variant 1's monthly cadence (the spec says only "rebalance
    periodically").
  - Eligible regimes take the spec's own self-reported
    `regime_applicability` (["HIGH_VOL", "NEUTRAL", "TRANSITIONING"])
    literally, mapping the non-canonical "NEUTRAL" tag to the calm
    canonical state LOW_VOL rather than dropping it or extending into
    CRISIS: LOW_VOL, TRANSITIONING, HIGH_VOL. CRISIS is excluded since
    the paper never claims empirical support there.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List
from scipy.stats import t as student_t

from strategies.base import BaseStrategy, Signal
from strategies.universe_default import sp500 as universe_filter
from strategies.safe_ops import EPS

__all__ = ['DiversifiedRiskPerceptionEsTv2']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_diversified_risk_perception_es_tv2'

ALPHA          = 0.95       # ES tail confidence level
GAMMA          = 3.0        # entropic-aggregation sharpness upweighting the worst model
LOOKBACK       = 756        # spec min_lookback_required (~3y trading sessions)
MIN_UNIVERSE   = 100        # spec minimum_universe_size
SELECT_FRAC    = 0.20       # lowest-DRP-ES quintile of the eligible universe to hold
GROSS_EXPOSURE = 0.55       # total book weight across the selected longs
BASE_SIZE_CAP  = 0.05       # per-name position_size_pct ceiling


def _student_t_es(returns: pd.DataFrame, alpha: float = ALPHA) -> pd.Series:
    """Per-ticker parametric Student-t ES, nu fit from sample excess kurtosis."""
    losses = -returns
    mu    = losses.mean()
    sigma = losses.std(ddof=1)
    exkurt = returns.kurt()
    nu = (6.0 / exkurt.clip(lower=0.2) + 4.0).clip(lower=4.1, upper=60.0)

    es = pd.Series(index=losses.columns, dtype=float)
    for col in losses.columns:
        dof = float(nu[col])
        scale = float(sigma[col]) * np.sqrt((dof - 2.0) / dof) if dof > 2.0 else float(sigma[col])
        t_alpha = student_t.ppf(alpha, dof)
        c_alpha = (student_t.pdf(t_alpha, dof) * (dof + t_alpha ** 2)) / ((dof - 1.0) * (1.0 - alpha))
        es[col] = float(mu[col]) + scale * c_alpha
    return es


def _drp_es_scores(returns: pd.DataFrame, alpha: float = ALPHA, gamma: float = GAMMA) -> pd.Series:
    """Per-ticker Diversified-Risk-Perception ES: entropic (log-sum-exp)
    aggregation of a historical-simulation ES and a parametric Student-t ES."""
    losses = -returns
    var_alpha = losses.quantile(alpha)
    tail      = losses.where(losses.ge(var_alpha, axis=1))
    es_hist   = tail.mean()
    es_t      = _student_t_es(returns, alpha)

    stacked = np.vstack([es_hist.values, es_t.values])
    lse = (1.0 / gamma) * np.log(np.exp(gamma * stacked).mean(axis=0))

    out = pd.Series(lse, index=es_hist.index)
    out.attrs['es_hist'] = es_hist
    out.attrs['es_t']    = es_t
    return out


class DiversifiedRiskPerceptionEsTv2(BaseStrategy):
    """Long-only tilt toward the lowest entropic-aggregated multi-model ES (DRP-ES) names."""

    id                = STRATEGY_ID
    name              = 'DiversifiedRiskPerceptionEsTv2'
    description       = 'Long the lowest log-sum-exp aggregated multi-model ES (DRP-ES) quintile, variant 2 (Liu, Liu & Shen 2026)'
    tier              = 2
    signal_frequency  = 'weekly'
    min_lookback      = LOOKBACK
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
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

        scale = self.position_scale(regime_state)

        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < MIN_UNIVERSE:
            print('[debug] signals=0', file=sys.stderr)
            return []

        window = prices[tickers].iloc[-(LOOKBACK + 1):]
        window = window.dropna(axis=1)
        if len(window) < LOOKBACK + 1 or window.shape[1] < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        returns = window.pct_change().dropna(how='all')
        if len(returns) < LOOKBACK:
            print('[debug] signals=0', file=sys.stderr)
            return []

        drp_es = _drp_es_scores(returns).replace([np.inf, -np.inf], np.nan).dropna()
        if drp_es.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        n_select = max(1, min(self.MAX_SIGNALS, int(round(len(drp_es) * SELECT_FRAC))))
        selected = drp_es.sort_values(ascending=True).iloc[:n_select]

        equal_weight = (GROSS_EXPOSURE / max(1, n_select))
        equal_weight = min(equal_weight, BASE_SIZE_CAP)

        rank_pct = selected.rank(pct=True, ascending=False)  # 1.0 = safest of the selected
        current_prices = window.iloc[-1]
        es_hist, es_t = drp_es.attrs.get('es_hist'), drp_es.attrs.get('es_t')

        def conf(pct: float) -> str:
            if pct >= 0.67:
                return 'HIGH'
            elif pct >= 0.33:
                return 'MED'
            return 'LOW'

        signals: List[Signal] = []
        for ticker, score in selected.items():
            if len(signals) >= self.MAX_SIGNALS:
                break
            raw_price = current_prices.get(ticker)
            if raw_price is None or raw_price != raw_price or raw_price <= 0:
                continue
            price = float(raw_price)
            size = round(float(equal_weight) * scale, 6)
            if size <= 0:
                continue
            stops = self.compute_stops_and_targets(
                window[ticker].dropna(), 'LONG', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=size,
                confidence=conf(float(rank_pct[ticker])),
                signal_params={
                    'drp_es': round(float(score), 6),
                    'es_historical': round(float(es_hist.get(ticker, float('nan'))), 6) if es_hist is not None else None,
                    'es_student_t': round(float(es_t.get(ticker, float('nan'))), 6) if es_t is not None else None,
                    'rank_pct_safest': round(float(rank_pct[ticker]), 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
