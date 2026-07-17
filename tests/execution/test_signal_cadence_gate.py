import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.signal_cadence_gate import (
    filter_by_cadence, compute_next_fire_date, advance_last_fire,
    EXPECTED_HOLDING_PERIODS, BOOTSTRAP_DAILY_DAYS,
)

def _sig(strategy_id, ticker='AAPL'):
    return {'strategy_id': strategy_id, 'ticker': ticker}

def _state(last, next_, avg=1.0, source='live_signal_pnl'):
    return {'last_fire_date': last, 'next_fire_date': next_, 'avg_holding_days': avg, 'source': source}

def test_filter_passes_when_today_geq_next_fire():
    today = date(2026, 5, 12)
    state = {'S1': _state(date(2026, 5, 10), date(2026, 5, 12))}
    passed, skipped = filter_by_cadence([_sig('S1')], state, today)
    assert len(passed) == 1
    assert len(skipped) == 0

def test_filter_skips_when_today_lt_next_fire():
    today = date(2026, 5, 12)
    state = {'S1': _state(date(2026, 5, 11), date(2026, 5, 13))}
    passed, skipped = filter_by_cadence([_sig('S1')], state, today)
    assert len(passed) == 0
    assert len(skipped) == 1
    assert 'cadence_pending_until_2026-05-13' in skipped[0]['reason']

def test_filter_unknown_strategy_passes_with_bootstrap():
    today = date(2026, 5, 12)
    state = {}
    passed, skipped = filter_by_cadence([_sig('UNKNOWN')], state, today)
    assert len(passed) == 1

def test_compute_next_fire_date_uses_avg_holding():
    next_d = compute_next_fire_date(last=date(2026, 5, 10), avg_holding_days=2.2)
    assert next_d == date(2026, 5, 13)  # ceil(2.2) = 3 days

def test_compute_next_fire_date_handles_zero_avg():
    next_d = compute_next_fire_date(last=date(2026, 5, 10), avg_holding_days=0.0)
    assert next_d == date(2026, 5, 11)  # min 1-day cadence

def test_advance_last_fire_updates_only_listed():
    today = date(2026, 5, 12)
    state = {'S1': _state(date(2026, 5, 10), date(2026, 5, 12)),
             'S2': _state(date(2026, 5, 10), date(2026, 5, 12))}
    advance_last_fire(state, ['S1'], today)
    assert state['S1']['last_fire_date'] == today
    assert state['S2']['last_fire_date'] == date(2026, 5, 10)

def test_two_strategies_only_one_passes():
    today = date(2026, 5, 12)
    state = {'S1': _state(date(2026, 5, 11), date(2026, 5, 12)),
             'S2': _state(date(2026, 5, 11), date(2026, 5, 14))}
    passed, skipped = filter_by_cadence([_sig('S1'), _sig('S2')], state, today)
    assert {p['strategy_id'] for p in passed} == {'S1'}
    assert {s['strategy_id'] for s in skipped} == {'S2'}
