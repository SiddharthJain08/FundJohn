"""
S_conformal_kelly_etp_sizing — Conformal Kelly ETP Sizing

Conformal prediction intervals derived from per-asset rolling return quantiles
replace variance in fractional Kelly sizing across 8 liquid ETFs (SPY, QQQ,
DIA, MDY, GLD, SLV, USO, DBC). Wider intervals shrink exposure; narrower
intervals grow it. Paper: Ryan 2026 (arXiv:2608.01494v1). IS Sharpe 1.34;
pre-registered OOS Sharpe ~0.44 / DD 36.6% — below passive S&P benchmark.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

INSTRUMENT_CLASS = "etp"
STRATEGY_ID = "S_conformal_kelly_etp_sizing"
ETP_BASKET = ['SPY', 'QQQ', 'DIA', 'MDY', 'GLD', 'SLV', 'USO', 'DBC']

__all__ = ['ConformalKellyEtpSizing']


class ConformalKellyEtpSizing(BaseStrategy):
    """Fractional Kelly sizing with conformal prediction intervals as the scale, across 8 ETFs."""

    id                = 'S_conformal_kelly_etp_sizing'
    name              = 'ConformalKellyEtpSizing'
    description       = ('Conformal prediction intervals replace variance in fractional Kelly sizing '
                         'across 8 liquid ETFs; wider intervals shrink exposure, narrower grow it.')
    tier              = 2
    min_lookback      = 756
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    KAPPA      = 0.15    # fractional Kelly coefficient (§3.3)
    RIDGE_LAM  = 10.0    # ridge regularisation λ (§3.1)
    CONF_ALPHA = 0.875   # conformal quantile level (§3.2)
    Z_ALPHA    = 1.15    # z_{0.875} ≈ 1.15
    SHRINK_LAM = 0.3     # geometric shrinkage toward expanding anchor
    CONF_WIN   = 500     # rolling nonconformity window (days)

    def default_parameters(self) -> dict:
        return {'kappa': self.KAPPA, 'ridge_lam': self.RIDGE_LAM, 'leverage_cap': 1.5}

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

        tickers = [t for t in ETP_BASKET if t in prices.columns]
        if len(tickers) < 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        prices_sub = prices[tickers].dropna(how='all')
        if len(prices_sub) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        try:
            weights = self._compute_weights(prices_sub, tickers)
        except Exception as exc:
            print(f'[debug] signals=0 exc={exc}', file=sys.stderr)
            return []

        if weights is None or weights.sum() < 1e-6:
            print('[debug] signals=0', file=sys.stderr)
            return []

        last_row = prices_sub.iloc[-1]
        signals: List[Signal] = []
        for ticker in tickers:
            w = float(weights.get(ticker, 0.0))
            if w < 0.005:
                continue
            price = float(last_row[ticker])
            if np.isnan(price) or price <= 0:
                continue
            stops = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), 'LONG', price, regime_state=regime_state,
            )
            pos = round(min(w * scale, 0.40), 4)
            confidence = 'HIGH' if w > 0.20 else ('MED' if w > 0.08 else 'LOW')
            signals.append(Signal(
                ticker=ticker, direction='LONG', entry_price=price,
                stop_loss=stops['stop'], target_1=stops['t1'], target_2=stops['t2'], target_3=stops['t3'],
                position_size_pct=pos, confidence=confidence,
                signal_params={'kelly_weight': round(w, 4), 'regime_scale': round(scale, 3)},
            ))

        signals = signals[:self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

    # ── Internals ─────────────────────────────────────────────────────────────

    def _compute_weights(self, prices: pd.DataFrame, tickers: list):
        returns = prices.pct_change().dropna(how='all')
        if len(returns) < self.min_lookback:
            return None

        kappa = self.parameters.get('kappa', self.KAPPA)
        leverage_cap = self.parameters.get('leverage_cap', 1.5)

        mu_hats, sig_hats = {}, {}
        for t in tickers:
            ret = returns[t].dropna()
            if len(ret) < 300:
                mu_hats[t], sig_hats[t] = 0.0, 0.05
            else:
                mu_hats[t], sig_hats[t] = self._conformal_params(ret)

        raw: dict[str, float] = {}
        for t in tickers:
            sigma = max(sig_hats[t], 1e-6)
            f = kappa * mu_hats[t] / (sigma ** 2)
            raw[t] = max(f, 0.0)  # long-only

        total = sum(raw.values())
        if total < 1e-6:
            return None

        weights = pd.Series({t: v / total for t, v in raw.items()})
        if self._leverage_flag(returns, tickers):
            weights = weights * 0.5
        return weights

    def _conformal_params(self, ret: pd.Series):
        """Expanding-window ridge regression + conformal interval sizing (§3.1–3.2)."""
        lam = self.parameters.get('ridge_lam', self.RIDGE_LAM)

        mom_21 = ret.rolling(21).sum()
        mom_63 = ret.rolling(63).sum()
        mom_252 = ret.rolling(252).sum()
        ewma_v  = ret.ewm(span=20).std()
        target  = ret.rolling(21).sum().shift(-21)

        df = pd.DataFrame(
            {'m21': mom_21, 'm63': mom_63, 'm252': mom_252, 'vol': ewma_v, 'y': target}
        ).dropna()
        # Prediction features are target-free: joining y (shift(-21)) truncates the
        # last 21 rows, so df.iloc[-1] would be T-21 features, not T.
        feat_df = pd.DataFrame(
            {'m21': mom_21, 'm63': mom_63, 'm252': mom_252, 'vol': ewma_v}
        ).dropna()

        if len(df) < 100:
            return float(ret.iloc[-21:].sum()), max(float(ret.std() * np.sqrt(21)), 1e-4)

        train = df.iloc[:-21] if len(df) > 21 else df
        if len(train) < 50:
            return float(ret.iloc[-21:].sum()), max(float(ret.std() * np.sqrt(21)), 1e-4)

        X = train[['m21', 'm63', 'm252', 'vol']].values
        y = train['y'].values
        n = X.shape[1]
        beta = np.linalg.solve(X.T @ X + lam * np.eye(n), X.T @ y)

        last = feat_df.iloc[-1].values
        mu_hat = float(last @ beta)

        nc   = np.abs(y - X @ beta)
        win  = min(self.CONF_WIN, len(nc))
        rq   = float(np.quantile(nc[-win:], self.CONF_ALPHA))
        eq   = float(np.quantile(nc, self.CONF_ALPHA))
        q_eff = (1.0 - self.SHRINK_LAM) * rq + self.SHRINK_LAM * eq
        return mu_hat, max(q_eff / self.Z_ALPHA, 1e-4)

    def _leverage_flag(self, returns: pd.DataFrame, tickers: list) -> bool:
        """Risk control (§4): scale down if recent downside miss rate exceeds historical."""
        try:
            hist   = returns[tickers].iloc[-252:]
            recent = returns[tickers].iloc[-63:]
            if len(recent) < 21 or len(hist) < 63:
                return False
            miss_h = float((hist < -0.02).values.mean())
            miss_r = float((recent < -0.02).values.mean())
            return miss_r > miss_h * 1.5
        except Exception:
            return False


# ── Standalone regime-partitioned backtest ────────────────────────────────────

def _run_backtest(
    prices_path: str, regimes_path: str,
    start: str = '2017-01-01', end: str = '2025-12-31',
) -> pd.DataFrame:
    """Monthly-bar walk-forward returning trades_df for regime partitioning."""
    import os
    empty = pd.DataFrame(columns=['strategy_id', 'signal_date', 'regime_state', 'pnl', 'r_multiple'])
    if not os.path.exists(prices_path):
        return empty

    all_cols = pd.read_parquet(prices_path, columns=[]).columns.tolist()
    load_cols = [t for t in ETP_BASKET if t in all_cols]
    if not load_cols:
        return empty

    prices_df = pd.read_parquet(prices_path, columns=load_cols).loc[start:end]

    regimes: dict[str, str] = {}
    if os.path.exists(regimes_path):
        reg_df = pd.read_parquet(regimes_path)
        dc = 'date' if 'date' in reg_df.columns else reg_df.columns[0]
        sc = 'regime_state' if 'regime_state' in reg_df.columns else reg_df.columns[1]
        for _, row in reg_df.iterrows():
            regimes[str(row[dc])[:10]] = str(row[sc])

    strategy = ConformalKellyEtpSizing()
    ref_col = next((c for c in ('SPY', 'QQQ') if c in prices_df.columns), load_cols[0])
    monthly_dates = prices_df[ref_col].dropna().resample('MS').first().dropna().index
    records = []

    for sig_date in monthly_dates:
        date_str = str(sig_date.date())
        win = prices_df.loc[:sig_date]
        if len(win) < strategy.min_lookback:
            continue
        regime_state = regimes.get(date_str, 'LOW_VOL')
        sigs = strategy.generate_signals(win, {'state': regime_state}, load_cols)
        if not sigs:
            continue
        pnl_list = []
        for sig in sigs:
            if sig.ticker not in prices_df.columns:
                continue
            fut = prices_df[sig.ticker].dropna().loc[sig_date:]
            if len(fut) < 22:
                continue
            entry, exit_ = float(fut.iloc[0]), float(fut.iloc[min(21, len(fut) - 1)])
            if entry <= 0:
                continue
            pnl_list.append((exit_ - entry) / entry)
        if not pnl_list:
            continue
        avg_pnl = float(sum(pnl_list) / len(pnl_list))
        records.append({
            'strategy_id': STRATEGY_ID, 'signal_date': date_str,
            'regime_state': regime_state, 'pnl': avg_pnl,
            'r_multiple': avg_pnl / 0.02 if avg_pnl != 0 else 0.0,
        })

    return pd.DataFrame(records)


if __name__ == '__main__':
    import json
    from backtest.quick_backtest import run_backtest_with_regime_partition

    PARQUET_ROOT = '/root/openclaw/data/master'
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
