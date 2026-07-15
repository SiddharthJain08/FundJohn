"""
_extra_panels.py — shared point-in-time loader for extra prices.parquet columns.

Strategies receive only a wide CLOSE panel via generate_signals(prices, ...).
Some edges need open/volume/vwap/transactions. The established pattern for
self-loading extra master-parquet columns (see oxford_crabel.basket_ohlc) is a
module-cached, column-pruned, ticker-filtered read sliced to the signal date.

This helper centralizes that pattern with a hard memory guardrail for the
2-core / 8GB VPS: chunked ticker reads (never the whole 8.8M-row file), a
date floor, and float32 wide pivots (~4 bytes/cell). A 500-ticker × 5-year
panel is ~7 MB resident.

CRITICAL point-in-time rule: callers must slice the returned panel with
`.loc[:asof]` where asof = prices.index[-1] before computing anything.
"""
from __future__ import annotations

import os
from typing import Dict, List, Tuple

import pandas as pd

_ROOT = os.path.join(os.path.dirname(__file__), '..', '..', '..')
PRICES_PARQUET = os.path.abspath(os.path.join(_ROOT, 'data', 'master', 'prices.parquet'))

# {(field, date_floor, tickers_key): wide DataFrame}
_CACHE: Dict[Tuple, pd.DataFrame] = {}

_CHUNK = 400          # tickers per parquet read — bounds transient memory
_VALID_FIELDS = {'open', 'high', 'low', 'close', 'volume', 'vwap', 'transactions'}


def load_wide(field: str, tickers: List[str], date_floor: str = '2021-01-01') -> pd.DataFrame:
    """Return a wide date × ticker panel of `field` for `tickers`, rows >= date_floor.

    Cached per (field, date_floor, tickers). Returns an EMPTY DataFrame on any
    failure — callers must handle missing data gracefully (never raise).
    """
    if field not in _VALID_FIELDS:
        return pd.DataFrame()
    tickers = sorted(set(t for t in tickers if isinstance(t, str)))
    if not tickers:
        return pd.DataFrame()
    key = (field, date_floor, hash(tuple(tickers)))
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    if not os.path.isfile(PRICES_PARQUET):
        return pd.DataFrame()
    frames = []
    try:
        for i in range(0, len(tickers), _CHUNK):
            chunk = tickers[i:i + _CHUNK]
            df = pd.read_parquet(
                PRICES_PARQUET,
                columns=['ticker', 'date', field],
                filters=[('ticker', 'in', chunk), ('date', '>=', date_floor)],
            )
            if df.empty:
                continue
            df['date'] = pd.to_datetime(df['date'])
            wide = df.pivot_table(index='date', columns='ticker', values=field,
                                  aggfunc='last').astype('float32')
            frames.append(wide)
        if not frames:
            out = pd.DataFrame()
        else:
            out = pd.concat(frames, axis=1).sort_index()
            out = out.loc[:, ~out.columns.duplicated()]
    except Exception:
        out = pd.DataFrame()
    # Keep the cache tiny: this is called with a stable pool per process.
    if len(_CACHE) > 6:
        _CACHE.clear()
    _CACHE[key] = out
    return out


def liquid_pool(prices: pd.DataFrame, max_names: int = 500,
                min_price: float = 5.0, lookback: int = 60,
                date_floor: str = '2021-01-01') -> List[str]:
    """Deterministic liquid-ticker pool from the close panel + self-loaded volume.

    Backtests without a resolver pass the FULL ~6k-ticker panel as universe, so
    cross-sectional strategies restrict internally: candidates = tickers with
    >=90% non-NaN closes over `lookback` and last price >= min_price, ranked by
    trailing `lookback`-day median dollar volume; top `max_names` kept.
    Point-in-time: uses only rows of `prices` (already sliced to the signal
    date by the engine) and volume rows <= prices.index[-1].
    """
    if prices is None or prices.empty:
        return []
    # Only the trailing `lookback` masked rows matter — bound per-call work by
    # pre-slicing a raw tail before the full-panel mask/copy. The union
    # calendar adds at most 2 weekend rows per 5 weekdays, so 2x lookback of
    # raw rows always yields >= lookback equity rows when the full panel would.
    max_raw = 2 * lookback + 30
    if len(prices) > max_raw:
        prices = prices.iloc[-max_raw:]
    # Union-calendar safety: without OPENCLAW_EQUITY_TRADING_CALENDAR the panel
    # carries weekend/holiday rows contributed solely by crypto/forex tickers,
    # where every equity is NaN — those rows would fail the completeness filter
    # for ALL equities and make the last row price-less. Mirror
    # lib.price_panel.apply_equity_calendar: keep rows with >=1 non-NaN equity.
    eq_cols = [c for c in prices.columns
               if not str(c).startswith('^') and '-USD' not in str(c)
               and '=F' not in str(c) and '=X' not in str(c)]
    if eq_cols:
        prices = prices.loc[prices[eq_cols].notna().any(axis=1).values]
    if len(prices) < lookback + 5:
        return []
    tail = prices.iloc[-lookback:]
    last = tail.iloc[-1]
    ok = tail.notna().mean() >= 0.90
    cand = [t for t in prices.columns
            if ok.get(t, False)
            and pd.notna(last.get(t))
            and float(last.get(t)) >= min_price
            and not t.startswith('^') and '-USD' not in t and '=F' not in t]
    if not cand:
        return []
    if len(cand) <= max_names:
        return sorted(cand)
    # Load volume for the STABLE full column set, not the bar-varying `cand`
    # list — a stable ticker tuple makes the load_wide cache hit after the
    # first call (cand changes bar-to-bar in backtests, which forced a full
    # chunked parquet re-read EVERY bar: ~3s/bar). The superset panel yields
    # identical rankings: cand tickers without volume data drop out of
    # `common` either way.
    vol = load_wide('volume', list(prices.columns), date_floor=date_floor)
    if vol.empty:
        # No volume data — fall back to completeness-then-name determinism.
        return sorted(cand)[:max_names]
    asof = prices.index[-1]
    v = vol.loc[:asof].iloc[-lookback:]
    common = [t for t in cand if t in v.columns]
    px = tail[common].astype('float64')
    dollar = (px * v[common].astype('float64')).median()
    top = dollar.dropna().sort_values(ascending=False).head(max_names)
    return sorted(top.index.tolist())
