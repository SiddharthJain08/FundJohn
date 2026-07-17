from datetime import date
import os
import psycopg2
import pytest
from src.pipeline.ticker_metadata_writer import write_snapshots, build_metadata_rows


@pytest.fixture
def fake_sources():
    alpaca_rows = [{
        "symbol": "AAPL", "asset_class": "us_equity", "exchange": "NASDAQ",
        "status": "active", "tradable": True, "shortable": True,
        "fractionable": True, "easy_to_borrow": True,
        "first_seen_at": "1980-12-12", "last_seen_at": "2026-05-22",
    }]
    fmp_profile = {"AAPL": {"sector": "Information Technology",
                            "industry": "Consumer Electronics",
                            "mktCap": 3.5e12, "ipoDate": "1980-12-12"}}
    prices_parquet = {"AAPL": {"adv_usd_20d": 1.8e10}}
    options_cache = {"AAPL": True}
    return alpaca_rows, fmp_profile, prices_parquet, options_cache


def test_build_metadata_rows(fake_sources):
    rows = build_metadata_rows(date(2026, 5, 22), *fake_sources, source_tag="live_daily")
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "AAPL"
    assert r["market_cap"] == 3.5e12
    assert r["adv_usd_20d"] == 1.8e10
    assert r["options_eligible"] is True
    assert r["sector"] == "Information Technology"
    assert r["source_tag"] == "live_daily"
    assert r["in_sp500"] is True  # SP500_SET contains AAPL


@pytest.mark.integration  # writes to live Postgres; needs POSTGRES_URI
def test_idempotent_write_db(fake_sources):
    rows = build_metadata_rows(date(2026, 5, 22), *fake_sources, source_tag="test_idem")
    n1 = write_snapshots(os.environ["POSTGRES_URI"], rows)
    n2 = write_snapshots(os.environ["POSTGRES_URI"], rows)  # re-run
    assert n1 == 1
    assert n2 == 1  # UPSERT returns 1 affected; same row
    with psycopg2.connect(os.environ["POSTGRES_URI"]) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM ticker_metadata_snapshots WHERE source_tag='test_idem'")
        assert cur.fetchone()[0] == 1
        cur.execute("DELETE FROM ticker_metadata_snapshots WHERE source_tag='test_idem'")
