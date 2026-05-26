"""tests/test_phase_d_crypto.py — SP-3.1 Phase D crypto reference strategy + constants."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))


def test_crypto_cost_bps_present():
    from backtest.unified_backtest import INSTRUMENT_COST_BPS, resolve_cost_model_bps
    assert INSTRUMENT_COST_BPS.get('crypto') == 25.0
    assert resolve_cost_model_bps('crypto') == 25.0


def test_crypto_promotion_threshold_present():
    from strategies.lifecycle import PROMOTION_THRESHOLDS, _promotion_threshold
    thr = PROMOTION_THRESHOLDS.get('crypto')
    assert thr is not None
    assert thr['min_sharpe'] == 0.5
    assert thr['max_drawdown'] == 0.40
    assert _promotion_threshold('crypto') == thr


def test_crypto_in_routed_classes():
    from strategies.lifecycle import ROUTED_INSTRUMENT_CLASSES
    assert 'crypto' in ROUTED_INSTRUMENT_CLASSES


def test_can_transition_uses_crypto_threshold():
    # A crypto candidate with 35% drawdown must be ALLOWED (crypto cap 40%),
    # whereas the equity cap (20%) would block it.
    from strategies.lifecycle import LifecycleStateMachine, StrategyState, StrategyRecord
    from datetime import datetime, timezone
    lsm = LifecycleStateMachine.__new__(LifecycleStateMachine)
    lsm._records = {}
    now = datetime.now(timezone.utc).isoformat()
    lsm._records['S_x'] = StrategyRecord(
        strategy_id='S_x', state=StrategyState.CANDIDATE, state_since=now,
        history=[], metadata={}, instrument_class='crypto')
    ok, msg = lsm.can_transition('S_x', StrategyState.LIVE,
                                 {'sharpe': 0.8, 'max_drawdown': 0.35})
    assert ok, msg
