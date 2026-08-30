# Source: http://arxiv.org/abs/2608.24703v1
# Deng & Zhang 2026 — "Lead-Lag Relationships in Financial Markets: A
# Comparison of Multiple Clustering Algorithms". Thesis: stocks cluster into
# lead-lag groups whose leader/follower return relationship persists
# short-term; a cluster's leader's most recent return direction predicts its
# followers' near-term returns. The paper's best-performing clustering method
# (reported Sharpe 0.866) is MiniRocket-KMeans.
#
# INTERPRETATION CHOICES (spec left these open — variant 2 of 2, deliberately
# different from the sibling S_leadlag_cluster_minirocket module):
#   - Candidate universe selection: sibling v1 used NO liquidity prefilter
#     (every >=60%-covered ticker, alphabetical cap). This variant instead
#     prefilters to the MAX_CANDIDATES names with the highest trailing
#     20-bar median dollar volume (close * volume, via the shared
#     `load_wide` panel loader). Rationale: information-diffusion leadership
#     is concentrated in liquid, heavily-traded names (Lo & MacKinlay 1990) —
#     thin names carry stale-price/non-trading noise that would corrupt a
#     shape-based clustering feature space rather than reveal genuine
#     lead-lag structure.
#   - Estimation/clustering window: the FULL min_lookback_required=504-bar
#     (2yr) window, not sibling v1's shorter rolling 252-bar window.
#     Rationale: if cluster membership is treated as a comparatively
#     slow-moving structural property (the opposite bet from v1), a longer
#     window gives more stable MiniRocket feature estimates and less
#     month-to-month whipsaw in which names cluster together.
#   - MiniRocket feature approximation: sktime/tslearn are unavailable in
#     this environment, so both variants hand-roll fixed random dilated
#     kernels. This variant pools EACH kernel/dilation into TWO statistics —
#     PPV (proportion-positive-values, as in v1) AND max-value — mirroring
#     the original ROCKET paper's finding that PPV and max capture
#     complementary shape information; it also uses a longer kernel
#     (length 7, vs v1's 9) and adds an 8-bar dilation (v1 max dilation is 4)
#     to reach further back for slower-decaying co-movement.
#   - Cluster count: silhouette-optimized over k=2..min(10, n_candidates-1),
#     per the paper's stated selection method (unambiguous, unchanged from v1).
#   - Leader identification: v1 scores each member by its OWN lag-1-vs-others
#     average correlation. This variant instead builds a directed weighted
#     graph over cluster members: edge(i -> j) = the MAX cross-correlation
#     of i's return lagged 1-3 bars against j's contemporaneous return (a
#     multi-lag search rather than a fixed lag-1 look, since the paper's
#     "lead-lag relationship" language does not pin the lag to exactly one
#     bar). The leader is the member with the highest OUT-STRENGTH (sum of
#     outgoing edge weights >= CORR_MIN) — i.e. the name whose past moves
#     best explain the most OTHER members' moves, a network-centrality read
#     of "leader" rather than v1's single-member-average read.
#   - CORR_MIN: empirically calibrated against this project's own
#     prices.parquet at implementation time using the SAME multi-lag-max
#     definition used here (not v1's lag-1-only definition, which is
#     numerically smaller) — the multi-lag max over 58 sampled names had
#     mean ~0.21, median ~0.19, p75 ~0.24. CORR_MIN=0.15 sits below the
#     median (keeps meaningful throughput) while still well above chance
#     for a 3-lag max-of-3 draw.
#   - Refresh cadence: WEEKLY (first trading day of each week), a faster
#     cadence than v1's MONTHLY — the multi-lag/out-strength leader
#     definition is more sensitive to short-lived co-movement bursts, so
#     this variant trades v1's turnover-minimizing bet for responsiveness.
#   - Regime eligibility: LOW_VOL + TRANSITIONING + HIGH_VOL (v1 restricts to
#     LOW_VOL + TRANSITIONING only). Rationale: this variant's liquidity
#     prefilter selects names least prone to correlation-structure
#     breakdown under stress, so the cluster-leadership edge is judged to
#     survive into HIGH_VOL; CRISIS is still excluded (forced-deleveraging
#     correlations go to 1 across nearly everything, destroying any
#     leader-specific signal).
#   - Direction: LONG the follower if the cluster leader's most recent return
#     was positive, SHORT if negative — read directly off the pseudocode
#     (unambiguous, unchanged from v1).
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

from strategies.base import BaseStrategy, Signal
from strategies.implementations._extra_panels import load_wide

__all__ = ['LeadLagClusterMinirocketV2']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_leadlag_cluster_minirocket_v2'

_RNG = np.random.RandomState(7)  # distinct seed from v1 — deterministic, no data-dependent randomness
_KERNEL_LEN = 7
_N_KERNELS = 24
_DILATIONS = (1, 2, 4, 8)
_KERNELS = [_RNG.randint(0, 2, size=_KERNEL_LEN) * 2 - 1 for _ in range(_N_KERNELS)]  # {-1,+1}^7


