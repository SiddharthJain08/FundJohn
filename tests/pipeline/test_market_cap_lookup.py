import pandas as pd
import pytest
from datetime import date

from src.pipeline.market_cap_lookup import build_market_cap_lookup


@pytest.fixture
def stores(tmp_path):
    shares = tmp_path / "shares.parquet"
    prices = tmp_path / "prices.parquet"
    pd.DataFrame([
        {"ticker": "NVDA", "asof_date": "2026-01-26", "shares": 2.4e9},
        {"ticker": "NVDA", "asof_date": "2026-04-26", "shares": 2.5e9},
        {"ticker": "OLDCO", "asof_date": "2020-02-01", "shares": 1.0e8},
    ]).to_parquet(shares, index=False)
    pd.DataFrame([
        {"ticker": "NVDA", "date": "2026-06-01", "close": 100.0},
        {"ticker": "NVDA", "date": "2026-06-03", "close": 110.0},
        {"ticker": "OLDCO", "date": "2026-01-02", "close": 5.0},  # stale vs 06-03
    ]).to_parquet(prices, index=False)
    return shares, prices


def test_latest_shares_times_latest_close(stores):
    shares, prices = stores
    out = build_market_cap_lookup(["NVDA"], date(2026, 6, 3),
                                  shares_path=shares, prices_path=prices)
    assert out["NVDA"] == pytest.approx(2.5e9 * 110.0)


def test_shares_carry_forward_pit(stores):
    shares, prices = stores
    out = build_market_cap_lookup(["NVDA"], date(2026, 6, 1),
                                  shares_path=shares, prices_path=prices)
    # as_of 06-01: latest shares row <= 06-01 is the 04-26 filing; close = 100
    assert out["NVDA"] == pytest.approx(2.5e9 * 100.0)


def test_stale_price_yields_none(stores):
    shares, prices = stores
    out = build_market_cap_lookup(["OLDCO"], date(2026, 6, 3),
                                  shares_path=shares, prices_path=prices)
    assert out["OLDCO"] is None  # close is >10 days old


def test_missing_ticker_yields_none(stores):
    shares, prices = stores
    out = build_market_cap_lookup(["GHOST"], date(2026, 6, 3),
                                  shares_path=shares, prices_path=prices)
    assert out["GHOST"] is None
