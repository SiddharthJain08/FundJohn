#!/usr/bin/env python3
"""Reconstruct per-strategy daily return series.

signal_pnl.unrealized_pnl_pct is a CUMULATIVE-since-entry level; we difference
consecutive marks per signal to a daily delta, then aggregate equal-weight across
the strategy's open signals. Backtest series come from strategy_backtest_trades via
unified_backtest._portfolio_daily_returns. Persisted to strategy_daily_returns.
"""
from __future__ import annotations

import os
from typing import Optional


def difference_signal_marks(marks: list[tuple]) -> dict[str, float]:
    """marks: ordered list of (date_str, cumulative_unrealized_pct, realized_or_none).

    Returns {date_str: daily_delta}. First day = level from 0. A day with a non-None
    realized value is the close day: delta = realized - prior cumulative.
    """
    out: dict[str, float] = {}
    prev = 0.0
    for date_str, cum, realized in marks:
        cum = float(cum) if cum is not None else prev
        if realized is not None:
            out[date_str] = float(realized) - prev
            prev = float(realized)
        else:
            out[date_str] = cum - prev
            prev = cum
    return out


def aggregate_strategy_daily(per_signal: dict[str, dict[str, float]]) -> dict[str, float]:
    """Equal-weight mean of per-signal daily deltas across signals present each date."""
    by_date: dict[str, list[float]] = {}
    for _sig, series in per_signal.items():
        for d, v in series.items():
            by_date.setdefault(d, []).append(v)
    return {d: sum(vs) / len(vs) for d, vs in by_date.items() if vs}
