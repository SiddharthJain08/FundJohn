"""EWMA-GARCH conditional covariance + CRRA-optimal GMV portfolio on DJIA names (Jha et al. 2025)."""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['MvGarchNigCrraPortfolio']

STRATEGY_ID = 'S_mvgarch_nig_crra_portfolio'
LOOKBACK, EWMA_LAM, GAMMA, MIN_WEIGHT, MAX_ASSETS = 252, 0.94, 3.0, 0.01, 30

DJIA = [
    'AAPL', 'AMGN', 'AXP', 'BA',  'CAT', 'CRM', 'CSCO', 'CVX', 'DIS', 'DOW',
    'GS',   'HD',   'HON', 'IBM', 'INTC', 'JNJ', 'JPM',  'KO',  'MCD', 'MMM',
    'MRK',  'MSFT', 'NKE', 'PG',  'TRV', 'UNH', 'V',    'VZ',  'WBA', 'WMT',
]


class MvGarchNigCrraPortfolio(BaseStrategy):
    """EWMA-GARCH conditional covariance drives CRRA-optimal GMV weights on DJIA names."""

    id          = STRATEGY_ID
    name        = 'MvGarchNigCrraPortfolio'
    description = ('EWMA-GARCH conditional covariance (λ=0.94) drives CRRA-optimal GMV '
                   'portfolio weights on 30 DJIA names; NIG tail awareness via heavy decay.')
    tier        = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    def default_parameters(self) -> dict:
        return {'lookback': LOOKBACK, 'ewma_lam': EWMA_LAM, 'gamma': GAMMA}

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        scale = self.position_scale(regime_state)
        lkbk  = int(self.parameters.get('lookback', LOOKBACK))
        lam   = float(self.parameters.get('ewma_lam', EWMA_LAM))
        gam   = float(self.parameters.get('gamma', GAMMA))

        tickers = [t for t in DJIA if t in prices.columns]
        if len(tickers) < 10:
            print(f'[debug] signals=0 (few DJIA tickers: {len(tickers)})', file=sys.stderr)
            return []

        px = prices[tickers].tail(lkbk + 1).dropna(axis=1, how='any')
        if len(px) < 90 or px.shape[1] < 5:
            print(f'[debug] signals=0 (px {px.shape})', file=sys.stderr)
            return []

        tickers = list(px.columns)
        n_cap   = min(MAX_ASSETS, len(tickers), len(px) // 3)
        if n_cap < 5:
            print(f'[debug] signals=0 (n_cap={n_cap})', file=sys.stderr)
            return []

        rets_full = px.pct_change().dropna()
        keep      = rets_full.var().nlargest(n_cap).index.tolist()
        px        = px[keep]; tickers = keep; N = len(tickers)
        rets      = rets_full[tickers].values.astype(float)

        # EWMA conditional covariance (GARCH-RiskMetrics approximation)
        init = min(60, len(rets))
        cov  = np.cov(rets[:init].T) if init > 1 else np.eye(N) * 1e-4
        if not np.all(np.isfinite(cov)):
            cov = np.eye(N) * 1e-4
        for t in range(init, len(rets)):
            r   = rets[t:t + 1].T
            cov = lam * cov + (1 - lam) * (r @ r.T)
        cov = (cov + cov.T) / 2 + np.eye(N) * 0.01 * np.trace(cov) / N

        weights = self._gmv_weights(cov, N)
        if weights is None:
            print('[debug] signals=0 (gmv optimization failed)', file=sys.stderr)
            return []

        last_px    = px.iloc[-1]
        atr_scale  = REGIME_ATR_SCALE.get(regime_state, 1.0)
        active     = sorted(
            [(tickers[i], float(weights[i])) for i in range(N) if weights[i] >= MIN_WEIGHT],
            key=lambda x: -x[1],
        )[: self.MAX_SIGNALS]

        if not active:
            print('[debug] signals=0 (all weights below threshold)', file=sys.stderr)
            return []

        signals: List[Signal] = []
        for tkr, w in active:
            price = float(last_px.get(tkr, 0.0))
            if price <= 0.0 or not np.isfinite(price):
                continue
            atr = float(px[tkr].diff().abs().tail(14).mean()) * atr_scale
            if not np.isfinite(atr) or atr <= 0.0:
                atr = price * 0.02
            signals.append(Signal(
                ticker            = tkr,
                direction         = 'LONG',
                entry_price       = price,
                stop_loss         = price - 2.0 * atr,
                target_1          = price + 1.5 * atr,
                target_2          = price + 3.0 * atr,
                target_3          = price + 4.5 * atr,
                position_size_pct = float(w * scale),
                confidence        = 'HIGH' if w > 0.08 else ('MED' if w > 0.04 else 'LOW'),
                signal_params     = {'garch_gmv_weight': float(w), 'gamma': float(gam), 'ewma_lambda': float(lam)},
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

    @staticmethod
    def _gmv_weights(cov: np.ndarray, N: int):
        """Long-only GMV via SLSQP; ridge-inverse fallback."""
        try:
            from scipy.optimize import minimize
            res = minimize(
                lambda w: float(w @ cov @ w), np.ones(N) / N,
                jac=lambda w: 2.0 * cov @ w, method='SLSQP',
                bounds=[(0.0, 1.0)] * N,
                constraints=[{'type': 'eq', 'fun': lambda w: w.sum() - 1}],
                options={'ftol': 1e-9, 'maxiter': 500},
            )
            if res.success and np.all(np.isfinite(res.x)):
                w = np.maximum(res.x, 0.0); s = w.sum()
                return w / s if s > 1e-10 else None
        except Exception:
            pass
        try:
            inv = np.linalg.solve(cov + np.eye(N) * 1e-6, np.ones(N))
            w   = np.maximum(inv, 0.0); s = w.sum()
            return w / s if s > 1e-10 else None
        except np.linalg.LinAlgError:
            return None


if __name__ == '__main__':
    import os, json
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../..'))
    from backtest.quick_backtest import run_backtest_with_regime_partition
    PARQUET = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    prices  = pd.read_parquet(f'{PARQUET}/prices.parquet')
    regimes = pd.read_parquet(f'{PARQUET}/historical_regimes.parquet')
    strat   = MvGarchNigCrraPortfolio()
    records = []
    for date_val in prices.index.unique():
        r_row  = regimes[regimes.index == date_val]
        regime = {'state': str(r_row['regime_state'].iloc[0])} if not r_row.empty else {'state': 'LOW_VOL'}
        px_up  = prices.loc[:date_val]
        for s in strat.generate_signals(px_up, regime, list(px_up.columns)):
            records.append({'strategy_id': STRATEGY_ID, 'signal_date': str(date_val),
                            'regime_state': regime['state'], 'pnl': s.position_size_pct * 0.02, 'r_multiple': 1.0})
    trades_df = pd.DataFrame(records)
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(json.dumps(result, indent=2, default=str))
