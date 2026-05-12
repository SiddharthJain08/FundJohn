"""Daily 23:55 ET — refresh strategy_state.next_fire_date from latest signal_pnl.

For each strategy:
  1. Pull last 30 closed trades from signal_pnl (rolling window).
  2. If >= 5 trades: avg_holding_days from live exit-time stats.
     Else: fall back to EXPECTED_HOLDING_PERIODS static dict.
     Else: bootstrap_daily (1.0).
  3. next_fire_date = last_fire_date + ceil(avg_holding_days).
  4. UPSERT strategy_state.

Posts a one-line summary to stdout (cron logs it).
"""
from __future__ import annotations
import math
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from execution.signal_cadence_gate import EXPECTED_HOLDING_PERIODS, BOOTSTRAP_DAILY_DAYS

MIN_TRADES_FOR_LIVE_STATS = 5


def _to_date(v):
    """Normalize date-like input to a `date`. Accepts date, datetime, or ISO str."""
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v).date()
        except ValueError:
            return None
    return None


def recompute_for_strategy(
    strategy_id: str,
    last_fire: date,
    closed_trades: list[dict],
    static_lookup: dict | None = None,
) -> dict:
    """Compute (avg_holding_days, next_fire_date, source) for one strategy.

    Pure function — no DB I/O. Caller persists.

    Args:
        strategy_id: Strategy identifier.
        last_fire: Last firing date for this strategy.
        closed_trades: List of closed trades with 'entry_ts' and 'exit_ts' keys.
        static_lookup: Optional static holding period override dictionary.

    Returns:
        Dictionary with keys:
            - avg_holding_days: float, average days held (>= 0.5)
            - next_fire_date: date, when strategy should fire next
            - source: str, one of 'live_signal_pnl', 'static_fallback', 'bootstrap_daily'
    """
    static_lookup = static_lookup if static_lookup is not None else EXPECTED_HOLDING_PERIODS

    # Try to compute from live history
    if len(closed_trades) >= MIN_TRADES_FOR_LIVE_STATS:
        deltas_days = []
        for t in closed_trades:
            entry = _to_date(t.get('entry_ts'))
            exit_ = _to_date(t.get('exit_ts'))
            if entry and exit_:
                delta = (exit_ - entry).days
                deltas_days.append(delta)

        if deltas_days:
            avg = max(0.5, sum(deltas_days) / len(deltas_days))
            source = 'live_signal_pnl'
        elif strategy_id in static_lookup:
            avg = float(static_lookup[strategy_id])
            source = 'static_fallback'
        else:
            avg = BOOTSTRAP_DAILY_DAYS
            source = 'bootstrap_daily'
    elif strategy_id in static_lookup:
        avg = float(static_lookup[strategy_id])
        source = 'static_fallback'
    else:
        avg = BOOTSTRAP_DAILY_DAYS
        source = 'bootstrap_daily'

    # Compute next_fire_date
    next_fire = (
        (last_fire + timedelta(days=max(1, math.ceil(avg))))
        if last_fire
        else None
    )
    return {
        'avg_holding_days': avg,
        'next_fire_date': next_fire,
        'source': source,
    }


def main():
    """Iterate over all strategies in strategy_state, refresh each, UPSERT."""
    import psycopg2
    import psycopg2.extras

    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        print('[cadence_recompute] ERROR: POSTGRES_URI not set')
        sys.exit(1)

    try:
        conn = psycopg2.connect(uri)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    except Exception as e:
        print(f'[cadence_recompute] ERROR connecting to DB: {e}')
        sys.exit(1)

    try:
        cur.execute('SELECT strategy_id, last_fire_date FROM strategy_state')
        states = list(cur.fetchall())
        print(f'[cadence_recompute] processing {len(states)} strategies')

        updated = 0
        for s in states:
            sid = s['strategy_id']

            # Fetch last 30 closed trades for this strategy.
            # Join execution_signals to get the entry date (signal_date).
            cur.execute(
                """
                SELECT
                    es.signal_date::date AS entry_ts,
                    sp.closed_at::date AS exit_ts
                FROM signal_pnl sp
                JOIN execution_signals es ON es.id = sp.signal_id
                WHERE sp.strategy_id = %s
                  AND sp.closed_at IS NOT NULL
                ORDER BY sp.closed_at DESC
                LIMIT 30
            """,
                (sid,),
            )
            closed = [dict(r) for r in cur.fetchall()]

            # Recompute cadence for this strategy
            result = recompute_for_strategy(
                sid, s['last_fire_date'] or date.today(), closed
            )

            # Upsert into strategy_state
            cur.execute(
                """
                UPDATE strategy_state
                SET avg_holding_days=%s,
                    next_fire_date=%s,
                    source=%s,
                    updated_at=NOW()
                WHERE strategy_id=%s
            """,
                (
                    result['avg_holding_days'],
                    result['next_fire_date'],
                    result['source'],
                    sid,
                ),
            )
            updated += 1

        conn.commit()
        print(f'[cadence_recompute] updated {updated} strategies')

    except Exception as e:
        conn.rollback()
        print(f'[cadence_recompute] ERROR during recompute: {e}')
        sys.exit(1)
    finally:
        cur.close()
        conn.close()


if __name__ == '__main__':
    main()
