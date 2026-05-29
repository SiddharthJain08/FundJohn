"""
S_bppp_bayesian_parametric_weights — Herculano (2026) arXiv:2602.21173
Bayesian Parametric Portfolio Policy: places a Laplace prior (L1 shrinkage)
on cross-sectional signal coefficients, shrinking overexposure to noisy
signals. LONG top quintile, SHORT bottom quintile by BPPP weight.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['BPPPBayesianParametricWeights']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_bppp_bayesian_parametric_weights'
_LAPLACE_LAMBDA  = 0.05   # L1 prior scale — controls shrinkage strength
_GAMMA           = 3.0    # CRRA risk aversion (MV approximation)


class BPPPBayesianParametricWeights(BaseStrategy):
    """Bayesian PPP: L1-shrunk cross-sectional signal weights — Herculano (2026)."""

    id               = STRATEGY_ID
    name             = 'BPPPBayesianParametricWeights'
    description      = ('Bayesian Parametric Portfolio Policy with Laplace prior shrinkage '
                        'on momentum, low-vol, and value signals — Herculano (2026)')
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 1260
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

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

        financials = (aux_data or {}).get('financials', {})
        scale = self.position_scale(regime_state)

        # ── Step 1: Build per-ticker signal vector ────────────────────────────
        rows: dict[str, dict] = {}
        for ticker in universe:
            if ticker not in prices.columns:
                continue
            ts = prices[ticker].dropna()
            if len(ts) < self.min_lookback:
                continue

            # Signal A: 12m-1m cross-sectional momentum (skip 1m reversal)
            mom12 = float(ts.iloc[-21] / ts.iloc[-252] - 1.0)
            # Signal B: 6m-1m intermediate momentum
            mom6 = float(ts.iloc[-21] / ts.iloc[-126] - 1.0) if len(ts) >= 126 else mom12
            # Signal C: low-volatility (negative realized vol → positive tilt)
            rvol = float(ts.pct_change().iloc[-63:].std() * np.sqrt(252))

            # Signal D: earnings yield (value) from financials when available
            fin = (financials.get(ticker) or {}) if financials else {}
            price_now = float(ts.iloc[-1])
            ey: float | None = None
            if fin.get('eps') and price_now > 0:
                try:
                    ey = float(fin['eps']) / price_now
                except (TypeError, ValueError):
                    pass
            if ey is None:
                mc = fin.get('marketCap')
                ni = fin.get('netIncome')
                if mc and ni:
                    try:
                        mc_f = float(mc)
                        ey = float(ni) / mc_f if mc_f > 0 else None
                    except (TypeError, ValueError):
                        pass

            rows[ticker] = {
                'mom12': mom12,
                'mom6':  mom6,
                'rvol':  rvol,
                'ey':    ey,
                'price': price_now,
                'ts':    ts,
            }

        if len(rows) < 50:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        tickers = list(rows.keys())
        n = len(tickers)

        # ── Step 2: Cross-sectional standardization [§3.1] ───────────────────
        def _cs_std(arr: np.ndarray) -> np.ndarray:
            mu, sd = np.nanmean(arr), np.nanstd(arr)
            if sd < 1e-9:
                return np.zeros(len(arr))
            return np.clip((arr - mu) / sd, -3.0, 3.0)

        z_mom12 = _cs_std(np.array([rows[t]['mom12'] for t in tickers], dtype=float))
        z_mom6  = _cs_std(np.array([rows[t]['mom6']  for t in tickers], dtype=float))
        z_lvol  = _cs_std(np.array([-rows[t]['rvol'] for t in tickers], dtype=float))  # low-vol = good
        ey_arr  = np.array([rows[t]['ey'] if rows[t]['ey'] is not None else np.nan
                            for t in tickers], dtype=float)
        z_ey    = _cs_std(ey_arr)
        np.nan_to_num(z_ey, copy=False, nan=0.0)

        Z = np.column_stack([z_mom12, z_mom6, z_lvol, z_ey])   # [n × 4]

        # ── Step 3: OLS theta estimate then Laplace MAP (soft-thresholding) ──
        # Use recent 21-day cross-sectional returns as a proxy signal-label
        try:
            ret21 = prices[tickers].pct_change(21).iloc[-1].values.astype(float)
            vmask = np.isfinite(ret21)
            if vmask.sum() >= 30:
                Zv = Z[vmask];  rv = ret21[vmask]
                ZtZ = Zv.T @ Zv / len(rv) + np.eye(4) * 1e-4
                Ztr = Zv.T @ rv / len(rv)
                theta_ols = np.linalg.solve(ZtZ, Ztr)
            else:
                theta_ols = np.zeros(4)
        except Exception:
            theta_ols = np.zeros(4)

        # Laplace MAP = soft-threshold (L1 shrinkage toward zero)
        lam = _LAPLACE_LAMBDA
        theta_map = np.sign(theta_ols) * np.maximum(np.abs(theta_ols) - lam, 0.0)

        # ── Step 4: BPPP portfolio weights ────────────────────────────────────
        # w_i = 1/N + E[theta|D_T] @ z_i    [Herculano §3.1 eq. 5]
        eq_wt   = 1.0 / n
        tilts   = Z @ theta_map                   # [n,]
        weights = eq_wt + tilts

        # Enforce constraints: max|w_i| <= 0.60, sum|w_i| <= 2.0
        max_abs = np.max(np.abs(weights))
        if max_abs > 0.60:
            weights *= 0.60 / max_abs
        lev = np.sum(np.abs(weights))
        if lev > 2.0:
            weights *= 2.0 / lev

        # ── Step 5: Select top/bottom quintile by BPPP weight ────────────────
        sorted_idx = np.argsort(weights)
        q = max(n // 5, 1)
        q = min(q, self.MAX_SIGNALS // 2)
        bottom_idx = sorted_idx[:q]
        top_idx    = sorted_idx[-q:]
        per_pos = min(round(scale / max(q, 1), 4), 0.04)

        signals: List[Signal] = []

        for idx in top_idx:
            t = tickers[idx]; d = rows[t]
            price = d['price']
            stops = self.compute_stops_and_targets(d['ts'], 'LONG', price, regime_state=regime_state)
            wt = float(weights[idx])
            conf = 'HIGH' if wt > eq_wt * 1.5 else ('MED' if wt > eq_wt * 1.1 else 'LOW')
            signals.append(Signal(
                ticker=t, direction='LONG', entry_price=price,
                stop_loss=stops['stop'], target_1=stops['t1'],
                target_2=stops['t2'], target_3=stops['t3'],
                position_size_pct=per_pos, confidence=conf,
                signal_params={
                    'bppp_weight': round(wt, 6),
                    'tilt': round(float(tilts[idx]), 6),
                    'theta_map': [round(float(x), 4) for x in theta_map],
                },
            ))

        for idx in bottom_idx:
            t = tickers[idx]; d = rows[t]
            price = d['price']
            stops = self.compute_stops_and_targets(d['ts'], 'SHORT', price, regime_state=regime_state)
            wt = float(weights[idx])
            conf = 'HIGH' if wt < eq_wt * 0.5 else ('MED' if wt < eq_wt * 0.9 else 'LOW')
            signals.append(Signal(
                ticker=t, direction='SHORT', entry_price=price,
                stop_loss=stops['stop'], target_1=stops['t1'],
                target_2=stops['t2'], target_3=stops['t3'],
                position_size_pct=per_pos, confidence=conf,
                signal_params={
                    'bppp_weight': round(wt, 6),
                    'tilt': round(float(tilts[idx]), 6),
                    'theta_map': [round(float(x), 4) for x in theta_map],
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
