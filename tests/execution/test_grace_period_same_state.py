"""_strategies_in_grace_period ignores same-state history notes (from == to)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import strategy_weights as sw  # noqa: E402


def _ts(days_ago):
    d = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return d.strftime('%Y-%m-%dT%H:%M:%S.000Z')


def _ev(f, t, days_ago):
    return {'from_state': f, 'to_state': t, 'timestamp': _ts(days_ago),
            'actor': 'x', 'reason': 'r', 'metadata': {}}


def test_same_state_note_does_not_reopen_grace():
    m = {'strategies': {
        'old_promo_then_note': {'state': 'live', 'history': [
            _ev('candidate', 'live', 90), _ev('live', 'live', 1)]},
        'fresh_promo': {'state': 'live', 'history': [_ev('candidate', 'live', 2)]},
        'fresh_promo_then_old_note': {'state': 'live', 'history': [
            _ev('candidate', 'live', 2), _ev('live', 'live', 1)]},
    }}
    got = sw._strategies_in_grace_period(m, 30)
    assert 'old_promo_then_note' not in got      # note must not count as a promotion
    assert 'fresh_promo' in got
    assert 'fresh_promo_then_old_note' in got    # real promotion still counts
