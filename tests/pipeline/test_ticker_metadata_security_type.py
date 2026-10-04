"""Writer maps vendor profile -> security_type (Phase 1). No DB."""
from datetime import date

from src.pipeline import ticker_metadata_writer as w


def _alpaca(sym):
    return {"symbol": sym, "asset_class": "us_equity", "exchange": "NYSE", "status": "active",
            "tradable": True, "shortable": True, "fractionable": True, "easy_to_borrow": True,
            "first_seen_at": "2000-01-01", "last_seen_at": "2026-10-01"}


def test_build_metadata_rows_maps_security_type():
    prof = {"SPY": {"isEtf": True, "isFund": False, "isAdr": False},
            "BIL": {"isEtf": True},
            "VFIAX": {"isEtf": False, "isFund": True, "isAdr": False},
            "TSM": {"isEtf": False, "isFund": False, "isAdr": True},
            "AAPL": {"isEtf": False, "isFund": False, "isAdr": False},
            "BRK.B": {"_empty": True, "_fetched_at": "x"}}
    syms = list(prof) + ["NOPROFILE"]
    rows = w.build_metadata_rows(date(2026, 10, 2), [_alpaca(s) for s in syms], prof,
                                 {}, {}, source_tag="t")
    got = {r["symbol"]: r["security_type"] for r in rows}
    assert got == {"SPY": "etf", "BIL": "etf", "VFIAX": "fund", "TSM": "adr",
                   "AAPL": "stock", "BRK.B": None, "NOPROFILE": None}


def test_upsert_sql_names_column_and_keeps_known_value():
    assert "security_type" in w.UPSERT_SQL
    assert "%(security_type)s" in w.UPSERT_SQL
    assert "COALESCE(EXCLUDED.security_type, ticker_metadata_snapshots.security_type)" in w.UPSERT_SQL


def test_write_snapshots_defaults_missing_security_type(monkeypatch):
    seen = []

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, sql, params): seen.append(params)

    class Conn(Cur):
        def cursor(self): return Cur()
        def commit(self): pass

    monkeypatch.setattr(w.psycopg2, "connect", lambda dsn: Conn())
    row = {"symbol": "X"}   # a builder that predates security_type
    assert w.write_snapshots("dsn", [row]) == 1
    assert seen[0]["security_type"] is None and "security_type" not in row
