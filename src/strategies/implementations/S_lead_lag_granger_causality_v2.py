# Source: https://quantjourney.substack.com/p/moving-beyond-correlation-hunting
# Jakub 2026 — "Moving Beyond Correlation: Hunting Alpha with Lead-Lag & Granger
# Causality". Thesis: contemporaneous correlation only reveals hedging
# relationships; some pairs exhibit a statistically significant lead-lag
# (Granger-causal) return relationship, and trading the LAGGING asset in the
# direction of the LEADING asset's prior move captures directional alpha the
# correlation-only view misses.
#
# INTERPRETATION CHOICES (spec left these open — variant 2 of 2, deliberately
# different from the sibling S_lead_lag_granger_causality module):
#   - Candidate universe size/selection: rather than favoring the highest
#     trailing-volatility names (a bet that "more variance = more room for a
#     lag relationship to show up"), this variant selects the LOWEST trailing
#     realized-volatility names as candidates. Rationale: the lead-lag
#     literature (Lo & MacKinlay 1990; Chordia & Swaminathan 2000) attributes
#     the effect to information-diffusion speed — large, mature, liquid names
#     digest news fastest and lead noisier, less-efficiently-priced names.
#     Realized volatility is a price-only proxy for that maturity/liquidity
#     axis: calmer names look more like index leaders, and a low-vol prefilter
#     avoids chasing noisy, possibly-spurious high-vol co-movements.
#   - Refresh cadence: WEEKLY (first trading day of each ISO week) instead of
#     monthly. The pseudocode's own lag window (k=1..5 trading days) implies
#     the causal relationship operates on a roughly weekly horizon; refreshing
#     only once a month risks trading a pair discovery for weeks after the
#     underlying relationship has already decayed. Weekly refresh keeps the
#     candidate set closer to the horizon the effect is hypothesized to live
#     on, at a still-tractable ~52 refreshes/year.
#   - Analysis window: the FULL min_lookback_required=504 (two trailing
#     years) is used as the estimation window for every refresh, rather than
#     a shorter rolling 252-bar sub-window. The spec's own overfitting_flags
#     cite "short_backtest" as a named risk; maximizing the sample fed into
#     the VAR/F-test on every re-estimation trades adaptiveness for
#     statistical power and fewer spuriously significant p-values.
#   - Direction: LONG the follower if the leader's return at the causal lag
#     was positive, SHORT if negative — read directly off the pseudocode
#     (unambiguous, unchanged from the sibling variant).
from __future__ import annotations

import sys
from itertools import permutations
from typing import List

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import grangercausalitytests

from strategies.base import BaseStrategy, Signal

__all__ = ['LeadLagGrangerCausalityV2']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_lead_lag_granger_causality_v2'


