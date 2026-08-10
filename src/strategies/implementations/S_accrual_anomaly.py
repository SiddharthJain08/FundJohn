"""
S_accrual_anomaly — Accrual Anomaly (Sloan 1996 / Quantpedia)
Source: https://quantpedia.com/strategies/accrual-anomaly/

LONG bottom-decile balance-sheet accruals (highest earnings quality),
SHORT top-decile balance-sheet accruals (lowest earnings quality).
Annual rebalance in May after all companies publish annual earnings.

Full Sloan (1996): BS_ACC = [(ΔCA − ΔCash) − (ΔCL − ΔSTD − ΔITP) − Dep] / avg(TA).
Implemented here as the dominant ΔWC term, BS_ACC ≈ ΔWC_yoy / avg(TA), because the
financials panel carries working_capital + prior-year history but not the CA/Cash/CL
line items (see _compute_bs_acc docstring, 2026-08-10).
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
    """Balance-sheet accruals scaled by average total assets.

    Data-contract rewrite 2026-08-10 (Sunday code review: the original read
    '*PriorYear' sibling keys for CA/Cash/CL that no data source here ever
    provides — every ticker returned None and the strategy never fired). The
    financials panel does not carry the current-asset / cash / current-
    liability line items Sloan's full BS_ACC needs, but it does carry
    working_capital (= CA − CL) with genuine prior-year history served as
    workingCapitalPriorYear / totalAssetsPriorYear (self-relative to the
    ticker's latest filing — aux_data_loader._financials_slice and its
    engine.py twin). So this computes the dominant ΔWC term of Sloan (1996):

        BS_ACC ≈ ΔWC_yoy / avg(TotalAssets)

    omitting the ΔCash / ΔSTD / ΔITP corrections and depreciation. Returns
    None when the panel lacks prior-year history for the ticker (the ranking
    then simply excludes it — no fabricated zeros).
    """
    wc    = _float_or_none(fin.get('workingCapital'))
    wc_py = _float_or_none(fin.get('workingCapitalPriorYear'))
    ta    = _float_or_none(fin.get('totalAssets'))
    ta_py = _float_or_none(fin.get('totalAssetsPriorYear'))

    if wc is None or wc_py is None or ta is None:
        return None
    if ta_py is None:
        ta_py = ta

    avg_ta = (ta + ta_py) / 2.0
    if avg_ta <= 0.0:
        return None

    bs_acc = (wc - wc_py) / avg_ta
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

        # Annual May rebalance (after annual earnings publish): fire on the
        # FIRST bar inside May — once per year under daily AND monthly
        # sampling, instead of every May bar under daily iteration.
        latest_date = prices.index[-1]
        if hasattr(latest_date, 'month'):
            prev_date = prices.index[-2] if len(prices.index) >= 2 else None
            into_may = latest_date.month == 5 and (
                prev_date is None or not hasattr(prev_date, 'month')
                or prev_date.month != 5)
            if not into_may:
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
