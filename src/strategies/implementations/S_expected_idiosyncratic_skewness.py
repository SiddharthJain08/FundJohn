from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['ExpectedIdiosyncraticSkewness']


class ExpectedIdiosyncraticSkewness(BaseStrategy):
    """Sort on forecasted idiosyncratic skewness: LONG low-skew, SHORT high-skew stocks."""

    id                = 'S_expected_idiosyncratic_skewness'
    name              = 'ExpectedIdiosyncraticSkewness'
    description       = 'Sort on forecasted idiosyncratic skewness: LONG low-skew, SHORT high-skew stocks'
    tier              = 2
    min_lookback      = 756
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    RESID_WINDOW  = 252   # 1-year residual window for current IS/IV snapshot
    LAG_WINDOW    = 126   # 6-month lagged window for cross-sectional predictor
    POS_PER_STOCK = 0.015 # per-stock position fraction before regime scale
    MIN_STOCKS    = 50    # minimum universe after filtering

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        scale = self.position_scale(regime_state)

        avail = [t for t in universe if t in prices.columns]
        pdata = prices[avail].dropna(axis=1, thresh=int(len(prices) * 0.70)).ffill()
        if len(pdata) < self.min_lookback or len(pdata.columns) < self.MIN_STOCKS:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        tickers = list(pdata.columns)
        rets    = pdata.pct_change().dropna()

        # Market factor proxy: equal-weighted cross-sectional return
        mkt = rets.mean(axis=1)

        def _compute_is_iv(window: int):
            """Return (is_map, iv_map) dicts for the given trailing window."""
            r_win = rets.tail(window)
            m_win = mkt.reindex(r_win.index)
            X_mkt = np.column_stack([np.ones(len(m_win)), m_win.values])
            is_map: dict = {}
            iv_map: dict = {}
            for tk in tickers:
                r = r_win[tk].values
                mask = np.isfinite(r)
                if mask.sum() < window // 2:
                    continue
                r_clean = r[mask]
                X_clean = X_mkt[mask]
                try:
                    bhat, _, _, _ = np.linalg.lstsq(X_clean, r_clean, rcond=None)
                    resid = r_clean - X_clean @ bhat
                except Exception:
                    continue
                T     = len(resid)
                sum2  = float(np.sum(resid ** 2))
                sum3  = float(np.sum(resid ** 3))
                if sum2 < 1e-14:
                    continue
                is_map[tk] = float(T * sum3 / (sum2 ** 1.5))
                iv_map[tk] = float(np.std(resid, ddof=1))
            return is_map, iv_map

        # Current snapshot (longer window)
        is_cur, iv_cur = _compute_is_iv(self.RESID_WINDOW)
        # Lagged snapshot (shorter window = predictors per §3.2)
        is_lag, iv_lag = _compute_is_iv(self.LAG_WINDOW)

        # Intersection of tickers with both snapshots
        common = [t for t in is_cur if t in is_lag and t in iv_lag]
        if len(common) < self.MIN_STOCKS:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Cross-sectional OLS: IS_cur = b0 + b1*IS_lag + b2*IV_lag
        # Gives stable β coefficients for forecasting next-period IS
        y    = np.array([is_cur[t] for t in common], dtype=float)
        x_is = np.array([is_lag[t] for t in common], dtype=float)
        x_iv = np.array([iv_lag[t] for t in common], dtype=float)

        # Winsorize inputs at 1st/99th percentile to suppress outliers
        for arr in [y, x_is, x_iv]:
            lo, hi = np.percentile(arr, [1, 99])
            arr[:] = np.clip(arr, lo, hi)

        X_cs = np.column_stack([np.ones(len(common)), x_is, x_iv])
        try:
            betas, _, _, _ = np.linalg.lstsq(X_cs, y, rcond=None)
        except Exception:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Forecast expected IS for next period using current IS + IV as predictors
        is_arr = np.array([is_cur[t] for t in common], dtype=float)
        iv_arr = np.array([iv_cur[t] for t in common], dtype=float)
        is_arr = np.clip(is_arr, *np.percentile(is_arr, [1, 99]))
        iv_arr = np.clip(iv_arr, *np.percentile(iv_arr, [1, 99]))
        X_fwd  = np.column_stack([np.ones(len(common)), is_arr, iv_arr])
        e_is   = X_fwd @ betas   # E[IS_{t+1}]

        ranked  = pd.Series(e_is, index=common).sort_values()
        n       = len(ranked)
        q_size  = max(1, n // 5)

        longs   = ranked.index[:q_size].tolist()   # low expected skew — underpriced
        shorts  = ranked.index[-q_size:].tolist()  # high expected skew — overpriced

        signals: List[Signal] = []
        pos = min(self.POS_PER_STOCK * scale, 0.04)
        half_cap = self.MAX_SIGNALS // 2

        for direction, candidates, quintile_tag in [
            ('LONG',  longs,  'Q1_low_skew'),
            ('SHORT', shorts, 'Q5_high_skew'),
        ]:
            for tk in candidates:
                if len(signals) >= (half_cap if direction == 'LONG' else self.MAX_SIGNALS):
                    break
                ps    = pdata[tk]
                price = float(ps.iloc[-1])
                if price <= 0:
                    continue
                st = self.compute_stops_and_targets(ps, direction, price, regime_state=regime_state)
                signals.append(Signal(
                    ticker=tk, direction=direction, entry_price=price,
                    stop_loss=st['stop'], target_1=st['t1'],
                    target_2=st['t2'], target_3=st['t3'],
                    position_size_pct=pos, confidence='MED',
                    signal_params={
                        'e_is':     round(float(ranked[tk]), 5),
                        'quintile': quintile_tag,
                    },
                    features={
                        'idio_skewness': round(float(is_cur.get(tk, 0.0)), 5),
                        'idio_vol':      round(float(iv_cur.get(tk, 0.0)), 5),
                    },
                ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
