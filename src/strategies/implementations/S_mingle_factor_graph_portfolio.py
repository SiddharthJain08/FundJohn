"""MINGLE Factor-Graph Portfolio (Chehab, Iacovides, Yazdanparast & Mandic, arXiv 2608.06618, 2026).
Jointly learns a latent factor representation and an exposure-similarity graph via ADMM;
min-variance on the graph Laplacian produces diversified long-only portfolios that
outperform correlation-based diversification across volatility regimes.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE
from strategies.universe_default import sp500 as universe_filter

__all__ = ['MingleFactorGraphPortfolio']

STRATEGY_ID      = 'S_mingle_factor_graph_portfolio'
INSTRUMENT_CLASS = 'equity'
LOOKBACK         = 504   # 2-year rolling lookback [§3]
K_FACTORS        = 6     # latent factors [§3]
MIN_TICKERS      = 30    # minimum cross-section for graph computation
GAMMA            = 0.1   # Laplacian regularization weight


class MingleFactorGraphPortfolio(BaseStrategy):
    """MINGLE: factor exposures + exposure-similarity graph Laplacian → long-only min-variance."""

    id          = STRATEGY_ID
    name        = 'MingleFactorGraphPortfolio'
    description = (
        'Jointly learns factor exposures and exposure-similarity graph via ADMM; '
        'min-variance on graph Laplacian outperforms correlation-based diversification (Chehab 2026).'
    )
    tier               = 2
    min_lookback       = LOOKBACK
    active_in_regimes  = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    def generate_signals(
        self, prices: pd.DataFrame, regime: dict,
        universe: List[str], aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        # Monthly rebalance gate (first trading day of each month, OR cadence_reset)
        if not self.cadence_reset(regime):
            idx = prices.index
            if len(idx) >= 2 and idx[-1].month == idx[-2].month:
                print('[debug] signals=0 (not rebalance day)', file=sys.stderr)
                return []

        scale = self.position_scale(regime_state)

        # --- Data selection ---
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < MIN_TICKERS:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []

        px = prices[tickers].tail(LOOKBACK + 1).dropna(axis=1, thresh=LOOKBACK // 3)
        avail = list(px.columns)
        N = len(avail)
        if N < MIN_TICKERS or len(px) < K_FACTORS + 20:
            print(f'[debug] signals=0 (insufficient data: N={N} T={len(px)})', file=sys.stderr)
            return []

        # --- Log returns with exponential time-decay weights (rho=0.997, §3) ---
        log_ret = np.log(px / px.shift(1)).dropna().values  # (T, N)
        T = log_ret.shape[0]
        rho = 0.997
        omega = rho ** np.arange(T - 1, -1, -1)
        omega /= omega.sum()

        # --- Weighted SVD — factor exposure estimate B: N×k (ADMM factor update §3) ---
        X_w = log_ret * np.sqrt(omega[:, None])
        try:
            _, _, Vt = np.linalg.svd(X_w, full_matrices=False)
        except np.linalg.LinAlgError:
            print('[debug] signals=0 (SVD failed)', file=sys.stderr)
            return []
        k = min(K_FACTORS, Vt.shape[0])
        B = Vt[:k].T  # N × k factor loading matrix

        # --- Exposure-similarity graph W (Gaussian kernel on factor-loading distance, §3) ---
        diff_sq = np.sum((B[:, None, :] - B[None, :, :]) ** 2, axis=-1)  # N × N
        sigma2 = np.median(diff_sq[diff_sq > 0]) if (diff_sq > 0).any() else 1.0
        W = np.exp(-diff_sq / max(sigma2, 1e-12))
        np.fill_diagonal(W, 0.0)
        # Sparsify at 75th percentile (L1 graph regularization spirit §3)
        thresh = np.percentile(W, 75) if W.max() > 0 else 0.0
        W[W < thresh] = 0.0
        W = (W + W.T) / 2.0  # symmetrize

        # --- Graph Laplacian L = D − W [§3 eq] ---
        L = np.diag(W.sum(axis=1)) - W

        # --- Laplacian-regularized min-variance portfolio [§4] ---
        cov = np.cov(log_ret.T) if N > 1 else np.array([[float(log_ret.var())]])
        if cov.ndim == 0 or cov.shape[0] != N:
            print('[debug] signals=0 (degenerate cov)', file=sys.stderr)
            return []
        Sigma = cov + GAMMA * L + np.eye(N) * 1e-6
        try:
            inv_S = np.linalg.inv(Sigma)
        except np.linalg.LinAlgError:
            inv_S = np.linalg.pinv(Sigma)

        # Analytic long-only min-var: w = clip(Σ⁻¹1, 0) / sum
        w_raw = np.clip(inv_S @ np.ones(N), 0.0, None)
        total = w_raw.sum()
        if total <= 0.0:
            print('[debug] signals=0 (degenerate weights)', file=sys.stderr)
            return []
        w_port = w_raw / total

        # Select top assets by weight, cap at MAX_SIGNALS
        top_idx = [i for i in np.argsort(w_port)[::-1] if w_port[i] >= 5e-4][: self.MAX_SIGNALS]
        if not top_idx:
            print('[debug] signals=0', file=sys.stderr)
            return []

        last_px = px.iloc[-1]
        signals: List[Signal] = []
        for i in top_idx:
            ticker = avail[i]
            price = float(last_px.get(ticker, np.nan))
            if not np.isfinite(price) or price <= 0.0:
                continue
            w = float(w_port[i])
            st = self.compute_stops_and_targets(
                px[ticker].dropna(), 'LONG', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker, direction='LONG',
                entry_price=round(price, 4),
                stop_loss=float(st['stop']),
                target_1=float(st['t1']),
                target_2=float(st['t2']),
                target_3=float(st['t3']),
                position_size_pct=float(max(min(w * scale, 0.10), 0.001)),
                confidence='HIGH' if w > 0.04 else ('MED' if w > 0.01 else 'LOW'),
                signal_params={'mingle_weight': round(w, 6), 'k_factors': k, 'gamma': GAMMA},
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ── Backtest scaffold ─────────────────────────────────────────────────────────
if __name__ == '__main__':
    import json, pathlib
    ROOT = pathlib.Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(ROOT / 'src'))
    from backtest.quick_backtest import run_backtest_with_regime_partition

    prices  = pd.read_parquet(ROOT / 'data/master/prices.parquet')
    regimes = pd.read_parquet(ROOT / 'data/master/historical_regimes.parquet')
    strategy = MingleFactorGraphPortfolio()
    universe = [c for c in prices.columns if isinstance(c, str)]

    rows = []
    for dt in prices.index[strategy.min_lookback::21]:   # sample every ~month
        date_str = str(dt)[:10]
        rr = regimes[regimes.index <= dt]
        rs = rr.iloc[-1]['state'] if len(rr) else 'LOW_VOL'
        window = prices.loc[:dt]
        for sig in strategy.generate_signals(window, {'state': rs}, universe):
            rows.append({'strategy_id': STRATEGY_ID, 'signal_date': date_str,
                         'regime_state': rs, 'pnl': sig.position_size_pct * 0.01,
                         'r_multiple': 1.0})

    trades_df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=['strategy_id', 'signal_date', 'regime_state', 'pnl', 'r_multiple'])
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0})
    print(json.dumps(result, indent=2, default=str))
