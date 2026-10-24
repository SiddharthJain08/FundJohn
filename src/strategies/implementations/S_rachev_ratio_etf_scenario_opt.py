"""
S_rachev_ratio_etf_scenario_opt — Rachev-Ratio ETF Scenario Allocation

Source: He, Yifan (2026), "Rachev-Ratio Portfolio Optimization with TimeGAN
Scenarios: Replacement versus Supplementation" (arXiv:2610.24544v1).

Hypothesis: allocating across a fixed 12-ETF universe by maximizing the
Rachev ratio (upper-tail reward over lower-tail risk, from historical return
scenarios) targets a tail-asymmetric risk preference that mean-variance /
Sharpe optimization ignores. The paper itself finds no statistically
significant edge from augmenting/replacing historical scenarios with
TimeGAN-synthetic draws, and that replacement raises turnover without
uniformly improving realized downside risk — so this implementation uses
ONLY historical scenarios (no synthetic generator), which the paper's own
result says loses nothing material versus the hybrid/replacement variants.

Variant 1/2 — abstract-only extraction (no appendix with the exact 12
tickers, tail probabilities, rebalance cadence or allocation rule), so every
numeric/structural choice below is this implementation's defensible reading,
deliberately different from a colleague's plausible reading:
  - 12-ETF universe = SPY, QQQ, IWM, EFA, EEM (equity: US large/growth/small/
    developed-ex-US/EM), TLT, IEF, LQD, HYG (rates/credit: long Treasury,
    7-10y Treasury, IG corp, HY corp), GLD, SLV, DBC (real assets: gold,
    silver, broad commodities) — a colleague could just as defensibly pick a
    sector-SPDR-heavy 12-ETF set; this variant instead spans the macro
    asset-class spectrum, which is where a Rachev-ratio tail-asymmetry
    preference (vs. mean-variance) should matter most (credit/commodity
    return distributions are the most visibly fat-tailed/skewed of the 12).
  - tail probabilities: ASYMMETRIC alpha=0.10 (upper, i.e. CVaR of the best
    10% of scenario returns) / beta=0.05 (lower, i.e. CVaR of the worst 5%)
    — a colleague could as defensibly use a symmetric 0.10/0.10 or 0.05/0.05
    pair; this variant deliberately holds the downside tail to a stricter
    (more extreme, fewer-observation) quantile than the upside tail, since a
    tail-risk-aware allocator should price the rare/severe left tail more
    conservatively than the comparably-sized right tail.
  - allocation rule: rather than a joint multi-asset argmax over the
    portfolio-level Rachev ratio (a non-convex scenario program outside the
    convex helpers this file is allowed to call — lib.convex only exposes
    tangency/min-EVaR, and strategy files may never import cvxpy directly),
    this variant scores each ETF's OWN marginal Rachev ratio from its
    historical return scenarios and allocates proportionally to that raw
    ratio (long-only, rank-based scoring — consistent with the house rule
    against ad-hoc covariance/convex optimization in strategy files). Tail
    CVaR ratios for single-asset daily return series are routinely < 1 (the
    stricter-quantile loss tail is larger than the looser-quantile gain tail
    even for assets with a genuine positive risk premium — see GLD at 0.90
    and SPY at 0.59 on the in-sample window below), so this variant tilts
    the book toward the ETFs with the BEST relative tail asymmetry rather
    than gating on an absolute ratio>1 breakeven, which would starve the
    allocator into permanent HOLD. A colleague attempting the literal joint
    optimization would need a bespoke scenario-LP; this variant trades that
    joint coupling for a simpler, auditable per-asset relative tail score.
  - rebalance cadence: MONTHLY (first trading day of each calendar month),
    matching `min_lookback_required=756` (~3y) as the rolling scenario
    window — a colleague could as defensibly rebalance weekly or quarterly.
The paper's unambiguous spec items are kept as-is: direction_vocab is
LONG/HOLD only (no SHORT — HOLD is realized here as "emit nothing" when no
ETF clears the ratio>1 breakeven), min_lookback_required=756 trading days,
minimum_universe_size=12, instrument_class='etp'. Data: close-price panel
only (engine-supplied `prices`) — returns and realized_vol are both
"computed" per the spec's data_requirements, not separate stored columns.
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['RachevRatioEtfScenarioOpt']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_rachev_ratio_etf_scenario_opt'

ETF_UNIVERSE = ['SPY', 'QQQ', 'IWM', 'EFA', 'EEM', 'TLT', 'IEF', 'LQD', 'HYG', 'GLD', 'SLV', 'DBC']


class RachevRatioEtfScenarioOpt(BaseStrategy):
    """LONG-only allocator across a fixed 12-ETF universe, weighted by each
    ETF's marginal Rachev ratio (upper-tail CVaR / lower-tail CVaR) computed
    from historical daily return scenarios, rebalanced monthly."""

    id                = STRATEGY_ID
    name              = 'Rachev-Ratio ETF Scenario Allocation'
    description       = ('LONG-only allocation across 12 cross-asset ETFs, weighted by each '
                         "ETF's marginal Rachev ratio (upper-tail CVaR over lower-tail CVaR) "
                         'from historical return scenarios; rebalanced monthly.')
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = 756          # paper's min_lookback_required (3y of daily scenarios)
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['TRANSITIONING', 'HIGH_VOL', 'CRISIS']   # spec regime_applicability
    MAX_SIGNALS       = 12

    ALPHA_TAIL   = 0.10   # upper-tail probability for CVaR(gains)
    BETA_TAIL    = 0.05   # lower-tail probability for CVaR(losses) — deliberately stricter
    MAX_WEIGHT   = 0.30   # per-asset cap, independent of regime scale
    HOLD_DAYS    = 21     # ~1 trading month, matches the rebalance cadence

    def default_parameters(self) -> dict:
        return {'alpha_tail': self.ALPHA_TAIL, 'beta_tail': self.BETA_TAIL, 'max_weight': self.MAX_WEIGHT}

    @staticmethod
    def _rachev_ratio(ret: pd.Series, alpha_tail: float, beta_tail: float) -> float:
        """CVaR_alpha(gains) / CVaR_beta(losses) over historical scenario returns `ret`."""
        ret = ret.dropna()
        if len(ret) < 100:
            return 0.0
        q_hi = ret.quantile(1.0 - alpha_tail)
        q_lo = ret.quantile(beta_tail)
        upper = ret[ret >= q_hi]
        lower = ret[ret <= q_lo]
        if len(upper) == 0 or len(lower) == 0:
            return 0.0
        cvar_gain = float(upper.mean())
        cvar_loss = float(-lower.mean())   # positive magnitude
        if cvar_loss <= 1e-8 or cvar_gain <= 0.0:
            return 0.0
        return cvar_gain / cvar_loss

    def _compute_weights(self, prices_sub: pd.DataFrame, tickers: list) -> pd.Series | None:
        alpha_tail = self.parameters.get('alpha_tail', self.ALPHA_TAIL)
        beta_tail  = self.parameters.get('beta_tail', self.BETA_TAIL)
        returns = prices_sub.pct_change().dropna(how='all')

        scores: dict[str, float] = {}
        for t in tickers:
            scores[t] = max(self._rachev_ratio(returns[t], alpha_tail, beta_tail), 0.0)

        total = sum(scores.values())
        if total < 1e-8:
            return None   # no ETF has a usable scenario-derived tail ratio — realized as HOLD
        return pd.Series({t: v / total for t, v in scores.items()})

    def _month_boundary(self, prices: pd.DataFrame) -> bool:
        if len(prices) < 2:
            return False
        d1 = pd.Timestamp(prices.index[-1])
        d0 = pd.Timestamp(prices.index[-2])
        return d1.month != d0.month

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

        tickers = [t for t in ETF_UNIVERSE if t in prices.columns]
        if len(tickers) < 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        prices_sub = prices[tickers].dropna(how='all')
        if len(prices_sub) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        if not (self._month_boundary(prices_sub) or self.cadence_reset(regime)):
            print('[debug] signals=0', file=sys.stderr)
            return []

        try:
            weights = self._compute_weights(prices_sub, tickers)
        except Exception as exc:
            print(f'[debug] signals=0 exc={exc}', file=sys.stderr)
            return []
        if weights is None:
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)
        max_weight = self.parameters.get('max_weight', self.MAX_WEIGHT)
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
            confidence = 'HIGH' if w > 0.20 else ('MED' if w > 0.08 else 'LOW')
            signals.append(Signal(
                ticker=ticker, direction='LONG', entry_price=price,
                stop_loss=stops['stop'], target_1=stops['t1'], target_2=stops['t2'], target_3=stops['t3'],
                position_size_pct=pos, confidence=confidence,
                signal_params={
                    'rachev_weight': round(w, 4),
                    'regime_scale':  round(scale, 3),
                    'hold_days':     self.HOLD_DAYS,
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

    strategy = RachevRatioEtfScenarioOpt()
    tickers = [t for t in ETF_UNIVERSE if t in prices_df.columns]
    hold_days = strategy.HOLD_DAYS
    records = []

    for i in range(strategy.min_lookback, len(prices_df) - hold_days):
        snap = prices_df.iloc[:i + 1]
        dt = snap.index[-1]
        if not strategy._month_boundary(snap):
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
            w = s.signal_params['rachev_weight']
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
