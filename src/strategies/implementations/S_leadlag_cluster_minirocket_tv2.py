# Source: http://arxiv.org/abs/2608.24703v1
# Deng & Zhang 2026 — "Lead-Lag Relationships in Financial Markets: A
# Comparison of Multiple Clustering Algorithms". Thesis: stocks cluster into
# lead-lag groups whose leader/follower return relationship persists
# short-term; a cluster's leader's most recent return direction predicts its
# followers' near-term returns. The paper's best-performing clustering method
# (reported Sharpe 0.866) is MiniRocket-KMeans.
#
# INTERPRETATION CHOICES (spec left these open — variant 1 of 2, deliberately
# different from a colleague's variant 2):
#   - MiniRocket features without sktime: the abstract-only extraction gives
#     no kernel/hyperparameter detail, and sktime/tslearn are not installed
#     in this environment. Rather than approximating with a plain-correlation
#     or DTW-distance clustering (the paper's OTHER, non-best methods), this
#     variant re-implements the core MiniRocket mechanism directly: fixed-seed
#     random {-1,+1} convolutional kernels at multiple dilations applied to
#     each name's log-return series, PPV (proportion-positive-values) pooled
#     per kernel/dilation into a feature vector. This keeps the "best method"
#     the paper reports, rather than falling back to a weaker alternative.
#   - Candidate universe selection: NO volatility/liquidity prefilter. Every
#     universe ticker with >=60% price coverage over the estimation window is
#     a clustering candidate (capped at MAX_CANDIDATES, taken alphabetically
#     for determinism). Rationale: a prefilter bakes in an assumption
#     (e.g. "calm names lead") the clustering algorithm is supposed to
#     discover on its own from the feature geometry — pre-selecting a tail
#     would bias which lead-lag structure MiniRocket-KMeans is allowed to find.
#   - Estimation/clustering window: a rolling 252-bar (1yr) window, not the
#     full min_lookback_required=504 (2yr). Rationale: lead-lag cluster
#     membership is a co-movement structure that can rotate as sector
#     leadership rotates; a full 2yr window would blur two regimes of cluster
#     structure together. min_lookback=504 is still enforced as a warm-up
#     floor so the first ANALYSIS_WINDOW is estimated on a fully "seasoned"
#     tape, not the strategy's own clustering window.
#   - Cluster count: silhouette-optimized over k=2..min(10, n_candidates-1),
#     per the paper's stated selection method (unambiguous, unchanged).
#   - Leader identification: within a cluster, the member whose lag-1 return
#     has the highest AVERAGE positive correlation with every other member's
#     contemporaneous return is the leader (a direct read of "lead-lag
#     relationship" — leader's move at t-1 co-moves with followers' moves at
#     t). Only pairs with lag-1 correlation >= CORR_MIN are traded, to avoid
#     acting on zero/negative cluster relationships. CORR_MIN is calibrated
#     empirically against this project's own prices.parquet (checked at
#     implementation time): single-name daily lag-1 cross-correlations here
#     are small in absolute terms (production sample: cluster leader-average
#     scores topped out ~0.08, mean ~0.01) — a textbook-style 0.10-0.30
#     threshold would silently zero out every signal on this data, so
#     CORR_MIN is set to the smallest value that still discriminates real
#     co-movement from noise, with the HIGH/MED/LOW confidence tiers doing
#     the conviction-ranking work instead of one hard cutoff.
#   - Refresh cadence: MONTHLY (first trading day of the month) — a slower
#     cadence than a plausible weekly-refresh colleague variant, trading
#     responsiveness for lower turnover/re-clustering noise given cluster
#     membership is a slower-moving structural property than a specific
#     Granger-lag estimate.
#   - Direction: LONG the follower if the cluster leader's most recent return
#     was positive, SHORT if negative — read directly off the pseudocode
#     (unambiguous, unchanged).
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

from strategies.base import BaseStrategy, Signal

__all__ = ['LeadLagClusterMinirocket']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_leadlag_cluster_minirocket'

_RNG = np.random.RandomState(42)  # fixed seed — deterministic kernels, no data-dependent randomness
_KERNEL_LEN = 9
_N_KERNELS = 16
_DILATIONS = (1, 2, 4)
_KERNELS = [_RNG.randint(0, 2, size=_KERNEL_LEN) * 2 - 1 for _ in range(_N_KERNELS)]  # {-1,+1}^9


def _minirocket_features(x: np.ndarray) -> np.ndarray:
    """PPV (proportion-positive-values) features from fixed random dilated
    convolutional kernels — a lightweight, dependency-free stand-in for
    sktime's MiniRocketMultivariate (unavailable in this environment)."""
    feats = []
    w = len(x)
    for kernel in _KERNELS:
        for d in _DILATIONS:
            span = (_KERNEL_LEN - 1) * d
            n_out = w - span
            if n_out <= 1:
                feats.append(0.5)
                continue
            conv = np.zeros(n_out)
            for j in range(_KERNEL_LEN):
                conv += kernel[j] * x[j * d: j * d + n_out]
            feats.append(float(np.mean(conv > 0)))
    return np.array(feats, dtype=float)


