"""
S_low_volatility_us_gk63.py — Garman-Klass range-vol variant of low_volatility_us.

Spec: docs/specs/2026-09-12-quantdinger-adoptions-spec.md §4 D4 (item 9).

Hypothesis: the low-volatility anomaly's ranking signal is the parent's
252-session close-to-close standard deviation, which throws away every
intraday bar. The Garman-Klass estimator

    sigma^2_GK(t) = 0.5 * ln(H_t / L_t)^2 - (2 ln 2 - 1) * ln(C_t / O_t)^2

uses the whole bar and is roughly 7x more efficient per observation than
close-to-close, so a 63-session GK window carries about as much information as
the parent's 252-session close window while responding four times faster to a
regime change in a name's realised risk.

Same universe, same rebalance cadence (daily-driven, decile-selected), same
decile fraction, same house ATR brackets as the parent — the ONLY change is the
ranking statistic, so the fleet gates measure the estimator, not a new strategy.

Data: close panel (engine) + self-loaded OPEN/HIGH/LOW/CLOSE panels from
prices.parquet via _extra_panels.load_wide — the established pattern for a
strategy needing extra master-parquet columns (see S_overnight_intraday_tug_of_war
and oxford_crabel.basket_ohlc). Point-in-time: every self-loaded panel is
sliced .loc[:asof] with asof = prices.index[-1] before anything is computed,
which is _extra_panels' documented caller contract.
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

try:
    from strategies.implementations._extra_panels import load_wide
except ImportError:  # direct-file import fallback (validate harness)
    from _extra_panels import load_wide

__all__ = ['LowVolatilityUSGK63', 'garman_klass_variance']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_low_volatility_us_gk63'

_GK_C = 2.0 * np.log(2.0) - 1.0


def garman_klass_variance(open_: pd.DataFrame, high: pd.DataFrame,
                           low: pd.DataFrame, close: pd.DataFrame) -> pd.DataFrame:
    """Per-bar Garman-Klass variance: 0.5*ln(H/L)^2 - (2ln2-1)*ln(C/O)^2.

    Returns a frame aligned with the inputs; NaN wherever any leg is missing or
    non-positive (log of <= 0 is undefined — a zero/negative price is bad data,
    not a zero-variance bar)."""
    valid = (open_ > 0) & (high > 0) & (low > 0) & (close > 0)
    hl = np.log(high.where(valid) / low.where(valid))
    co = np.log(close.where(valid) / open_.where(valid))
    return 0.5 * hl ** 2 - _GK_C * co ** 2


class LowVolatilityUSGK63(BaseStrategy):
    """Rank asc by mean 63-session Garman-Klass variance; LONG the lowest decile."""

    id          = STRATEGY_ID
    name        = 'LowVolatilityUSGK63'
    description = ('Garman-Klass range-vol variant of low_volatility_us: rank asc by mean '
                   '63-session Garman-Klass variance; LONG the lowest-variance decile, equal-weight')
    tier        = 2

    # NEUTRAL expands to LOW_VOL + TRANSITIONING via base.py synonym resolution —
    # identical to the parent's declaration.
    active_in_regimes = ['LOW_VOL', 'NEUTRAL', 'TRANSITIONING']

    # 63 GK bars + a pad, so the prescreen's min_lookback-aware floor loads
    # enough history (factor_prescreen.run_prescreen reads this attribute).
    min_lookback = 70

    GK_WINDOW   = 63
    MIN_VALID   = 45          # usable GK bars required inside the window
    DECILE_FRAC = 0.10
    MIN_TICKERS = 10
    DATE_FLOOR  = '2021-01-01'

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        available = [t for t in universe if t in prices.columns]
        if len(available) < self.MIN_TICKERS:
            print('[debug] signals=0', file=sys.stderr)
            return []
        if len(prices) < self.GK_WINDOW + 1:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── Self-load OHLC, POINT-IN-TIME ─────────────────────────────────────
        # asof is the signal bar; .loc[:asof] before .tail() is _extra_panels'
        # documented caller contract and is what makes this look-ahead-safe.
        #
        # Key load_wide by the STABLE full close-panel column set
        # (prices.columns), not by `available` (universe intersected with
        # prices.columns) — `available` tracks the per-bar `universe` arg,
        # which a backtest's point-in-time resolver can vary every bar
        # (unified_backtest.py's bar_universe = resolver.resolve(...)). A
        # bar-varying ticker tuple would defeat load_wide's cache key on
        # every call for a DAILY-cadence strategy (4 chunked full-parquet
        # reads/bar; _extra_panels.py:125-131 documents ~3s/bar for exactly
        # this pattern). Mirrors liquid_pool's own precedent for the same
        # reason. `cols` below still restricts the result to `available`, so
        # this changes nothing about what gets ranked — only the cache key.
        asof = prices.index[-1]
        panels = {}
        for field in ('open', 'high', 'low', 'close'):
            w = load_wide(field, list(prices.columns), date_floor=self.DATE_FLOOR)
            if w is None or w.empty:
                print('[debug] signals=0', file=sys.stderr)
                return []
            panels[field] = w.loc[:asof].tail(self.GK_WINDOW)

        cols = None
        for w in panels.values():
            cols = set(w.columns) if cols is None else (cols & set(w.columns))
        cols = sorted(c for c in (cols or set()) if c in available)
        if len(cols) < self.MIN_TICKERS:
            print('[debug] signals=0', file=sys.stderr)
            return []

        idx = panels['close'].index
        for field in ('open', 'high', 'low'):
            idx = idx.intersection(panels[field].index)
        if len(idx) < self.MIN_VALID:
            print('[debug] signals=0', file=sys.stderr)
            return []

        o = panels['open'].loc[idx, cols].astype('float64')
        h = panels['high'].loc[idx, cols].astype('float64')
        low_ = panels['low'].loc[idx, cols].astype('float64')
        c = panels['close'].loc[idx, cols].astype('float64')

        gk = garman_klass_variance(o, h, low_, c)
        counts = gk.notna().sum()
        gk_mean = gk.mean(skipna=True)
        gk_mean = gk_mean[counts >= self.MIN_VALID].dropna()
        gk_mean = gk_mean[gk_mean > 0]
        if gk_mean.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # Lowest-variance decile, capped at MAX_SIGNALS — parent's selection rule.
        n_select = max(1, int(len(gk_mean) * self.DECILE_FRAC))
        selected = gk_mean.nsmallest(min(n_select, self.MAX_SIGNALS))

        latest = prices[list(selected.index)].ffill().iloc[-1]
        decile_median = float(selected.median())
        base_weight = round(1.0 / len(selected), 6)

        signals: List[Signal] = []
        for ticker in selected.index:
            price = float(latest[ticker]) if ticker in latest.index else float('nan')
            if not price or pd.isna(price) or price <= 0:
                continue

            gk_t = float(selected[ticker])
            confidence = 'HIGH' if gk_t <= decile_median else 'MED'

            stops = self.compute_stops_and_targets(
                prices_series=prices[ticker].dropna(),
                direction='LONG',
                current_price=price,
                regime_state=regime_state,
            )

            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=round(price, 4),
                stop_loss=round(stops['stop'], 4),
                target_1=round(stops['t1'], 4),
                target_2=round(stops['t2'], 4),
                target_3=round(stops['t3'], 4),
                position_size_pct=round(base_weight * scale, 6),
                confidence=confidence,
                signal_params={
                    'gk_var_63d':         round(gk_t, 10),
                    'gk_vol_ann':         round(float(np.sqrt(max(gk_t, 0.0) * 252.0)), 6),
                    'gk_bars_used':       int(counts.get(ticker, 0)),
                    'universe_gk_pctile': round(float((gk_mean < gk_t).sum()) / len(gk_mean), 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
