"""Episode / window primitives for strategy authoring.

Vectorised, causal (bar i depends on bars <= i) and NaN-explicit names for
constructs that strategies otherwise hand-roll as state machines.  The
vocabulary follows TDengine's stream-window functions (STATE_WINDOW,
EVENT_WINDOW, STATECOUNT, STATEDURATION, SESSION); only the concepts are used,
no code is copied, and the semantics differ where stated below.

Two rules worth knowing:

* NaN never glues windows.  ``state_window`` gives NaN rows id 0 and, by
  default, a key that reappears after a NaN opens a NEW window (``A, NaN, A``
  -> ``1, 0, 2``).  Pass ``nan_breaks=False`` to bridge the gap.
* ``event_window`` defaults to ``same_bar_single=False``: a bar where both
  start and end are true starts a new episode (closing any open one).  This
  differs from TDengine's EVENT_WINDOW, where such a bar forms a one-row
  window.  Pass ``same_bar_single=True`` for end-wins (the bar is flat).

``close_unfinished=False`` is the only place the final bar is needed: an
episode still open on the last bar is dropped (zeroed).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _require_bool(s: pd.Series, name: str) -> pd.Series:
    if not isinstance(s, pd.Series):
        raise ValueError(f"{name} must be a pandas Series of bool")
    if s.dtype != bool:
        raise ValueError(f"{name} must be bool dtype with no NaN (got {s.dtype})")
    return s


def state_window(key: pd.Series, *, nan_breaks: bool = True) -> pd.Series:
    """Window id per maximal run of equal consecutive non-NaN keys (from 1); NaN -> 0."""
    notna = key.notna()
    if nan_breaks:
        prev = key.shift(1)
    else:
        prev = key.ffill().shift(1)
    new = notna & (prev.isna() | (key != prev))
    ids = new.cumsum().where(notna, 0)
    return ids.astype(int)


def statecount(cond: pd.Series) -> pd.Series:
    """Consecutive True bars ending at this bar; 0 where False."""
    cond = _require_bool(cond, "cond")
    grp = (~cond).cumsum()
    return cond.astype(int).groupby(grp).cumsum().astype(int)


def stateduration(cond: pd.Series) -> pd.Series:
    """Calendar days from the current True run's first bar to this bar; -1 where False."""
    cond = _require_bool(cond, "cond")
    if not isinstance(cond.index, pd.DatetimeIndex):
        raise ValueError("stateduration needs a DatetimeIndex")
    ts = pd.Series(cond.index, index=cond.index)
    run_start = ts.where(cond & ~cond.shift(1, fill_value=False)).ffill()
    days = (ts - run_start).dt.days
    return days.where(cond, -1).fillna(-1).astype(int)


def event_window(start: pd.Series, end: pd.Series, *, close_unfinished: bool = False,
                 same_bar_single: bool = False, start_true_for: int = 0,
                 end_true_for: int = 0) -> pd.Series:
    """Episode id per bar (0 = flat) for 'enter when start ... until end'.

    An id marks bars where the position is held after that bar's decision: the
    entry bar carries the id, the end bar carries 0.  A repeated start while
    open continues the episode; an end while flat is a no-op.
    """
    start = _require_bool(start, "start")
    end = _require_bool(end, "end")
    if not start.index.equals(end.index):
        raise ValueError("start and end must share an index")
    if start_true_for >= 2:
        start = statecount(start) >= start_true_for
    if end_true_for >= 2:
        end = statecount(end) >= end_true_for

    nan = pd.Series(np.nan, index=start.index)
    if same_bar_single:
        raw = nan.mask(start, 1.0).mask(end, 0.0)
    else:
        raw = nan.mask(end, 0.0).mask(start, 1.0)
    state = raw.ffill().fillna(0.0)
    prev_state = state.shift(1, fill_value=0.0)
    new = (state == 1.0) & ((prev_state == 0.0) | (start & end))
    ids = (new.cumsum() * state).astype(int)
    if not close_unfinished and len(ids) and ids.iloc[-1] > 0:
        ids = ids.where(ids != ids.iloc[-1], 0)
    return ids


def session_gaps(index: pd.DatetimeIndex, tol) -> pd.Series:
    """Group id from 1, incremented where the gap to the previous timestamp exceeds tol."""
    ts = pd.Series(index, index=index)
    gap = ts.diff() > pd.Timedelta(tol)
    return (gap.cumsum() + 1).astype(int)


def _scalar_or_pos(x, n: int) -> np.ndarray:
    if isinstance(x, (pd.Series, pd.Index, np.ndarray, list, tuple)):
        arr = np.asarray(x, dtype=object)
        if len(arr) != n:
            raise ValueError("per-bar argument must align positionally with price")
        return arr
    return np.full(n, x, dtype=object)


def episodes_to_trades(episode_ids, price, dates, *, regime=None, direction='LONG') -> list:
    """Turn episode ids into trade rows.

    Entry = first bar carrying id k; exit = bar after the last one carrying k,
    or the final bar of the data when k runs to the end.  ``direction`` and
    ``regime`` are scalars or sequences aligned positionally with ``price``,
    read at the entry bar.
    """
    ids = np.asarray(episode_ids, dtype=int)
    px = np.asarray(price, dtype=float)
    n = len(ids)
    if len(px) != n or len(dates) != n:
        raise ValueError("episode_ids, price and dates must have equal length")
    dirs = _scalar_or_pos(direction, n)
    regs = None if regime is None else _scalar_or_pos(regime, n)
    d = pd.DatetimeIndex(pd.to_datetime(np.asarray(dates)))
    rows = []
    seen = set()
    for i0 in range(n):
        k = ids[i0]
        if k <= 0 or k in seen:
            continue
        seen.add(k)
        last = int(np.flatnonzero(ids == k)[-1])
        ix = last + 1 if last + 1 < n else n - 1
        entry, exit_ = px[i0], px[ix]
        dr = dirs[i0]
        pnl = (exit_ - entry) / entry if dr == 'LONG' else (entry - exit_) / entry
        rows.append({
            'signal_date': d[i0].strftime('%Y-%m-%d'),
            'exit_date': d[ix].strftime('%Y-%m-%d'),
            'direction': dr,
            'entry_price': float(entry),
            'exit_price': float(exit_),
            'pnl': float(pnl),
            'regime_state': None if regs is None else regs[i0],
        })
    return rows
