"""open_book.py — per-bar stepper for exit-hook strategies (spec
docs/specs/2026-08-28-per-bar-exit-hook-spec.md §2).

Only strategies with `exit_hook=True` use this path; every other strategy
keeps unified_backtest.simulate_trade (walk-at-entry) byte-identical. For an
open trade on a bar the order is FIXED: intra-bar bracket (_bar_exit) →
strategy.should_exit at the close → time cap (hold_cap) / end_of_data.
Marks accumulate exactly as simulate_trade does (mark-to-close on interior
bars, adverse exit fill on the exit bar) so downstream MTM/tail stats need
no change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import sys

import pandas as pd

from backtest.unified_backtest import _bar_exit

HOOK_REASON_PREFIX = 'strategy_exit:'


@dataclass
class OpenTrade:
    ticker: str
    direction: int                 # +1 long / -1 short
    entry_date: pd.Timestamp       # fill date; bars <= entry_date are never stepped
    entry_price: float             # raw fill level (persisted as entry_price)
    entry_fill: float              # entry_price after adverse slippage
    stop_loss: float
    target_1: float
    hold_cap: int                  # min(signal hold_days, max_hold_days)
    entry_regime: str
    signal_params: dict
    slippage: float                # one-way fraction (bps / 1e4)
    holding_days: int = 0
    prev_mark: float = 0.0
    daily_marks: list = field(default_factory=list)

    def __post_init__(self):
        # The first interior mark is (close / prev_mark - 1); an unset
        # prev_mark must therefore be the fill, never 0.0. Callers that pass
        # prev_mark explicitly (unified_backtest, tests) are unaffected.
        if self.prev_mark == 0.0:
            self.prev_mark = self.entry_fill


def resolve_hold_cap(signal_params, max_hold_days: int) -> int:
    """Per-signal hold_days capped by the run's max_hold_days (spec §1)."""
    try:
        hd = int(float((signal_params or {}).get('hold_days')))
    except (TypeError, ValueError):
        return int(max_hold_days)
    if hd < 1:
        return int(max_hold_days)
    return min(hd, int(max_hold_days))


def _position_dict(t: OpenTrade) -> dict:
    return {
        'ticker':        t.ticker,
        'direction':     'LONG' if t.direction > 0 else 'SHORT',
        # spec §1: the ACTUAL FILL, mirroring live's mark_entry_price
        # precedence. t.entry_price (the signal level) is what gets PERSISTED
        # on the trade record; what the hook sees is what was paid.
        'entry_price':   t.entry_fill,
        'entry_date':    t.entry_date,
        'days_held':     t.holding_days,
        'stop_loss':     t.stop_loss,
        'target_1':      t.target_1,
        'signal_params': t.signal_params,
    }


def _close(t: OpenTrade, dt, exit_level: float, reason: str) -> dict:
    exit_fill = exit_level * (1.0 - t.direction * t.slippage)
    t.daily_marks.append((dt, t.direction * (exit_fill / t.prev_mark - 1.0)))
    pnl = t.direction * (exit_fill - t.entry_fill) / t.entry_fill
    return {
        'ticker':        t.ticker,
        'direction':     'long' if t.direction > 0 else 'short',
        'entry_date':    t.entry_date.date() if hasattr(t.entry_date, 'date') else t.entry_date,
        'entry_price':   t.entry_price,
        'exit_date':     dt.date() if hasattr(dt, 'date') else dt,
        'exit_price':    exit_fill,
        'exit_reason':   reason,
        'holding_days':  t.holding_days,
        'pnl_pct':       pnl,
        'entry_regime':  t.entry_regime,
        'signal_stop':   t.stop_loss,
        'signal_target': t.target_1,
        'daily_marks':   list(t.daily_marks),
    }


def advance_open_book(open_book: list, current_date, bars_by_ticker: dict,
                      prices_to_date: pd.DataFrame, regime_payload: dict, aux: dict,
                      instance, *, dt_priority: str, counters: dict) -> list:
    """Step every open trade through `current_date`'s bar. Mutates
    `open_book` (closed trades removed) and returns the closed trade dicts."""
    closed: list = []
    use_hook = bool(getattr(instance, 'exit_hook', False))
    still_open: list = []
    for t in open_book:
        if current_date <= t.entry_date:
            still_open.append(t)
            continue
        bars = bars_by_ticker.get(t.ticker)
        if bars is None or current_date not in bars.index:
            if bars is None or len(bars.index[bars.index > current_date]) == 0:
                # Mirrors simulate_trade's `bars_future.empty` case: the ticker has
                # no bar after entry at all (holding_days is necessarily 0 here — a
                # stepped bar that was the ticker's last already closed the trade as
                # end_of_data at its close). No slippage, no fabricated mark.
                closed.append({
                    'ticker':        t.ticker,
                    'direction':     'long' if t.direction > 0 else 'short',
                    'entry_date':    t.entry_date.date() if hasattr(t.entry_date, 'date') else t.entry_date,
                    'entry_price':   t.entry_price,
                    'exit_date':     t.entry_date.date() if hasattr(t.entry_date, 'date') else t.entry_date,
                    'exit_price':    t.entry_price,
                    'exit_reason':   'end_of_data',
                    'holding_days':  t.holding_days,
                    'pnl_pct':       0.0,
                    'entry_regime':  t.entry_regime,
                    'signal_stop':   t.stop_loss,
                    'signal_target': t.target_1,
                    'daily_marks':   list(t.daily_marks),
                })
            else:
                still_open.append(t)
            continue
        bar = bars.loc[current_date]
        high, low, close = float(bar['high']), float(bar['low']), float(bar['close'])
        t.holding_days += 1
        # 1. intra-bar bracket
        exit_level, reason = _bar_exit(t.direction, high, low, t.stop_loss, t.target_1, dt_priority)
        # 2. hook at the close
        if exit_level is None and use_hook:
            try:
                r = instance.should_exit(_position_dict(t), prices_to_date, regime_payload, aux)
            except Exception as e:  # spec §1: hold, count, log first
                r = None
                counters['hook_raised'] = counters.get('hook_raised', 0) + 1
                if 'first_hook_raise' not in counters:
                    counters['first_hook_raise'] = f'{type(e).__name__}: {e}'
                    print(f'[open_book] should_exit raised on {t.ticker} {current_date.date()}: '
                          f'{counters["first_hook_raise"]} — holding', file=sys.stderr)
            if r:
                exit_level, reason = close, f'{HOOK_REASON_PREFIX}{r}'
                counters['hook_exits'] = counters.get('hook_exits', 0) + 1
        # 3. time cap / end of data
        if exit_level is None:
            no_more_bars = len(bars.index[bars.index > current_date]) == 0
            if t.holding_days >= t.hold_cap:
                exit_level, reason = close, 'max_hold'
            elif no_more_bars:
                exit_level, reason = close, 'end_of_data'
        if exit_level is not None:
            closed.append(_close(t, current_date, exit_level, reason))
            continue
        t.daily_marks.append((current_date, t.direction * (close / t.prev_mark - 1.0)))
        t.prev_mark = close
        still_open.append(t)
    open_book[:] = still_open
    return closed
