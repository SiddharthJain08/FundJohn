#!/usr/bin/env python3
"""Reconstruct per-strategy daily return series.

signal_pnl.unrealized_pnl_pct is a CUMULATIVE-since-entry level; we difference
consecutive marks per signal to a daily delta, then aggregate equal-weight across
the strategy's open signals. Backtest series come from strategy_backtest_trades via
unified_backtest._portfolio_daily_returns. Persisted to strategy_daily_returns.
"""
from __future__ import annotations

import os


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


def _db():
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv()
    return psycopg2.connect(os.environ.get('DATABASE_URL')
                            or os.environ.get('POSTGRES_URI')
                            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def _live_marks_by_strategy(window_days: int):
    """({strategy_id: {signal_id: {date: daily_delta}}}, {strategy_id: {date: regime_state}})
    from differenced signal_pnl marks."""
    sql = """
        SELECT es.strategy_id, sp.signal_id::text, sp.pnl_date::text,
               sp.unrealized_pnl_pct::float, sp.realized_pnl_pct::float,
               es.regime_state
          FROM signal_pnl sp
          JOIN execution_signals es ON es.id = sp.signal_id
         WHERE sp.pnl_date >= CURRENT_DATE - (%s::int * INTERVAL '1 day')
         ORDER BY es.strategy_id, sp.signal_id, sp.pnl_date
    """
    raw: dict[str, dict[str, list]] = {}
    regime_of: dict[str, dict[str, str]] = {}
    with _db() as conn, conn.cursor() as cur:
        cur.execute(sql, (window_days,))
        for sid, sig, d, unreal, real, regime in cur.fetchall():
            raw.setdefault(sid, {}).setdefault(sig, []).append((d, unreal, real))
            regime_of.setdefault(sid, {})[d] = regime
    out: dict[str, dict[str, dict[str, float]]] = {}
    for sid, sigs in raw.items():
        out[sid] = {sig: difference_signal_marks(marks) for sig, marks in sigs.items()}
    return out, regime_of


def rebuild_daily_returns(window_days: int = 180, trigger: str = 'manual') -> int:
    """Reconstruct live (differenced) per-strategy daily returns and upsert. Returns row count."""
    live, regime_of = _live_marks_by_strategy(window_days)
    rows = []
    for sid, per_signal in live.items():
        agg = aggregate_strategy_daily(per_signal)
        for d, ret in agg.items():
            rows.append((sid, d, ret, regime_of.get(sid, {}).get(d), 'live'))
    if not rows:
        return 0
    with _db() as conn, conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO strategy_daily_returns
                 (strategy_id, ret_date, daily_return_pct, regime_state, source)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (strategy_id, ret_date, source) DO UPDATE
                 SET daily_return_pct = EXCLUDED.daily_return_pct,
                     regime_state = EXCLUDED.regime_state,
                     computed_at = NOW()""",
            rows)
        conn.commit()
    return len(rows)
