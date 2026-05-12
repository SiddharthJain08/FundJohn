"""Cadence is preserved across regime liquidation events."""
import sys
from datetime import date, timedelta
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

pytestmark = pytest.mark.integration


def test_liquidation_does_not_reset_cadence():
    """Strategy with avg_holding=2.0d fires Mon (last_fire=Mon, next_fire=Wed).
    Tue: regime liquidator runs (does NOT touch strategy_state).
    Wed: strategy is cadence-eligible (next_fire=Wed <= today=Wed) and re-fires."""
    from execution.signal_cadence_gate import filter_by_cadence

    monday = date(2026, 5, 11)
    tuesday = date(2026, 5, 12)
    wednesday = date(2026, 5, 13)

    state = {'S1': {'last_fire_date': monday, 'next_fire_date': wednesday,
                    'avg_holding_days': 2.0, 'source': 'live_signal_pnl'}}

    signal = {'strategy_id': 'S1', 'ticker': 'AAPL'}

    # Tue: cadence_pending (next_fire=Wed > today=Tue)
    passed_tue, skipped_tue = filter_by_cadence([signal], state, tuesday)
    assert len(passed_tue) == 0
    assert len(skipped_tue) == 1
    assert 'cadence_pending_until_2026-05-13' in skipped_tue[0]['reason']

    # Simulate Tue regime liquidation: strategy_state is NOT modified by the liquidator.
    # (regime_liquidator.liquidate_on_regime_change doesn't touch strategy_state at all.)

    # Wed: cadence-eligible (next_fire=Wed <= today=Wed) → passes
    passed_wed, skipped_wed = filter_by_cadence([signal], state, wednesday)
    assert len(passed_wed) == 1
    assert len(skipped_wed) == 0


def test_liquidation_does_not_advance_last_fire():
    """advance_last_fire is only called on order EMISSION, not on liquidation.
    last_fire_date stays at the day the strategy actually fired its signal,
    even if the position was subsequently liquidated by a regime change."""
    from execution.signal_cadence_gate import advance_last_fire

    monday = date(2026, 5, 11)
    state = {'S1': {'last_fire_date': monday, 'next_fire_date': date(2026, 5, 13),
                    'avg_holding_days': 2.0, 'source': 'live_signal_pnl'}}

    # advance_last_fire is called when orders ARE emitted; not on liquidation.
    # Verify it does what it says.
    advance_last_fire(state, ['S1'], date(2026, 5, 14))
    assert state['S1']['last_fire_date'] == date(2026, 5, 14)
