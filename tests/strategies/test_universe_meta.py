from datetime import date
import pytest
from src.strategies.universe_meta import TickerMetadata

def test_construct_minimal():
    m = TickerMetadata(
        symbol="AAPL", asset_class="us_equity", exchange="NASDAQ",
        status="active", tradable=True, shortable=True,
        fractionable=True, easy_to_borrow=True,
        market_cap=3.5e12, adv_usd_20d=1.8e10,
        sector="Information Technology", industry="Consumer Electronics",
        options_eligible=True, in_sp500=True, in_r1000=True, in_r3000=True,
        listed_date=date(1980, 12, 12), delisted_date=None,
    )
    assert m.symbol == "AAPL"
    assert m.in_sp500 is True

def test_frozen():
    m = TickerMetadata(
        symbol="AAPL", asset_class="us_equity", exchange="NASDAQ",
        status="active", tradable=True, shortable=True,
        fractionable=True, easy_to_borrow=True,
        market_cap=None, adv_usd_20d=None,
        sector=None, industry=None,
        options_eligible=False, in_sp500=True, in_r1000=True, in_r3000=True,
        listed_date=None, delisted_date=None,
    )
    with pytest.raises((AttributeError, TypeError)):
        m.symbol = "MSFT"

def test_from_row():
    row = {
        "symbol": "MSFT", "asset_class": "us_equity", "exchange": "NASDAQ",
        "status": "active", "tradable": True, "shortable": True,
        "fractionable": True, "easy_to_borrow": True,
        "market_cap": 3.0e12, "adv_usd_20d": 1.5e10,
        "sector": "Information Technology", "industry": "Software",
        "options_eligible": True, "in_sp500": True, "in_r1000": True, "in_r3000": True,
        "listed_date": date(1986, 3, 13), "delisted_date": None,
    }
    m = TickerMetadata.from_row(row)
    assert m.symbol == "MSFT"
    assert m.market_cap == 3.0e12
