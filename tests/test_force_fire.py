"""tests/test_force_fire.py — cadence gate bypass on force_all."""
import sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from execution.signal_cadence_gate import filter_by_cadence


def test_force_all_returns_everything():
    signals = [
        {'strategy_id': 'a', 'ticker': 'X'},
        {'strategy_id': 'b', 'ticker': 'Y'},
        {'strategy_id': 'c', 'ticker': 'Z'},
    ]
    # State: 'a' has next_fire in the future (would normally skip);
    # 'b' has next_fire in the past (would normally pass);
    # 'c' is unknown (bootstrap → pass)
    state = {
        'a': {'next_fire_date': date(2026, 6, 1)},
        'b': {'next_fire_date': date(2026, 1, 1)},
    }
    today = date(2026, 5, 14)

    # WITHOUT force_all: 'a' should be skipped
    passed, skipped = filter_by_cadence(signals, state, today, force_all=False)
    sids_passed = {s['strategy_id'] for s in passed}
    sids_skipped = {s['strategy_id'] for s in skipped}
    assert 'a' in sids_skipped, f'expected a skipped, got passed={sids_passed} skipped={sids_skipped}'
    assert 'b' in sids_passed
    assert 'c' in sids_passed

    # WITH force_all: all three pass, no skips
    passed, skipped = filter_by_cadence(signals, state, today, force_all=True)
    assert len(passed) == 3
    assert len(skipped) == 0


if __name__ == '__main__':
    test_force_all_returns_everything()
    print('PASS test_force_all_returns_everything')
