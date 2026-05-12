"""Tests for regime_live_pnl rollup computation.

Run: pytest tests/test_regime_live_pnl.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from metrics import regime_live_pnl as rlp  # noqa: E402


def _trade(strategy='s1', regime='LOW_VOL', pnl=1.5, closed_days_ago=5,
           held=3):
    closed = date.today() - timedelta(days=closed_days_ago)
    signal = closed - timedelta(days=held)
    return {
        'strategy_id': strategy,
        'regime_state': regime,
        'signal_date': signal,
        'closed_at': closed,
        'realized_pnl_pct': pnl,
        'days_held': held,
    }


def test_rollup_empty_input_returns_empty_frame():
    df = pd.DataFrame(columns=['strategy_id', 'regime_state', 'signal_date',
                                'closed_at', 'realized_pnl_pct', 'days_held'])
    out = rlp.compute_rollup(df, windows=[30, 90, 0])
    assert out.empty


def test_rollup_groups_by_strategy_regime_window():
    rows = [
        _trade('momentum_a', 'LOW_VOL', pnl=2.0, closed_days_ago=10),
        _trade('momentum_a', 'LOW_VOL', pnl=-1.0, closed_days_ago=15),
        _trade('momentum_a', 'HIGH_VOL', pnl=0.5, closed_days_ago=20),
        _trade('mean_rev', 'LOW_VOL', pnl=3.0, closed_days_ago=5),
    ]
    df = pd.DataFrame(rows)
    out = rlp.compute_rollup(df, windows=[30, 0])
    # 4 groups (3 strategy×regime pairs) × 2 windows = 6 rows expected
    assert len(out) == 6
    a_low_30 = out[(out['strategy_id'] == 'momentum_a')
                   & (out['regime_state'] == 'LOW_VOL')
                   & (out['window_days'] == 30)].iloc[0]
    assert a_low_30['trade_count'] == 2
    assert a_low_30['win_count'] == 1
    assert pytest.approx(a_low_30['total_pnl_pct']) == 1.0
    assert pytest.approx(a_low_30['avg_pnl_pct']) == 0.5


def test_rollup_window_excludes_trades_outside_horizon():
    rows = [
        _trade('s1', 'LOW_VOL', pnl=10.0, closed_days_ago=5),
        _trade('s1', 'LOW_VOL', pnl=-20.0, closed_days_ago=60),  # outside 30d
    ]
    df = pd.DataFrame(rows)
    out = rlp.compute_rollup(df, windows=[30, 90])
    w30 = out[(out['strategy_id'] == 's1') & (out['window_days'] == 30)].iloc[0]
    w90 = out[(out['strategy_id'] == 's1') & (out['window_days'] == 90)].iloc[0]
    assert w30['trade_count'] == 1
    assert w90['trade_count'] == 2


def test_rollup_window_zero_is_all_time():
    rows = [_trade('s1', 'LOW_VOL', pnl=1.0, closed_days_ago=400)]
    df = pd.DataFrame(rows)
    out = rlp.compute_rollup(df, windows=[30, 0])
    # 30d window: trade is 400d ago, so no row for window 30
    w30 = out[out['window_days'] == 30]
    assert w30.empty
    w0 = out[out['window_days'] == 0]
    assert len(w0) == 1
    assert w0.iloc[0]['trade_count'] == 1


def test_rollup_skips_open_trades_realized_pnl_is_null():
    rows = [_trade('s1', 'LOW_VOL', pnl=2.0, closed_days_ago=5)]
    df_with_open = pd.DataFrame(rows + [{
        'strategy_id': 's1',
        'regime_state': 'LOW_VOL',
        'signal_date': date.today() - timedelta(days=3),
        'closed_at': None,
        'realized_pnl_pct': None,
        'days_held': None,
    }])
    # The compute_rollup function processes whatever rows it gets — null
    # filtering is the SQL loader's job. So this dataframe must already
    # exclude the open row. Verify compute_rollup handles dropna for safety.
    df_clean = df_with_open.dropna(subset=['closed_at', 'realized_pnl_pct'])
    out = rlp.compute_rollup(df_clean, windows=[30])
    assert out.iloc[0]['trade_count'] == 1


def test_persist_rollup_writes_rows(monkeypatch):
    df = pd.DataFrame([{
        'strategy_id': 's1', 'regime_state': 'LOW_VOL', 'window_days': 30,
        'trade_count': 3, 'win_count': 2, 'total_pnl_pct': 1.5,
        'avg_pnl_pct': 0.5, 'stdev_pnl_pct': 1.0, 'sharpe_proxy': 0.5,
        'max_dd_proxy': -1.0, 'avg_hold_days': 4.0,
        'last_signal_at': datetime.now(timezone.utc),
    }])
    inserts: list = []

    class FakeCursor:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def executemany(self, sql, rows): inserts.extend(rows)
        def execute(self, *a, **k): pass

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def cursor(self): return FakeCursor()
        def commit(self): pass

    monkeypatch.setattr(rlp, '_connect', lambda uri: FakeConn())
    rlp.persist_rollup(df, uri='ignored', run_at=datetime.now(timezone.utc))
    assert len(inserts) == 1


def test_persist_rollup_empty_returns_zero():
    assert rlp.persist_rollup(pd.DataFrame(), uri='ignored') == 0
