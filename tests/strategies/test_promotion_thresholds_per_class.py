"""SP-4: candidate->live promotion applies the per-class threshold.

Confirm-only regression — Phase 0 already wired per-class thresholds; this locks
the behavior so a future edit can't silently let an option strategy promote at
equity's 0.5 floor.
Run: pytest tests/test_promotion_thresholds_per_class.py -v
"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from strategies.lifecycle import (  # noqa: E402
    LifecycleStateMachine, StrategyRecord, StrategyState, _promotion_threshold)


def _sm_with(instrument_class):
    rec = StrategyRecord(strategy_id='S_x', state=StrategyState.CANDIDATE,
                         state_since='2026-05-01T00:00:00Z',
                         instrument_class=instrument_class)
    return LifecycleStateMachine({'S_x': rec})


def test_thresholds_lookup():
    # Policy 2026-07-13 v2: shared strictly-positive Sharpe floor + 100-trade
    # minimum; per-class DD ceilings unchanged. Calmar escape hatch (edge-
    # recovery epoch, 2026-07-27): dd may exceed the ceiling when
    # calmar >= min_calmar AND dd <= the per-class dd_hard_cap (50/60/85).
    # Keep value-synced with src/lib/promotion_service.js PROMOTION_THRESHOLDS.
    assert _promotion_threshold('option') == {
        'min_sharpe': 0.0, 'max_drawdown': 0.30, 'min_trades': 100,
        'min_calmar': 0.5, 'dd_hard_cap': 0.60}
    assert _promotion_threshold('crypto') == {
        'min_sharpe': 0.0, 'max_drawdown': 0.70, 'min_trades': 100,
        'min_calmar': 0.5, 'dd_hard_cap': 0.85}
    assert _promotion_threshold('equity') == {
        'min_sharpe': 0.0, 'max_drawdown': 0.20, 'min_trades': 100,
        'min_calmar': 0.5, 'dd_hard_cap': 0.50}


def test_option_dd_ceiling_between_equity_and_crypto():
    sm = _sm_with('option')
    # DD 25% fails equity's 20% ceiling but passes option's 30%.
    ok, _ = sm.can_transition('S_x', StrategyState.LIVE,
                              {'sharpe': 0.6, 'max_drawdown': 0.25})
    assert ok
    ok, msg = sm.can_transition('S_x', StrategyState.LIVE,
                                {'sharpe': 0.6, 'max_drawdown': 0.32})
    assert not ok
    assert 'instrument_class=option' in msg


def test_option_passes_above_floor():
    sm = _sm_with('option')
    ok, _ = sm.can_transition('S_x', StrategyState.LIVE,
                              {'sharpe': 0.85, 'max_drawdown': 0.25})
    assert ok


def test_non_positive_sharpe_blocked_all_classes():
    for ic in ('equity', 'option', 'crypto'):
        sm = _sm_with(ic)
        ok, _ = sm.can_transition('S_x', StrategyState.LIVE,
                                  {'sharpe': 0.0, 'max_drawdown': 0.05})
        assert not ok, ic


def test_crypto_dd_tolerance():
    sm = _sm_with('crypto')
    ok, _ = sm.can_transition('S_x', StrategyState.LIVE,
                              {'sharpe': 0.6, 'max_drawdown': 0.65})
    assert ok  # 65% DD allowed for crypto, would fail equity's 20%
