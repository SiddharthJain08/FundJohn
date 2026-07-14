"""
S_accrual_anomaly — Accrual Anomaly (Sloan 1996 / Quantpedia)
Source: https://quantpedia.com/strategies/accrual-anomaly/

LONG bottom-decile balance-sheet accruals (highest earnings quality),
SHORT top-decile balance-sheet accruals (lowest earnings quality).
Annual rebalance in May after all companies publish annual earnings.

BS_ACC = (ΔCA − ΔCash) − (ΔCL − ΔSTD − ΔITP) − Dep
         -----------------------------------------------
                    avg(TotalAssets)

Normalized by average total assets (Sloan 1996 convention).
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE
from src.strategies.universe_default import no_otc as universe_filter

__all__ = ['AccrualAnomaly']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_accrual_anomaly'


def _float_or_none(v) -> float | None:
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _compute_bs_acc(fin: dict) -> float | None:
    """
    Compute balance-sheet accruals scaled by avg total assets.
    Returns None if required fields are missing.
    """
    # ── current-year fields ───────────────────────────────────────────────────
    ca   = _float_or_none(fin.get('currentAssets') or fin.get('totalCurrentAssets'))
    cash = _float_or_none(fin.get('cashAndCashEquivalents') or fin.get('cashAndShortTermInvestments'))
    cl   = _float_or_none(fin.get('currentLiabilities') or fin.get('totalCurrentLiabilities'))
    std  = _float_or_none(fin.get('currentDebt') or fin.get('shortTermDebt') or fin.get('currentPortionOfLongTermDebt') or 0.0)
    itp  = _float_or_none(fin.get('incomeTaxPayable') or fin.get('taxPayable') or 0.0)
    dep  = _float_or_none(fin.get('depreciationAndAmortization') or fin.get('depreciation') or fin.get('depreciationAmortization') or 0.0)
    ta   = _float_or_none(fin.get('totalAssets'))

    # ── prior-year fields (FMP sometimes provides *PriorYear / *PY keys) ─────
    ca_py   = _float_or_none(fin.get('currentAssetsPriorYear') or fin.get('currentAssetsPY') or fin.get('totalCurrentAssetsPriorYear'))
    cash_py = _float_or_none(fin.get('cashAndCashEquivalentsPriorYear') or fin.get('cashAndCashEquivalentsPY'))
    cl_py   = _float_or_none(fin.get('currentLiabilitiesPriorYear') or fin.get('currentLiabilitiesPY') or fin.get('totalCurrentLiabilitiesPriorYear'))
    std_py  = _float_or_none(fin.get('currentDebtPriorYear') or fin.get('shortTermDebtPriorYear') or fin.get('currentDebtPY') or 0.0)
    itp_py  = _float_or_none(fin.get('incomeTaxPayablePriorYear') or fin.get('taxPayablePriorYear') or fin.get('incomeTaxPayablePY') or 0.0)
    ta_py   = _float_or_none(fin.get('totalAssetsPriorYear') or fin.get('totalAssetsPY') or fin.get('totalAssetsPreviousYear'))

    # ── require all non-optional current-year fields ──────────────────────────
    if any(v is None for v in [ca, cash, cl, ta]):
        return None
    # require prior-year for at least the 3 main balance-sheet items
    if any(v is None for v in [ca_py, cash_py, cl_py]):
        return None

    # ── safe defaults for optional prior-year fields ──────────────────────────
    std  = std  if std  is not None else 0.0
    itp  = itp  if itp  is not None else 0.0
    dep  = dep  if dep  is not None else 0.0
    std_py = std_py if std_py is not None else 0.0
    itp_py = itp_py if itp_py is not None else 0.0
    ta_py  = ta_py  if ta_py  is not None else ta

    avg_ta = (ta + ta_py) / 2.0
    if avg_ta <= 0.0:
        return None

    delta_ca   = ca   - ca_py
    delta_cash = cash - cash_py
    delta_cl   = cl   - cl_py
    delta_std  = std  - std_py
    delta_itp  = itp  - itp_py

    bs_acc = ((delta_ca - delta_cash) - (delta_cl - delta_std - delta_itp) - dep) / avg_ta
    return float(bs_acc) if np.isfinite(bs_acc) else None


class AccrualAnomaly(BaseStrategy):
    """LONG low-BS-accrual decile / SHORT high-BS-accrual decile — annual May rebalance (Sloan 1996)."""

    id               = STRATEGY_ID
    name             = 'AccrualAnomaly'
    description      = ('Stocks with low balance-sheet accruals (high earnings quality) outperform '
                        'stocks with high accruals (low earnings quality); market underreacts to '
                        'the cash vs. non-cash components of earnings.')
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 252
    # Accrual anomaly is an all-weather factor; valid across all volatility regimes
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

        # Annual rebalance: fire only in May (after earnings season)
        latest_date = prices.index[-1]
        if hasattr(latest_date, 'month') and latest_date.month != 5:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        financials = (aux_data or {}).get('financials', {})
        if not financials:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # ── Step 1: compute BS_ACC for each ticker ────────────────────────────
        acc_map: dict[str, float] = {}
        for ticker in universe:
            if ticker not in prices.columns:
                continue
            fin = financials.get(ticker)
            if not fin:
                continue

            bs_acc = _compute_bs_acc(fin)
            if bs_acc is None:
                continue

            acc_map[ticker] = bs_acc

        if len(acc_map) < 20:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # ── Step 2: winsorize, rank, select deciles ───────────────────────────
        tickers = list(acc_map.keys())
        accruals = np.array([acc_map[t] for t in tickers], dtype=float)

        # Winsorize at 1%/99% (Sloan 1996 convention)
        lo, hi = np.percentile(accruals, 1), np.percentile(accruals, 99)
        accruals_w = np.clip(accruals, lo, hi)

        # Percentile rank: 0 = lowest accruals (LONG), 1 = highest (SHORT)
        ranks_pct = pd.Series(accruals_w).rank(pct=True).to_numpy()

        n = len(tickers)
        decile = max(n // 10, 1)
        decile = min(decile, self.MAX_SIGNALS // 2)

        sorted_idx = np.argsort(ranks_pct)
        long_idx  = sorted_idx[:decile]    # bottom decile — lowest accruals
        short_idx = sorted_idx[-decile:]   # top decile    — highest accruals

        per_pos = min(round(scale / max(decile, 1), 4), 0.05)

        signals: List[Signal] = []

        for idx in long_idx:
            t = tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'LONG', price, regime_state=regime_state)
            pct   = float(ranks_pct[idx])
            conf  = 'HIGH' if pct < 0.05 else ('MED' if pct < 0.10 else 'LOW')
            signals.append(Signal(
                ticker            = t,
                direction         = 'LONG',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = per_pos,
                confidence        = conf,
                signal_params     = {
                    'bs_acc':     round(float(acc_map[t]), 6),
                    'acc_pct':    round(pct, 4),
                    'strategy':   STRATEGY_ID,
                },
            ))

        for idx in short_idx:
            t = tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'SHORT', price, regime_state=regime_state)
            pct   = float(ranks_pct[idx])
            conf  = 'HIGH' if pct > 0.95 else ('MED' if pct > 0.90 else 'LOW')
            signals.append(Signal(
                ticker            = t,
                direction         = 'SHORT',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = per_pos,
                confidence        = conf,
                signal_params     = {
                    'bs_acc':     round(float(acc_map[t]), 6),
                    'acc_pct':    round(pct, 4),
                    'strategy':   STRATEGY_ID,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
