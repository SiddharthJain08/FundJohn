"""
S_rachev_ratio_etf_scenario_opt_tv2 — Rachev-Ratio ETF Scenario Allocation
(variant 2 of 2)

Source: He, Yifan (2026), "Rachev-Ratio Portfolio Optimization with TimeGAN
Scenarios: Replacement versus Supplementation" (arXiv:2610.24544v1).

Hypothesis: allocating across a fixed 12-ETF universe by maximizing the
Rachev ratio (upper-tail reward over lower-tail risk, from historical return
scenarios) targets a tail-asymmetric risk preference that mean-variance /
Sharpe optimization ignores. The paper's own ablation finds no statistically
significant edge from TimeGAN-synthetic scenarios over historical ones, so —
same as variant 1 — this implementation uses ONLY historical scenarios.

Variant 2/2 — abstract-only extraction, so every numeric/structural choice
below is deliberately a DIFFERENT defensible reading than variant 1's:
  - 12-ETF universe: 11 Select Sector SPDRs (XLC, XLY, XLP, XLE, XLF, XLV,
    XLI, XLB, XLRE, XLK, XLU) + SPY as the broad-market anchor — a
    within-equity sector-rotation universe, rather than variant 1's
    cross-asset-class spread (rates/credit/commodities). Sector dispersion
    is where a tail-asymmetric allocator should differentiate itself from
    a cap-weighted index, since sector CVaR profiles diverge sharply in
    drawdowns even though all 12 share the same equity beta.
  - tail probabilities: SYMMETRIC alpha=0.05 (upper) / beta=0.05 (lower) —
    variant 1 deliberately split the quantiles (0.10/0.05); this variant
    instead holds both tails to the same strict quantile, so the Rachev
    ratio here measures pure tail-shape asymmetry (skew) rather than
    conflating it with a quantile-width choice.
  - allocation rule: an ABSOLUTE breakeven gate (ratio > 1.0, i.e. the
    worst-5% average loss is smaller in magnitude than the best-5% average
    gain) selects a TOP_K equal-weight sleeve, rather than variant 1's
    continuous proportional-to-score weighting across the full universe.
    This is the literal "ratio > 1" reading of the paper's optimization
    criterion as a hard constraint rather than a soft objective, and
    produces a concentrated, HOLD-capable book (if fewer than K ETFs clear
    1.0, the sleeve shrinks — including to fully flat) instead of always
    being invested in whichever 12 names happen to score best.
  - rebalance cadence: QUARTERLY (first trading day of each calendar
    quarter), not monthly — sector tail-asymmetry is a slower-moving
    property than variant 1's monthly cadence assumes, and quarterly
    turnover better matches the 756-day (3y) scenario window's effective
    update frequency.
  - eligible regimes: LOW_VOL, TRANSITIONING, HIGH_VOL (excludes CRISIS) —
    variant 1 included CRISIS; this variant's thesis is that sector tail
    dispersion collapses in a CRISIS regime (correlations → 1, "everything
    sells off together"), so the sleeve should stand down rather than keep
    rotating sectors once the regime is already in tail-event territory.
The paper's unambiguous spec items are kept as-is: direction_vocab is
LONG/HOLD only (no SHORT — HOLD is realized here as "emit nothing" when
fewer than one ETF clears the ratio>1 gate), min_lookback_required=756
trading days, minimum_universe_size=12, instrument_class='etp'. Data:
close-price panel only (engine-supplied `prices`) — returns and
realized_vol are both "computed" per the spec, not separate stored columns.
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['RachevRatioEtfScenarioOptTv2']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_rachev_ratio_etf_scenario_opt_tv2'

ETF_UNIVERSE = ['XLC', 'XLY', 'XLP', 'XLE', 'XLF', 'XLV', 'XLI', 'XLB', 'XLRE', 'XLK', 'XLU', 'SPY']


class RachevRatioEtfScenarioOptTv2(BaseStrategy):
    """LONG-only sector-rotation sleeve: equal-weights the top-K sector
    SPDRs (+ SPY) whose marginal Rachev ratio (symmetric 5%/5% upper-CVaR
    over lower-CVaR, from historical daily scenarios) clears an absolute
    breakeven gate of 1.0; rebalanced quarterly."""

    id                = STRATEGY_ID
    name              = 'Rachev-Ratio ETF Scenario Allocation (Tv2)'
    description       = ('LONG-only top-K equal-weight sleeve across 11 sector SPDRs + SPY, gated '
                         'on each ETF clearing an absolute Rachev-ratio (symmetric 5%/5% tail CVaR) '
                         'breakeven of 1.0 from historical return scenarios; rebalanced quarterly.')
    tier              = 2
    signal_frequency  = 'quarterly'
    min_lookback      = 756          # paper's min_lookback_required (3y of daily scenarios)
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 12

    ALPHA_TAIL   = 0.05   # upper-tail probability for CVaR(gains) — symmetric with beta
    BETA_TAIL    = 0.05   # lower-tail probability for CVaR(losses)
    BREAKEVEN    = 1.0    # absolute gate: ratio must exceed this to be eligible
    TOP_K        = 4      # equal-weight sleeve size among qualifiers
    HOLD_DAYS    = 63     # ~1 trading quarter, matches the rebalance cadence

    def default_parameters(self) -> dict:
        return {'alpha_tail': self.ALPHA_TAIL, 'beta_tail': self.BETA_TAIL,
                'breakeven': self.BREAKEVEN, 'top_k': self.TOP_K}

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

    def _select_sleeve(self, prices_sub: pd.DataFrame, tickers: list) -> list:
        """Returns the list of qualifying tickers (ratio > breakeven), capped at TOP_K,
        ranked by descending ratio. Empty list is a valid HOLD outcome."""
        alpha_tail = self.parameters.get('alpha_tail', self.ALPHA_TAIL)
        beta_tail  = self.parameters.get('beta_tail', self.BETA_TAIL)
        breakeven  = self.parameters.get('breakeven', self.BREAKEVEN)
        top_k      = int(self.parameters.get('top_k', self.TOP_K))
        returns = prices_sub.pct_change().dropna(how='all')

        scores: dict[str, float] = {}
        for t in tickers:
            scores[t] = self._rachev_ratio(returns[t], alpha_tail, beta_tail)

        qualifiers = [t for t, v in scores.items() if v > breakeven]
        qualifiers.sort(key=lambda t: scores[t], reverse=True)
        return qualifiers[:top_k]

    def _quarter_boundary(self, prices: pd.DataFrame) -> bool:
        if len(prices) < 2:
            return False
        d1 = pd.Timestamp(prices.index[-1])
        d0 = pd.Timestamp(prices.index[-2])
        q1 = (d1.month - 1) // 3
        q0 = (d0.month - 1) // 3
        return (d1.year, q1) != (d0.year, q0)

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

        if not (self._quarter_boundary(prices_sub) or self.cadence_reset(regime)):
            print('[debug] signals=0', file=sys.stderr)
            return []

        try:
            sleeve = self._select_sleeve(prices_sub, tickers)
        except Exception as exc:
            print(f'[debug] signals=0 exc={exc}', file=sys.stderr)
            return []
        if not sleeve:
            print('[debug] signals=0', file=sys.stderr)   # realized as HOLD
            return []

        scale = self.position_scale(regime_state)
        weight = round(1.0 / len(sleeve), 4)
        last_row = prices_sub.iloc[-1]

        signals: List[Signal] = []
        for ticker in sleeve:
            price = float(last_row[ticker])
            if not (price == price) or price <= 0:
                continue
            series = prices_sub[ticker].dropna()
            stops = self.compute_stops_and_targets(series, 'LONG', price, regime_state=regime_state)
            pos = round(weight * scale, 4)
            signals.append(Signal(
                ticker=ticker, direction='LONG', entry_price=price,
                stop_loss=stops['stop'], target_1=stops['t1'], target_2=stops['t2'], target_3=stops['t3'],
                position_size_pct=pos, confidence='HIGH' if len(sleeve) <= 2 else 'MED',
                signal_params={
                    'sleeve_size':  len(sleeve),
                    'equal_weight': weight,
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
    """Quarterly walk-forward over `ETF_UNIVERSE`, returning a trades_df for
    regime partitioning (equal-weighted realized return across the
    rebalanced sleeve, one row per rebalance date)."""
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

    strategy = RachevRatioEtfScenarioOptTv2()
    tickers = [t for t in ETF_UNIVERSE if t in prices_df.columns]
    hold_days = strategy.HOLD_DAYS
    records = []

    for i in range(strategy.min_lookback, len(prices_df) - hold_days):
        snap = prices_df.iloc[:i + 1]
        dt = snap.index[-1]
        if not strategy._quarter_boundary(snap):
            continue
        regime_state = regimes.get(str(dt.date()), 'LOW_VOL')
        sigs = strategy.generate_signals(snap, {'state': regime_state}, tickers)
        if not sigs:
            continue
        exit_dt = prices_df.index[i + hold_days]
        pnl_num, n = 0.0, 0
        for s in sigs:
            exit_px = prices_df[s.ticker].get(exit_dt)
            if exit_px is None or pd.isna(exit_px):
                continue
            ret = (float(exit_px) - s.entry_price) / s.entry_price
            pnl_num += ret
            n += 1
        if n == 0:
            continue
        avg_pnl = pnl_num / n
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
