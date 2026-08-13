"""
S_ast_rd_expenditures_and_stock_returns — R&D Expenditures and Stock Returns
Source: https://quantpedia.com/strategies/rd-expenditures-and-stock-returns/

LONG top-quintile / SHORT bottom-quintile stocks ranked by 5-year
decay-weighted R&D-to-MarketCap ratio. Annual rebalance end of April.

Hypothesis: Firms with higher R&D spending relative to market cap earn higher
future returns; long top quintile, short bottom quintile by 5-year
decay-weighted R&D-to-MarketCap ratio. (Chan, Lakonishok & Sougiannis 2001.)

Porting note: reference implementation is QuantConnect LEAN. Clean-room
re-implementation for OpenClaw BaseStrategy contract.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE
from src.strategies.universe_default import no_otc as universe_filter

__all__ = ['RDExpendituresAndStockReturns']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_ast_rd_expenditures_and_stock_returns'

# Decay weights: year 0 (most recent) = 1.0, year 4 (oldest) = 0.2
_RD_COEFS = [1.0, 0.8, 0.6, 0.4, 0.2]

# FMP / common field names for annual R&D (most recent first then prior years)
_RD_KEYS_BY_LAG = [
    # lag 0 — most recent fiscal year
    ['researchAndDevelopment', 'researchAndDevelopmentExpenses',
     'rdExpense', 'research_and_development'],
    # lag 1 — one year prior
    ['researchAndDevelopmentPY1', 'researchAndDevelopmentPY',
     'researchAndDevelopmentPriorYear', 'rdExpensePY1'],
    # lag 2
    ['researchAndDevelopmentPY2', 'rdExpensePY2'],
    # lag 3
    ['researchAndDevelopmentPY3', 'rdExpensePY3'],
    # lag 4
    ['researchAndDevelopmentPY4', 'rdExpensePY4'],
]


def _safe_float(v) -> float | None:
    try:
        f = float(v)
        return f if np.isfinite(f) and f >= 0 else None
    except (TypeError, ValueError):
        return None


def _get_rd_series(fin: dict) -> list[float | None]:
    """Return list of R&D values [lag0, lag1, ..., lag4]; None where unavailable."""
    result = []
    for lag_keys in _RD_KEYS_BY_LAG:
        val = None
        for key in lag_keys:
            v = fin.get(key)
            if v is not None:
                val = _safe_float(v)
                break
        result.append(val)
    return result


def _rd_score(rd_series: list[float | None], market_cap: float) -> float | None:
    """
    RD_score = Σ(coef_i * rd_i) / MarketCap  (Eq. 1, Chan et al. 2001)
    Only uses lags with available data; requires at least lag-0.
    """
    if market_cap is None or market_cap <= 0:
        return None
    if rd_series[0] is None:
        return None  # most recent year R&D required
    total = 0.0
    n_avail = 0
    for coef, rd in zip(_RD_COEFS, rd_series):
        if rd is not None:
            total += coef * rd
            n_avail += 1
    if n_avail == 0:
        return None
    score = total / market_cap
    return float(score) if np.isfinite(score) else None


class RDExpendituresAndStockReturns(BaseStrategy):
    """LONG top-quintile / SHORT bottom-quintile by 5-yr decay-weighted R&D/MarketCap (annual April rebalance)."""

    id               = STRATEGY_ID
    name             = 'RDExpendituresAndStockReturns'
    description      = ('Firms with higher R&D spending relative to market cap earn higher future '
                        'returns; long top quintile, short bottom quintile by 5-year '
                        'decay-weighted R&D-to-MarketCap ratio.')
    tier             = 2
    signal_frequency = 'annual'
    calendar_edge    = True   # window IS the signal; ports across regime flips (2026-08-13)
    min_lookback     = 252
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
            print('[debug] signals=0 (regime filter)', file=sys.stderr)
            return []

        # Annual rebalance: fire only at end of April
        last_date = pd.Timestamp(prices.index[-1])
        if last_date.month != 4:
            print('[debug] signals=0 (not April rebalance)', file=sys.stderr)
            return []

        financials = (aux_data or {}).get('financials', {})
        if not financials:
            print('[debug] signals=0 (no financials)', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # ── Step 1: compute R&D score per ticker ─────────────────────────────
        score_map: dict[str, float] = {}
        for ticker in universe:
            if ticker not in prices.columns:
                continue
            fin = financials.get(ticker)
            if not fin:
                continue

            market_cap = _safe_float(
                fin.get('marketCap') or fin.get('market_cap') or fin.get('mktCap')
            )
            if market_cap is None or market_cap <= 0:
                continue

            # Price filter: must be above $5
            ts = prices[ticker].dropna()
            if len(ts) < 5:
                continue
            current_price = float(ts.iloc[-1])
            if current_price < 5.0:
                continue

            rd_series = _get_rd_series(fin)
            score = _rd_score(rd_series, market_cap)
            if score is None:
                continue

            score_map[ticker] = score

        if len(score_map) < 20:
            print(f'[debug] signals=0 (only {len(score_map)} valid tickers with R&D data)', file=sys.stderr)
            return []

        # ── Step 2: rank by R&D score and select quintiles ───────────────────
        tickers   = list(score_map.keys())
        scores    = np.array([score_map[t] for t in tickers], dtype=float)
        ranks_pct = pd.Series(scores).rank(pct=True).to_numpy()  # 0=lowest, 1=highest

        n       = len(tickers)
        quintile = max(n // 5, 1)
        quintile = min(quintile, self.MAX_SIGNALS // 2)

        sorted_idx = np.argsort(ranks_pct)
        short_idx  = sorted_idx[:quintile]    # bottom quintile — lowest R&D score → SHORT
        long_idx   = sorted_idx[-quintile:]   # top quintile    — highest R&D score → LONG

        # Equal-weight within each leg
        per_pos = min(round(scale / max(quintile, 1), 4), 0.05)

        signals: List[Signal] = []

        for idx in long_idx:
            t = tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'LONG', price, regime_state=regime_state)
            pct   = float(ranks_pct[idx])
            conf  = 'HIGH' if pct >= 0.95 else ('MED' if pct >= 0.90 else 'LOW')
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
                    'rd_score':     round(float(score_map[t]), 8),
                    'rd_score_pct': round(pct, 4),
                    'strategy':     STRATEGY_ID,
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
            conf  = 'HIGH' if pct <= 0.05 else ('MED' if pct <= 0.10 else 'LOW')
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
                    'rd_score':     round(float(score_map[t]), 8),
                    'rd_score_pct': round(pct, 4),
                    'strategy':     STRATEGY_ID,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
