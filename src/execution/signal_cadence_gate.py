"""Per-strategy firing cadence based on average holding period.

Prevents position-stacking on long-horizon strategies: a strategy that
holds for ~2.2 days fires every ~3 days instead of every weekday.

Sources of avg_holding_days, in priority:
  1. Live signal_pnl exit-time stats (>= 5 closed trades)
  2. Static EXPECTED_HOLDING_PERIODS lookup
  3. Bootstrap daily (1-day cadence)

The gate is invoked by regime_blended_sizer.size_positions() on each
10am cycle. Skipped signals are written to cadence_skips for forensic
review and surfaced in the daily digest.
"""
from __future__ import annotations
import math
from datetime import date, timedelta
from typing import Iterable

# Static fallback for strategies with insufficient live history.
# Mirrors trade_handoff_builder.EXPECTED_HOLDING_PERIODS pattern.
# Populated in Task 14 when integrating with live data.
EXPECTED_HOLDING_PERIODS: dict[str, float] = {}
BOOTSTRAP_DAILY_DAYS = 1.0

def compute_next_fire_date(last: date, avg_holding_days: float) -> date:
    days = max(1, math.ceil(avg_holding_days)) if avg_holding_days > 0 else 1
    return last + timedelta(days=days)

def filter_by_cadence(
    signals: list[dict],
    strategy_state: dict[str, dict],
    today: date,
) -> tuple[list[dict], list[dict]]:
    """Return (passed_signals, skipped_records).

    skipped records have shape {strategy_id, ticker, reason, context}.
    """
    passed = []
    skipped = []
    for sig in signals:
        sid = sig['strategy_id']
        st = strategy_state.get(sid)
        if st is None:
            # Unknown strategy → bootstrap daily (always pass).
            passed.append(sig)
            continue
        next_fire = st.get('next_fire_date')
        if next_fire is None or today >= next_fire:
            passed.append(sig)
        else:
            skipped.append({
                'strategy_id': sid,
                'ticker': sig.get('ticker'),
                'reason': f'cadence_pending_until_{next_fire.isoformat()}',
                'context': {'last_fire_date': st.get('last_fire_date'),
                            'avg_holding_days': st.get('avg_holding_days'),
                            'source': st.get('source')},
            })
    return passed, skipped

def advance_last_fire(strategy_state: dict[str, dict], strategy_ids: Iterable[str], today: date) -> None:
    """Mutate strategy_state in-place to set last_fire_date=today for given strategies."""
    for sid in strategy_ids:
        if sid in strategy_state:
            strategy_state[sid]['last_fire_date'] = today