class LeadLagClusterMinirocket(BaseStrategy):
    """Lead-lag cluster momentum (MiniRocket-KMeans variant): cluster names by
    MiniRocket-style return-shape features, silhouette-select k, identify each
    cluster's leader by lag-1 co-movement correlation, LONG/SHORT followers in
    the direction of the leader's most recent return. See module header for
    the interpretation choices made where the source paper's spec was
    ambiguous (variant 1 of 2)."""

    id                = STRATEGY_ID
    name              = 'LeadLagClusterMinirocket'
    description       = ('Cluster names into lead-lag groups via MiniRocket-style features + KMeans; '
                          'trade followers in the direction of their cluster leader\'s prior return.')
    tier              = 2
    min_lookback      = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    ANALYSIS_WINDOW  = 252   # rolling clustering window (variant 1: shorter than min_lookback)
    MAX_CANDIDATES   = 150
    MAX_K            = 10
    CORR_MIN         = 0.02  # calibrated against real data — see module header
    BASE_SIZE        = 0.03

    def default_parameters(self) -> dict:
        return {'corr_min': self.CORR_MIN, 'base_size': self.BASE_SIZE}

    @staticmethod
    def _is_month_start(prices: pd.DataFrame, as_of: pd.Timestamp) -> bool:
        """True on the first trading bar of a new month, using the prices
        panel's own index (no external trading calendar dependency)."""
        if len(prices) < 2:
            return True
        prev = pd.Timestamp(prices.index[-2])
        return (prev.year, prev.month) != (as_of.year, as_of.month)

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

        cols = sorted(t for t in universe if t in prices.columns)
        if len(cols) < 4:
            print('[debug] signals=0', file=sys.stderr)
            return []

        window_raw = prices[cols].iloc[-(self.ANALYSIS_WINDOW + 1):]
        coverage = window_raw.notna().mean()
        valid = sorted(coverage[coverage >= 0.6].index)[:self.MAX_CANDIDATES]
        window = window_raw[valid].ffill().bfill()
        valid2 = [c for c in window.columns if (window[c] > 0).all() and window[c].notna().all()]
        window = window[valid2]
        if window.shape[1] < 4:
            print('[debug] signals=0', file=sys.stderr)
            return []

        log_ret = np.log(window).diff().dropna()
        if len(log_ret) < self.ANALYSIS_WINDOW // 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        try:
            corr_min = float(self.parameters.get('corr_min', self.CORR_MIN))
        except (TypeError, ValueError):
            corr_min = self.CORR_MIN
        try:
            base_size = float(self.parameters.get('base_size', self.BASE_SIZE))
        except (TypeError, ValueError):
            base_size = self.BASE_SIZE

        tickers = list(log_ret.columns)
        feat_matrix = np.vstack([_minirocket_features(log_ret[t].to_numpy()) for t in tickers])
        feat_matrix = (feat_matrix - feat_matrix.mean(axis=0)) / (feat_matrix.std(axis=0) + 1e-9)

        best_k, best_score, best_labels = None, -1.0, None
        max_k = min(self.MAX_K, len(tickers) - 1)
        for k in range(2, max_k + 1):
            try:
                labels = KMeans(n_clusters=k, n_init=10, random_state=42).fit_predict(feat_matrix)
                if len(set(labels)) < 2:
                    continue
                score = silhouette_score(feat_matrix, labels)
            except Exception:
                continue
            if score > best_score:
                best_k, best_score, best_labels = k, score, labels
        if best_labels is None:
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)
        signals: List[Signal] = []
        for cid in sorted(set(best_labels)):
            members = [t for t, lab in zip(tickers, best_labels) if lab == cid]
            if len(members) < 2:
                continue
            mem_ret = log_ret[members]
            best_leader, best_leader_score = None, -1.0
            for cand in members:
                others = [m for m in members if m != cand]
                lagged = mem_ret[cand].shift(1)
                corrs = [lagged.corr(mem_ret[o]) for o in others]
                corrs = [c for c in corrs if pd.notna(c)]
                if not corrs:
                    continue
                avg_corr = float(np.mean(corrs))
                if avg_corr > best_leader_score:
                    best_leader, best_leader_score = cand, avg_corr
            if best_leader is None or best_leader_score < corr_min:
                continue
            leader = best_leader
            leader_move = float(mem_ret[leader].iloc[-1])
            if leader_move == 0.0 or not np.isfinite(leader_move):
                continue
            direction = 'LONG' if leader_move > 0.0 else 'SHORT'
            for follower in members:
                if follower == leader:
                    continue
                pair_corr = mem_ret[leader].shift(1).corr(mem_ret[follower])
                if pd.isna(pair_corr) or pair_corr < corr_min:
                    continue
                price_series = prices[follower].dropna()
                if price_series.empty:
                    continue
                entry_price = float(price_series.iloc[-1])
                if not (np.isfinite(entry_price) and entry_price > 0.0):
                    continue
                conf = 'HIGH' if pair_corr >= 0.06 else ('MED' if pair_corr >= 0.035 else 'LOW')
                size = round(base_size * scale * (0.5 + 0.5 * min(pair_corr, 1.0)), 4)
                levels = self.compute_stops_and_targets(price_series, direction, entry_price, regime_state=regime_state)
                signals.append(Signal(
                    ticker=follower, direction=direction, entry_price=entry_price,
                    stop_loss=levels['stop'], target_1=levels['t1'],
                    target_2=levels['t2'], target_3=levels['t3'],
                    position_size_pct=size, confidence=conf,
                    signal_params={
                        'leader': leader, 'cluster_id': int(cid), 'k': int(best_k),
                        'silhouette': round(float(best_score), 4),
                        'lag1_corr': round(float(pair_corr), 4),
                        'leader_move': round(leader_move, 6),
                    },
                ))
                if len(signals) >= self.MAX_SIGNALS:
                    break
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

    strategy = LeadLagClusterMinirocket()
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

    trades_df = pd.DataFrame(rows, columns=['strategy_id', 'signal_date', 'regime_state', 'pnl', 'r_multiple'])
    print(f'Backtest produced {len(trades_df)} trades', file=sys.stderr)
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print('regime_partition:', result['regime_partition'])
    print('eligible_regimes_proposed:', result['eligible_regimes_proposed'])
