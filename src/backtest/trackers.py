"""Tracker layer for ``unified_backtest._per_bar_simulate`` (six-repo D3a).

Pure module (stdlib + pandas/math only).  Trackers observe the loop through a
``BacktestContext`` and fold their diagnostics into one ``info`` dict at the
end; ``FinishedBacktest`` raised from ``handle`` aborts the loop cleanly.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

import pandas as pd


class FinishedBacktest(Exception):
    """Raised by a tracker to stop the loop; ``str()`` is the reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class InconsistentBacktest(FinishedBacktest):
    """A tracker found the run internally inconsistent (e.g. non-finite mark)."""


@dataclass
class BacktestContext:
    instance: object
    close_wide: pd.DataFrame
    strategy_id: Optional[str]
    oos_dates: pd.DatetimeIndex
    trades: list
    counters: dict
    current_date: Optional[pd.Timestamp] = None
    started: float = 0.0


class Tracker(Protocol):
    name: str

    def initialize(self, ctx: BacktestContext) -> None: ...
    def handle(self, ctx: BacktestContext, bar_idx: int) -> None: ...
    def finalize(self, info: dict) -> None: ...


class _Base:
    name = 'tracker'

    def initialize(self, ctx: BacktestContext) -> None:
        self._ctx = ctx

    def handle(self, ctx: BacktestContext, bar_idx: int) -> None:
        return None

    def finalize(self, info: dict) -> None:
        return None


class TrackerCollection:
    def __init__(self, trackers: list):
        self._trackers = list(trackers)

    def initialize(self, ctx: BacktestContext) -> None:
        ctx.started = time.monotonic()
        for t in self._trackers:
            t.initialize(ctx)

    def handle(self, ctx: BacktestContext, bar_idx: int) -> None:
        for t in self._trackers:
            t.handle(ctx, bar_idx)

    def finalize(self) -> dict:
        info: dict = {}
        for t in self._trackers:
            t.finalize(info)
        return info

    def names(self) -> list:
        return [t.name for t in self._trackers]


class ExitCensusTracker(_Base):
    name = 'exit_census'

    def __init__(self, census_fn: Callable[[list], dict]):
        self._fn = census_fn

    def finalize(self, info: dict) -> None:
        info['exit_reasons'] = self._fn(self._ctx.trades)


class HookTracker(_Base):
    name = 'hook'

    def finalize(self, info: dict) -> None:
        c = self._ctx.counters
        info['hook_exits'] = c.get('hook_exits', 0)
        info['hook_raised'] = c.get('hook_raised', 0)
        info['first_hook_raise'] = c.get('first_hook_raise')


class IntradayBarTracker(_Base):
    name = 'intraday_bars'

    def finalize(self, info: dict) -> None:
        c = self._ctx.counters
        for k in ('intraday_trades', 'intraday_no_bars', 'intraday_missing_entry_bar'):
            info[k] = c.get(k, 0)


class MaxRuntimeTracker(_Base):
    name = 'max_runtime'

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic):
        self._seconds = seconds
        self._clock = clock
        self._elapsed = 0.0

    def handle(self, ctx: BacktestContext, bar_idx: int) -> None:
        e = self._clock() - ctx.started
        self._elapsed = e
        if e > self._seconds:
            when = ctx.current_date.date() if ctx.current_date is not None else 'drain'
            raise FinishedBacktest(
                f'max_runtime: elapsed {e:.1f}s > budget {self._seconds:g}s '
                f'at bar {bar_idx}/{len(ctx.oos_dates)} ({when})')

    def finalize(self, info: dict) -> None:
        info['elapsed_s'] = self._elapsed


def _nonfinite(x) -> bool:
    try:
        return not math.isfinite(float(x))
    except (TypeError, ValueError):
        return True


class ConsistencyTracker(_Base):
    name = 'consistency'

    def __init__(self):
        self._seen = 0

    def handle(self, ctx: BacktestContext, bar_idx: int) -> None:
        trades = ctx.trades
        for tr in trades[self._seen:]:
            bad = None
            if _nonfinite(tr.get('pnl_pct')):
                bad = 'pnl_pct'
            else:
                for i, m in enumerate(tr.get('daily_marks') or ()):
                    if _nonfinite(m[1]):
                        bad = f'daily_marks[{i}]'
                        break
            if bad:
                raise InconsistentBacktest(
                    f'nonfinite: {tr.get("ticker")} {tr.get("entry_date")} {bad}')
        self._seen = len(trades)

    def finalize(self, info: dict) -> None:
        info['consistency_checked'] = self._seen
