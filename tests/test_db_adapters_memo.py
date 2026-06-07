"""SP-7 Phase C Task 2 — PostgresMetadataDB memoization + injected conn."""
from datetime import date
from unittest import mock


class _FakeCursor:
    description = [type("D", (), {"name": n})() for n in (
        "snapshot_date", "symbol", "asset_class", "exchange", "status", "tradable",
        "shortable", "fractionable", "easy_to_borrow", "market_cap", "adv_usd_20d",
        "sector", "industry", "options_eligible", "in_sp500", "in_r1000", "in_r3000",
        "listed_date", "delisted_date")]
    def execute(self, sql, params): pass
    def fetchall(self):
        return [(date(2026, 6, 1), "AAPL", "us_equity", "NASDAQ", "active", True,
                 True, True, True, 3.5e12, 1.8e10, "IT", "CE",
                 True, True, True, True, date(1980, 12, 12), None)]
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _FakeConn:
    def cursor(self): return _FakeCursor()
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_memo_one_connect_per_as_of(monkeypatch):
    from src.strategies import _db_adapters
    calls = []
    monkeypatch.setattr(_db_adapters.psycopg2, "connect",
                        lambda dsn: calls.append(dsn) or _FakeConn())
    db = _db_adapters.PostgresMetadataDB("postgresql://fake")
    r1 = db.fetch_metadata_as_of(date(2026, 6, 5))
    r2 = db.fetch_metadata_as_of(date(2026, 6, 5))  # memo hit
    assert len(calls) == 1
    assert r1 is r2
    assert r1[0].symbol == "AAPL"
    db.fetch_metadata_as_of(date(2026, 6, 6))       # different as_of → new query
    assert len(calls) == 2


def test_injected_conn_skips_connect(monkeypatch):
    from src.strategies import _db_adapters
    monkeypatch.setattr(_db_adapters.psycopg2, "connect",
                        lambda dsn: (_ for _ in ()).throw(AssertionError("must not connect")))
    db = _db_adapters.PostgresMetadataDB("postgresql://fake", conn=_FakeConn())
    rows = db.fetch_metadata_as_of(date(2026, 6, 5))
    assert rows[0].symbol == "AAPL"