class LeadLagGrangerCausalityV2(BaseStrategy):
    """Granger-causal lead-lag pairs (variant 2): LONG/SHORT the lagging
    asset in the direction of the leading asset's prior move, with a
    low-volatility candidate prefilter, weekly refresh, and a full
    two-year estimation window. See module header for the interpretation
    choices made where the source paper's spec was ambiguous.
    """

    id                = STRATEGY_ID
    name              = 'LeadLagGrangerCausalityV2'
    description       = ('Directional alpha from statistically significant Granger-causal '
                          'lead-lag return relationships among low-vol (index-leader-proxy) names.')
    tier              = 2
    min_lookback      = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    ANALYSIS_WINDOW = 504   # full min_lookback used as the estimation window
    TOP_N           = 20    # candidate universe cap (TOP_N*(TOP_N-1) directed Granger tests)
    MAX_LAG         = 5     # k = 1..5 trading days, per spec pseudocode
    ALPHA           = 0.05  # significance threshold
    BASE_SIZE       = 0.03

    def default_parameters(self) -> dict:
        return {'top_n': self.TOP_N, 'alpha': self.ALPHA, 'base_size': self.BASE_SIZE}

    @staticmethod
    def _is_week_start(prices: pd.DataFrame, as_of: pd.Timestamp) -> bool:
        """True on the first trading bar of a new ISO week, using the prices
        panel's own index (no external trading calendar dependency)."""
        if len(prices) < 2:
            return True
        prev = pd.Timestamp(prices.index[-2])
        return prev.isocalendar()[:2] != as_of.isocalendar()[:2]

    def generate_signals(
        self, prices: pd.DataFrame, regime: dict, universe: List[str], aux_data: dict = None
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []
        if len(prices) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        as_of = pd.Timestamp(prices.index[-1])
        if not self._is_week_start(prices, as_of):
            print('[debug] signals=0 (off-cadence, next refresh is week-start)', file=sys.stderr)
            return []

        cols = [t for t in universe if t in prices.columns]
        if len(cols) < 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # The master prices panel is a union of every source's calendar, so an
        # individual equity legitimately has NaN gaps on dates other markets
        # traded even with a full listing history. Require >=60% raw coverage
        # over the estimation window, then ffill/bfill residual gaps.
        window_raw = prices[cols].iloc[-(self.ANALYSIS_WINDOW + 1):]
        coverage = window_raw.notna().mean()
        valid = coverage[coverage >= 0.6].index
        window = window_raw[valid].ffill().bfill()
        valid2 = window.columns[(window > 0).all() & window.notna().all()]
        window = window[valid2]
        if window.shape[1] < 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        log_ret = np.log(window).diff().dropna()
        if len(log_ret) < self.ANALYSIS_WINDOW // 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        try:
            top_n = int(self.parameters.get('top_n', self.TOP_N))
        except (TypeError, ValueError):
            top_n = self.TOP_N
        try:
            alpha = float(self.parameters.get('alpha', self.ALPHA))
        except (TypeError, ValueError):
            alpha = self.ALPHA
        try:
            base_size = float(self.parameters.get('base_size', self.BASE_SIZE))
        except (TypeError, ValueError):
            base_size = self.BASE_SIZE

        # Low-vol prefilter (index-leader proxy) — the opposite tail from the
        # sibling variant's high-vol prefilter.
        vol = log_ret.std().reset_index()
        vol.columns = ['ticker', 'std']
        vol = vol.sort_values(['std', 'ticker'], ascending=[True, True])  # deterministic tie-break
        candidates = list(vol['ticker'].iloc[:top_n])
        if len(candidates) < 2:
            print('[debug] signals=0', file=sys.stderr)
            return []
        cand_ret = log_ret[candidates]
        scale = self.position_scale(regime_state)

        results = []
        for leader, follower in permutations(candidates, 2):
            data = cand_ret[[follower, leader]].to_numpy(dtype=float)
            try:
                gc = grangercausalitytests(data, maxlag=self.MAX_LAG, verbose=False)
            except Exception:
                continue
            pvals = {}
            for lag, res in gc.items():
                try:
                    pvals[lag] = float(res[0]['ssr_ftest'][1])
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
            if not pvals:
                continue
            best_lag = min(pvals, key=pvals.get)
            p = pvals[best_lag]
            if not (np.isfinite(p) and p < alpha):
                continue
            # Leader's return best_lag bars prior — the analog of x2(t-k) used
            # to forecast x1(t) in the fitted test, shifted one step forward
            # to forecast the FOLLOWER's next (not-yet-realized) move.
            leader_move = float(cand_ret[leader].iloc[-best_lag])
            if leader_move == 0.0 or not np.isfinite(leader_move):
                continue
            stability = sum(1 for v in pvals.values() if v < alpha) / len(pvals)
            results.append({'leader': leader, 'follower': follower, 'lag': int(best_lag),
                             'p': p, 'stability': stability, 'leader_move': leader_move})

        results.sort(key=lambda r: r['p'])
        used_followers = set()
        signals: List[Signal] = []
        for r in results:
            follower = r['follower']
            if follower in used_followers:
                continue
            price_series = prices[follower].dropna()
            if price_series.empty:
                continue
            entry_price = float(price_series.iloc[-1])
            if not (np.isfinite(entry_price) and entry_price > 0.0):
                continue
            direction = 'LONG' if r['leader_move'] > 0.0 else 'SHORT'
            conf = 'HIGH' if (r['p'] < 0.01 and r['stability'] >= 0.6) else 'MED'
            size = round(base_size * scale * (0.5 + 0.5 * r['stability']), 4)
            levels = self.compute_stops_and_targets(price_series, direction, entry_price, regime_state=regime_state)
            signals.append(Signal(
                ticker=follower, direction=direction, entry_price=entry_price,
                stop_loss=levels['stop'], target_1=levels['t1'],
                target_2=levels['t2'], target_3=levels['t3'],
                position_size_pct=size, confidence=conf,
                signal_params={
                    'leader': r['leader'], 'lag_days': r['lag'],
                    'p_value': round(r['p'], 6), 'lag_stability': round(r['stability'], 3),
                    'leader_move': round(r['leader_move'], 6),
                },
            ))
            used_followers.add(follower)
            if len(signals) >= self.MAX_SIGNALS:
                break

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


if __name__ == '__main__':
    import os
    from backtest.quick_backtest import run_backtest_with_regime_partition

    ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    HOLD_EVAL = 10  # bars simulated forward to score a trade in this sanity backtest only

    long_df = pd.read_parquet(f'{ROOT}/prices.parquet', columns=['ticker', 'date', 'close'])
    wide = long_df.pivot_table(index='date', columns='ticker', values='close')
    wide.index = pd.to_datetime(wide.index)
    wide = wide.sort_index().loc['2017-01-01':'2025-12-31']

    reg_df = pd.read_parquet(f'{ROOT}/historical_regimes.parquet')
    reg_df['date'] = pd.to_datetime(reg_df['date'])
    reg_map = reg_df.set_index('date')['regime'].to_dict()

    # Standalone-script universe proxy: exclude non-equity symbols and require
    # near-complete coverage over the run — production passes a curated
    # universe (default sp500 predicate) instead of this heuristic.
    junk = lambda c: c.startswith('^') or '-USD' in c or '=F' in c
    keep_cols = [c for c in wide.columns if not junk(c) and wide[c].notna().sum() >= 2000]
    universe = keep_cols

    strategy = LeadLagGrangerCausalityV2()
    rows = []
    for i in range(strategy.min_lookback, len(wide)):
        bar_date = wide.index[i]
        r_state = reg_map.get(pd.Timestamp(bar_date), 'LOW_VOL')
        sigs = strategy.generate_signals(wide.iloc[:i + 1], {'state': r_state}, universe)
        for sig in sigs:
            fut = wide[sig.ticker].dropna()
            fut = fut[fut.index >= bar_date]
            if len(fut) < 2:
                continue
            window_fut = fut.iloc[1:HOLD_EVAL + 1]
            exit_price = None
            for p in window_fut:
                p = float(p)
                if sig.direction == 'LONG' and (p <= sig.stop_loss or p >= sig.target_1):
                    exit_price = sig.stop_loss if p <= sig.stop_loss else sig.target_1
                    break
                if sig.direction == 'SHORT' and (p >= sig.stop_loss or p <= sig.target_1):
                    exit_price = sig.stop_loss if p >= sig.stop_loss else sig.target_1
                    break
            if exit_price is None:
                exit_price = float(window_fut.iloc[-1]) if len(window_fut) else sig.entry_price
            pnl = (exit_price - sig.entry_price) if sig.direction == 'LONG' else (sig.entry_price - exit_price)
            denom = max(abs(sig.entry_price - sig.stop_loss), 1e-6)
            rows.append({'strategy_id': STRATEGY_ID, 'signal_date': str(bar_date)[:10],
                         'regime_state': r_state, 'pnl': float(pnl), 'r_multiple': float(pnl / denom)})

    trades_df = pd.DataFrame(rows)
    print(f'Backtest produced {len(trades_df)} trades', file=sys.stderr)
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print('regime_partition:', result['regime_partition'])
    print('eligible_regimes_proposed:', result['eligible_regimes_proposed'])
