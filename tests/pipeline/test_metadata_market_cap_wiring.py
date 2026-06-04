from datetime import date

from src.pipeline.ticker_metadata_writer import build_metadata_rows


ALPACA_ROWS = [
    {"symbol": "NVDA", "asset_class": "us_equity", "exchange": "NASDAQ",
     "status": "active", "tradable": True, "shortable": True,
     "fractionable": True, "easy_to_borrow": True,
     "first_seen_at": None, "last_seen_at": None},
    {"symbol": "TINY", "asset_class": "us_equity", "exchange": "NYSE",
     "status": "active", "tradable": True, "shortable": False,
     "fractionable": False, "easy_to_borrow": False,
     "first_seen_at": None, "last_seen_at": None},
]


def test_lookup_overrides_fmp_and_feeds_ranking():
    rows = build_metadata_rows(
        date(2026, 6, 4), ALPACA_ROWS,
        fmp_profile={"NVDA": {"mktCap": 1.0}},   # stale/wrong FMP value
        prices_parquet={}, options_cache={}, source_tag="test",
        market_cap_lookup={"NVDA": 3.0e12, "TINY": 5.0e8},
    )
    by = {r["symbol"]: r for r in rows}
    assert by["NVDA"]["market_cap"] == 3.0e12      # lookup wins over FMP
    assert by["TINY"]["market_cap"] == 5.0e8
    # ranking self-heals: both rank into r3000, NVDA into r1000
    assert by["NVDA"]["in_r1000"] is True
    assert by["TINY"]["in_r3000"] is True


def test_absent_lookup_preserves_legacy_fmp_path():
    rows = build_metadata_rows(
        date(2026, 6, 4), ALPACA_ROWS,
        fmp_profile={"NVDA": {"mktCap": 7.0}},
        prices_parquet={}, options_cache={}, source_tag="test",
    )
    by = {r["symbol"]: r for r in rows}
    assert by["NVDA"]["market_cap"] == 7.0          # byte-identical legacy
    assert by["TINY"]["market_cap"] is None
