# Source: https://quantjourney.substack.com/p/moving-beyond-correlation-hunting
# Jakub 2026 — "Moving Beyond Correlation: Hunting Alpha with Lead-Lag & Granger
# Causality". Thesis: contemporaneous correlation only reveals hedging
# relationships; some pairs exhibit a statistically significant lead-lag
# (Granger-causal) return relationship, and trading the LAGGING asset in the
# direction of the LEADING asset's prior move captures directional alpha the
# correlation-only view misses.
#
# INTERPRETATION CHOICES (spec left these open — variant 1 of 2):
#   - Candidate universe size: the paper's pseudocode tests "each candidate
#     pair (A,B) in universe" with no bound (spec minimum_universe_size=100
#     -> up to ~4,950 unordered pairs). A full O(n^2) VAR-based Granger test
#     on every pair, every trading day, is not compute-tractable. We narrow
#     the candidate set each refresh to the TOP_N tickers by trailing
#     realized volatility — higher-vol names carry more return variance for a
#     lag relationship to show up in, and volatility ranking is orthogonal to
#     the paper's correlation-vs-causality distinction (unlike, say, a
#     contemporaneous-correlation prefilter, which would reintroduce the
#     exact confound the paper argues against).
#   - Refresh cadence: re-running ~TOP_N^2 Granger tests every bar is
#     unnecessary (lead-lag structure is a slower-moving property than daily
#     noise) and expensive across a multi-year backtest. We re-scan the full
#     candidate universe once per calendar month (first trading day) and are
#     silent (no new entries) the rest of the month — open positions still
#     ride their normal stop/target machinery.
#   - Analysis window: a rolling ANALYSIS_WINDOW=252 (one trailing year),
#     re-estimated every refresh, rather than one static fit over the full
#     min_lookback_required(504) history — lead-lag relationships can drift
#     (M&A, index changes, sector realignment) so a rolling year is favored
#     over a single long static window. min_lookback stays 504 so a strategy
#     instance needs two full years of history before it will ever fire, per
#     the spec's explicit min_lookback_required.
#   - Direction: LONG the follower if the leader's return at the causal lag
#     was positive, SHORT if negative — read directly off the pseudocode.
from __future__ import annotations

import sys
from itertools import permutations
from typing import List

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import grangercausalitytests

from strategies.base import BaseStrategy, Signal

__all__ = ['LeadLagGrangerCausality']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_lead_lag_granger_causality'


class LeadLagGrangerCausality(BaseStrategy):
    """Granger-causal lead-lag pairs: LONG/SHORT the lagging asset in the
    direction of the leading asset's prior move. See module header for the
    interpretation choices made where the source paper's spec was ambiguous.
    """

    id                = STRATEGY_ID
    name              = 'LeadLagGrangerCausality'
    description       = ('Directional alpha from statistically significant Granger-causal '
                          'lead-lag return relationships, distinct from contemporaneous correlation.')
    tier              = 2
    min_lookback      = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    ANALYSIS_WINDOW = 252   # trailing bars re-estimated every refresh
    TOP_N           = 15    # candidate universe cap (TOP_N*(TOP_N-1) directed Granger tests)
    MAX_LAG         = 5     # k = 1..5 trading days, per spec pseudocode
    ALPHA           = 0.05  # significance threshold
    BASE_SIZE       = 0.03

    def default_parameters(self) -> dict:
        return {'top_n': self.TOP_N, 'alpha': self.ALPHA, 'base_size': self.BASE_SIZE}

    @staticmethod
    def _is_month_start(prices: pd.DataFrame, as_of: pd.Timestamp) -> bool:
        """True on the first trading bar of a new calendar month, using the
        prices panel's own index (no external trading calendar dependency)."""
        if len(prices) < 2:
            return True
        prev = pd.Timestamp(prices.index[-2])
        return prev.year != as_of.year or prev.month != as_of.month

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
        if not self._is_month_start(prices, as_of):
            print('[debug] signals=0 (off-cadence, next refresh is month-start)', file=sys.stderr)
            return []

        cols = [t for t in universe if t in prices.columns]
        if len(cols) < 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # The master prices panel is a union of every source's calendar, so an
        # individual equity legitimately has NaN gaps on dates other markets
        # traded (crypto/futures rows, etc) even with a full listing history.
        # Requiring 100% density here would (and in testing did) empty the
        # candidate pool; instead require >=60% raw coverage, then ffill/bfill
        # the residual gaps before computing returns.
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

        vol = log_ret.std().reset_index()
        vol.columns = ['ticker', 'std']
        vol = vol.sort_values(['std', 'ticker'], ascending=[False, True])  # deterministic tie-break
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

    strategy = LeadLagGrangerCausality()
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