def _minirocket_features(x: np.ndarray) -> np.ndarray:
    """PPV + max-value features from fixed random dilated convolutional
    kernels — a lightweight, dependency-free stand-in for sktime's
    MiniRocketMultivariate (unavailable in this environment). Two pooled
    statistics per kernel/dilation, per the original ROCKET paper."""
    feats = []
    w = len(x)
    for kernel in _KERNELS:
        for d in _DILATIONS:
            span = (_KERNEL_LEN - 1) * d
            n_out = w - span
            if n_out <= 1:
                feats.extend([0.5, 0.0])
                continue
            conv = np.zeros(n_out)
            for j in range(_KERNEL_LEN):
                conv += kernel[j] * x[j * d: j * d + n_out]
            feats.append(float(np.mean(conv > 0)))
            feats.append(float(np.max(conv)))
    return np.array(feats, dtype=float)


class LeadLagClusterMinirocketV2(BaseStrategy):
    """Lead-lag cluster momentum (MiniRocket-KMeans variant 2): liquidity-
    prefiltered candidates, cluster by MiniRocket-style PPV+max features,
    silhouette-select k, identify each cluster's leader by multi-lag
    out-strength centrality, LONG/SHORT followers in the direction of the
    leader's most recent return. See module header for the interpretation
    choices made where the source paper's spec was ambiguous (variant 2 of 2)."""

    id                = STRATEGY_ID
    name              = 'LeadLagClusterMinirocketV2'
    description       = ('Liquidity-prefiltered lead-lag clusters via MiniRocket-style features + KMeans; '
                          'trade followers in the direction of their out-strength-centrality leader\'s prior return.')
    tier              = 2
    min_lookback      = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    ANALYSIS_WINDOW  = 504   # rolling clustering window (variant 2: full min_lookback, vs v1's shorter 252)
    MAX_CANDIDATES   = 150
    MAX_K            = 10
    CORR_MIN         = 0.15  # calibrated against real data using this variant's multi-lag-max definition
    BASE_SIZE        = 0.03

    def default_parameters(self) -> dict:
        return {'corr_min': self.CORR_MIN, 'base_size': self.BASE_SIZE}

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

        cols = sorted(t for t in universe if t in prices.columns)
        if len(cols) < 4:
            print('[debug] signals=0', file=sys.stderr)
            return []

        window_raw = prices[cols].iloc[-(self.ANALYSIS_WINDOW + 1):]
        coverage = window_raw.notna().mean()
        covered = sorted(coverage[coverage >= 0.6].index)

        # Liquidity prefilter: rank by trailing 20-bar median dollar volume.
        vol_panel = load_wide('volume', covered)
        if vol_panel is not None and not vol_panel.empty:
            common = [t for t in covered if t in vol_panel.columns]
            recent_px = window_raw[common].iloc[-20:]
            recent_vol = vol_panel[common].reindex(recent_px.index).iloc[-20:]
            dollar_vol = (recent_px * recent_vol).median()
            ranked = dollar_vol.dropna().sort_values(ascending=False)
            valid = list(ranked.index[:self.MAX_CANDIDATES])
        else:
            valid = covered[:self.MAX_CANDIDATES]

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
                labels = KMeans(n_clusters=k, n_init=10, random_state=7).fit_predict(feat_matrix)
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

            # Directed edge weights: max lag(1..3) cross-correlation i -> j.
            edges = {}
            for i in members:
                for j in members:
                    if i == j:
                        continue
                    vals = []
                    for lag in (1, 2, 3):
                        c = mem_ret[i].shift(lag).corr(mem_ret[j])
                        if pd.notna(c):
                            vals.append(c)
                    edges[(i, j)] = max(vals) if vals else -1.0

            out_strength = {}
            for i in members:
                out_strength[i] = sum(w for (a, _), w in edges.items() if a == i and w >= corr_min)
            leader = max(out_strength, key=out_strength.get) if out_strength else None
            if leader is None or out_strength[leader] <= 0.0:
                continue

            leader_move = float(mem_ret[leader].iloc[-1])
            if leader_move == 0.0 or not np.isfinite(leader_move):
                continue
            direction = 'LONG' if leader_move > 0.0 else 'SHORT'

            for follower in members:
                if follower == leader:
                    continue
                edge_w = edges.get((leader, follower), -1.0)
                if edge_w < corr_min:
                    continue
                price_series = prices[follower].dropna()
                if price_series.empty:
                    continue
                entry_price = float(price_series.iloc[-1])
                if not (np.isfinite(entry_price) and entry_price > 0.0):
                    continue
                conf = 'HIGH' if edge_w >= 0.24 else ('MED' if edge_w >= 0.19 else 'LOW')
                size = round(base_size * scale * (0.5 + 0.5 * min(edge_w, 1.0)), 4)
                levels = self.compute_stops_and_targets(price_series, direction, entry_price, regime_state=regime_state)
                signals.append(Signal(
                    ticker=follower, direction=direction, entry_price=entry_price,
                    stop_loss=levels['stop'], target_1=levels['t1'],
                    target_2=levels['t2'], target_3=levels['t3'],
                    position_size_pct=size, confidence=conf,
                    signal_params={
                        'leader': leader, 'cluster_id': int(cid), 'k': int(best_k),
                        'silhouette': round(float(best_score), 4),
                        'lead_lag_edge': round(float(edge_w), 4),
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

    strategy = LeadLagClusterMinirocketV2()
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
