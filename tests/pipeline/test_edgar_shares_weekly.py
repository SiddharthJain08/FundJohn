"""edgar_shares weekly-refresh additions (openclaw-edgar-shares.timer, 2026-08-23).

The ingester had NO scheduler — shares_outstanding.parquet froze at
fetched_at 2026-06-04. A weekly sweep of the ~7.1k CIK-mapped Alpaca
equities is ~80 min / ~28 GB of companyfacts JSON, so the unit runs a
BOUNDED slice per week: tickers never attempted first, then the stalest by
last attempt, capped by --max-tickers. A fetch log (not the parquet's
fetched_at, which only moves when a NEW row lands) records attempts so a
ticker with no new filing is not re-fetched every week.
"""
from __future__ import annotations

import json

from src.pipeline.backfillers import edgar_shares as mod


def test_order_never_attempted_first_then_stalest():
    log = {'B': '2026-08-01T00:00:00+00:00', 'A': '2026-07-01T00:00:00+00:00'}
    universe = ['A', 'B', 'D', 'C']
    assert mod.order_by_fetch_log(universe, log) == ['C', 'D', 'A', 'B']


def test_order_then_cap_keeps_head():
    log = {'A': '2026-07-01T00:00:00+00:00'}
    assert mod.order_by_fetch_log(['A', 'B', 'C'], log, max_tickers=2) == ['B', 'C']


def test_fetch_log_round_trip_is_atomic_and_merges(tmp_path):
    p = tmp_path / '.shares_outstanding_fetch_log.json'
    assert mod.load_fetch_log(p) == {}
    mod.save_fetch_log(p, {'A': '2026-08-23T03:00:00+00:00'})
    mod.save_fetch_log(p, {'B': '2026-08-23T03:05:00+00:00'})   # merge, not overwrite
    assert set(json.loads(p.read_text())) == {'A', 'B'}
    assert not list(tmp_path.glob('*.tmp*')), 'tmp file must be os.replace-d away'


def test_alpaca_active_universe_query(monkeypatch):
    captured = {}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, *args):
            captured['sql'] = sql
        def fetchall(self):
            return [('AAPL',), ('BRK.B',)]

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()
        def close(self): pass

    monkeypatch.setattr(mod, '_connect_pg', lambda dsn: _Conn())
    out = mod.alpaca_active_universe('postgresql://stub')
    assert out == ['AAPL', 'BRK.B']
    sql = captured['sql'].lower()
    assert 'alpaca_tradable_universe' in sql and "status" in sql and 'tradable' in sql
    assert 'us_equity' in sql
