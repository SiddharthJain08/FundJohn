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
ranking statistic, so the fleet gates measure the estimator, not a new
strategy. `DATE_FLOOR = '2016-01-01'` matches the fleet's shared backtest
window: `unified_backtest.DEFAULT_START_DATE = '2016-04-11'` (the earliest
`historical_regimes` row) is the actual start of any backtest run, and the
per-bar `prices_to_date = close_wide.loc[:current_date]` the engine builds is
NOT itself bounded at `start_dt` (unified_backtest.py:993) — it carries the
close panel's full available history. The 63-equity-bar window ending exactly
on `DEFAULT_START_DATE` (2016-04-11) begins 2016-01-14 (bdate_range check),
safely inside `DATE_FLOOR` — so this variant's self-loaded OHL panels already
have a full window on the very first backtest bar, exactly like the parent:
the two are compared over the SAME span from day one, not a shorter one,
which is the whole point of the D4 experiment (an earlier `2021-01-01` floor
would instead have started this variant's own coverage ~5 years later than
the parent's).

Data: close panel (engine) + self-loaded OPEN/HIGH/LOW panels from
prices.parquet via _extra_panels.load_wide (CLOSE is taken from the engine's
own `prices` panel, not re-loaded — see the loader below); the established
pattern for a strategy needing extra master-parquet columns (see
S_overnight_intraday_tug_of_war and oxford_crabel.basket_ohlc). Point-in-time:
every self-loaded panel is reindexed to the engine's own calendar
(`prices.index[prices.index <= asof]`, asof = prices.index[-1]) before the
63-bar `.tail()` — both to stay look-ahead-safe (_extra_panels' documented
caller contract) and to keep the window on the EQUITY calendar the backtest
actually handed us, regardless of what calendar `load_wide` itself returns
(it is loaded with equity-only tickers via `lib.price_panel.is_equity_ticker`,
so a 7-day-market ticker such as BTC-USD in the universe can never dilute the
window with weekend/holiday rows that would starve it below `MIN_VALID`).
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from lib.price_panel import is_equity_ticker

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

    Returns a frame aligned with the inputs; NaN wherever a bar is not a
    usable OHLC bar (fix round 1, R4) — a bar is valid only if:
      - all four legs are finite and strictly positive (log of <= 0 is
        undefined — a zero/negative or missing price is bad data);
      - H >= max(O, C) and L <= min(O, C) (the high/low must actually bound
        the open/close, or the bar is internally inconsistent — e.g. a bad
        print with O above H); and
      - H > L strictly (a zero-range O=H=L=C stale/illiquid bar is NaN, not a
        legitimate zero-variance bar — otherwise a stale, non-trading name
        would look like the quietest name in the universe and win the
        low-vol ranking on bad data)."""
    finite   = np.isfinite(open_) & np.isfinite(high) & np.isfinite(low) & np.isfinite(close)
    positive = (open_ > 0) & (high > 0) & (low > 0) & (close > 0)
    bounded  = (high >= open_) & (high >= close) & (low <= open_) & (low <= close)
    ranged   = high > low
    valid = finite & positive & bounded & ranged
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
    # Chosen so GK_WINDOW=63 equity bars are already available before
    # unified_backtest.DEFAULT_START_DATE = '2016-04-11', so the parent and
    # this variant share the same fleet backtest window from the first bar
    # (see module docstring) — fix round 1, R3.
    DATE_FLOOR  = '2016-01-01'

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

        # ── Self-load OHL, POINT-IN-TIME ──────────────────────────────────────
        # asof is the signal bar. Fix round 1:
        #
        # R1(a) — load_wide is keyed by EQUITY-ONLY tickers
        # (lib.price_panel.is_equity_ticker), not the full `prices.columns`.
        # `apply_equity_calendar` only ever drops ROWS, never columns, so
        # requesting a 7-day-market ticker such as BTC-USD would pull load_wide's
        # OWN returned panel onto the union calendar (weekend/holiday rows that
        # exist only because a crypto ticker trades that day) — a `.tail(63)`
        # over that union index would then span far more than 63 equity
        # sessions and could starve MIN_VALID. `equity_cols` is still derived
        # entirely from `prices.columns` (not from `available`, which tracks
        # the per-bar `universe` arg a backtest's point-in-time resolver can
        # vary every bar — unified_backtest.py's bar_universe =
        # resolver.resolve(...)), so the load_wide cache key stays BAR-STABLE
        # for this daily-cadence strategy (4 chunked full-parquet reads/bar
        # otherwise; _extra_panels.py:125-131 documents ~3s/bar for exactly
        # this pattern; mirrors liquid_pool's own precedent).
        #
        # R1(b) — belt-and-suspenders regardless of what calendar load_wide's
        # own OHLC panel ends up on: reindex to the EQUITY calendar the
        # backtest actually handed us (`prices.index`, filtered to <= asof)
        # BEFORE the tail, so the window is always exactly GK_WINDOW rows of
        # `prices.index`, never a union-calendar span.
        #
        # R2 — only OPEN/HIGH/LOW are self-loaded; CLOSE comes from the
        # engine's own `prices` panel below (same source, already PIT-sliced,
        # and what the parent ranks on) instead of a fourth load_wide call —
        # cuts self-loaded panel memory by a quarter.
        asof = prices.index[-1]
        equity_cols = [t for t in prices.columns if is_equity_ticker(t)]
        equity_calendar = prices.index[prices.index <= asof]

        panels = {}
        for field in ('open', 'high', 'low'):
            w = load_wide(field, equity_cols, date_floor=self.DATE_FLOOR)
            if w is None or w.empty:
                print('[debug] signals=0', file=sys.stderr)
                return []
            # reindex straight onto the LAST GK_WINDOW equity dates — identical to
            # reindex(full calendar).tail(GK_WINDOW) but never materialises a
            # full-history copy per field (≈64 MiB transient each at 2016+).
            panels[field] = w.reindex(equity_calendar[-self.GK_WINDOW:])

        cols = None
        for w in panels.values():
            cols = set(w.columns) if cols is None else (cols & set(w.columns))
        cols = sorted(c for c in (cols or set()) if c in available)
        if len(cols) < self.MIN_TICKERS:
            print('[debug] signals=0', file=sys.stderr)
            return []

        idx = panels['open'].index
        for field in ('high', 'low'):
            idx = idx.intersection(panels[field].index)
        idx = idx.intersection(prices.index)
        if len(idx) < self.MIN_VALID:
            print('[debug] signals=0', file=sys.stderr)
            return []

        o = panels['open'].loc[idx, cols].astype('float64')
        h = panels['high'].loc[idx, cols].astype('float64')
        low_ = panels['low'].loc[idx, cols].astype('float64')
        c = prices.loc[idx, cols].astype('float64')

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
