"""SP-4: candidate->live promotion applies the per-class threshold.

Confirm-only regression — Phase 0 already wired per-class thresholds; this locks
the behavior so a future edit can't silently let an option strategy promote at
equity's 0.5 floor.
Run: pytest tests/test_promotion_thresholds_per_class.py -v
"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from strategies.lifecycle import (  # noqa: E402
    LifecycleStateMachine, StrategyRecord, StrategyState, _promotion_threshold)


def _sm_with(instrument_class):
    rec = StrategyRecord(strategy_id='S_x', state=StrategyState.CANDIDATE,
                         state_since='2026-05-01T00:00:00Z',
                         instrument_class=instrument_class)
    return LifecycleStateMachine({'S_x': rec})


def test_thresholds_lookup():
    assert _promotion_threshold('option') == {'min_sharpe': 0.80, 'max_drawdown': 0.30}
    assert _promotion_threshold('crypto') == {'min_sharpe': 0.50, 'max_drawdown': 0.70}
    assert _promotion_threshold('equity') == {'min_sharpe': 0.5, 'max_drawdown': 0.20}


def test_option_blocked_at_equity_passing_sharpe():
    sm = _sm_with('option')
    ok, msg = sm.can_transition('S_x', StrategyState.LIVE,
                                {'sharpe': 0.6, 'max_drawdown': 0.10})
    # 0.6 clears equity's 0.5 floor but NOT option's 0.80 floor.
    assert not ok
    assert '0.8' in msg and 'instrument_class=option' in msg


def test_option_passes_above_floor():
    sm = _sm_with('option')
    ok, _ = sm.can_transition('S_x', StrategyState.LIVE,
                              {'sharpe': 0.85, 'max_drawdown': 0.25})
    assert ok


def test_crypto_dd_tolerance():
    sm = _sm_with('crypto')
    ok, _ = sm.can_transition('S_x', StrategyState.LIVE,
                              {'sharpe': 0.6, 'max_drawdown': 0.65})
    assert ok  # 65% DD allowed for crypto, would fail equity's 20%
