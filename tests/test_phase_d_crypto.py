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


import os
import pytest


@pytest.mark.skipif(not os.environ.get('POSTGRES_URI'), reason='no POSTGRES_URI')
def test_fractional_qty_survives_round_trip():
    import psycopg2
    conn = psycopg2.connect(os.environ['POSTGRES_URI'])
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO alpaca_submissions
              (run_date, ticker, strategy_id, direction, qty,
               entry_price, stop_price, target_price, pct_nav, notional_usd,
               time_in_force, order_class, client_order_id, submitted_at)
            VALUES (DATE '2099-01-01', 'BTC-USD', '__pytest__', 'long', 0.00018,
                    76000, 70000, 80000, 0.01, 15.0, 'gtc', 'simple', '__pytest_coid__', NOW())
            RETURNING qty
        """)
        stored = float(cur.fetchone()[0])
        assert stored == pytest.approx(0.00018), f'qty truncated to {stored}'
    finally:
        conn.rollback()   # never commit — leaves the canonical table untouched
        conn.close()
