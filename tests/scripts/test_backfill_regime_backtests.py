"""Unit tests for scripts/backfill_regime_backtests.py."""
from __future__ import annotations
import sys
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))

import backfill_regime_backtests as bf


@pytest.fixture
def fake_conn():
    """Mock psycopg2 connection with cursor context manager."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def test_resolve_filepath_direct():
    """Exact-name file resolves."""
    fp = bf.resolve_filepath('momentum_12_1')
    assert fp is not None
    assert fp.name == 'momentum_12_1.py'


def test_resolve_filepath_lowercase_fallback():
    """Strategy id with uppercase prefix resolves to lowercase file."""
    fp = bf.resolve_filepath('S5_max_pain')
    assert fp is not None
    # Manifest id is mixed-case, file is lowercase
    assert fp.name.lower() == 's5_max_pain.py'


def test_resolve_filepath_missing():
    """Unknown strategy returns None."""
    assert bf.resolve_filepath('strategy_that_does_not_exist_zzz') is None


def test_write_strategy_writes_four_regime_rows(fake_conn):
    """Each strategy writes exactly len(CANONICAL_REGIMES) rows even for partial breakdowns."""
    conn, _ = fake_conn
    run_id = str(uuid.uuid4())
    result = {
        'regime_breakdown': {
            'LOW_VOL':       {'sharpe': 1.2, 'max_dd': 0.05, 'total_return_pct': 8.0,
                              'trade_count': 50, 'oos_days': 200, 'window_count': 1},
            'TRANSITIONING': {'sharpe': 0.8, 'max_dd': 0.07, 'total_return_pct': 4.0,
                              'trade_count': 30, 'oos_days': 150, 'window_count': 1},
            'HIGH_VOL':      {'note': 'no_oos_window'},
            'CRISIS':        {'note': 'not_declared'},
        },
        'declared_regimes': ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL'],
    }
    with patch.object(bf.psycopg2.extras, 'execute_values') as ev:
        n = bf.write_strategy(conn, run_id, 'TEST_strategy', {}, result)
    assert n == 4
    assert ev.called
    # Inspect the rows arg (third positional)
    rows = ev.call_args[0][2]
    assert len(rows) == 4
    regimes = [r[2] for r in rows]
    assert regimes == bf.CANONICAL_REGIMES
    # CRISIS row should have note='not_declared' and no metrics
    crisis = [r for r in rows if r[2] == 'CRISIS'][0]
    assert crisis[9] == 'not_declared'  # note column
    assert crisis[3] is None  # sharpe


def test_write_strategy_handles_zero_trade_regime(fake_conn):
    """A regime with trade_count=0 still writes a row (sharpe 0 is meaningful)."""
    conn, _ = fake_conn
    run_id = str(uuid.uuid4())
    result = {
        'regime_breakdown': {
            'LOW_VOL': {'sharpe': 0.0, 'max_dd': 0.0, 'total_return_pct': 0.0,
                        'trade_count': 0, 'oos_days': 100, 'window_count': 1},
            'TRANSITIONING': {'note': 'not_declared'},
            'HIGH_VOL': {'note': 'not_declared'},
            'CRISIS': {'note': 'not_declared'},
        },
        'declared_regimes': ['LOW_VOL'],
    }
    with patch.object(bf.psycopg2.extras, 'execute_values') as ev:
        n = bf.write_strategy(conn, run_id, 'TEST_zero', {}, result)
    assert n == 4
    rows = ev.call_args[0][2]
    lv = [r for r in rows if r[2] == 'LOW_VOL'][0]
    assert lv[3] == 0.0  # sharpe — explicit zero, not None


def test_already_written_returns_true_when_present(fake_conn):
    conn, cur = fake_conn
    cur.fetchone.return_value = (1,)
    assert bf.already_written(conn, 'r', 's') is True


def test_already_written_returns_false_when_missing(fake_conn):
    conn, cur = fake_conn
    cur.fetchone.return_value = None
    assert bf.already_written(conn, 'r', 's') is False


def test_load_strategies_filters_by_state():
    """Reads real manifest; every returned entry must have the requested state + a real file."""
    live = bf.load_strategies({'live'})
    assert len(live) > 0, 'manifest has live strategies'
    for sid, entry, fp in live:
        assert entry['state'] == 'live'
        assert fp.exists()
    # 'archived' filter excludes 'live'
    arch = bf.load_strategies({'archived'})
    arch_ids = {sid for sid, _, _ in arch}
    live_ids = {sid for sid, _, _ in live}
    assert arch_ids.isdisjoint(live_ids)
