from datetime import date
import pytest
from src.strategies.universe_meta import TickerMetadata
from src.strategies.universe_default import (
    DEFAULT_UNIVERSE_FILTER, CANDIDATE_PREDICATES,
    sp500, r1000, r3000, options_eligible_only,
    large_cap, mid_cap, small_cap_liquid,
    large_cap_options, mid_cap_options,
    no_adr, no_otc, top500_by_adv,
)

@pytest.fixture
def aapl():
    return TickerMetadata(
        symbol="AAPL", asset_class="us_equity", exchange="NASDAQ",
        status="active", tradable=True, shortable=True,
        fractionable=True, easy_to_borrow=True,
        market_cap=3.5e12, adv_usd_20d=1.8e10,
        sector="Information Technology", industry="Consumer Electronics",
        options_eligible=True, in_sp500=True, in_r1000=True, in_r3000=True,
        listed_date=date(1980, 12, 12), delisted_date=None,
    )

@pytest.fixture
def unknown_pink():
    return TickerMetadata(
        symbol="XYZQ", asset_class="us_equity", exchange="OTC",
        status="active", tradable=True, shortable=False,
        fractionable=False, easy_to_borrow=False,
        market_cap=5e7, adv_usd_20d=1e5,
        sector=None, industry=None,
        options_eligible=False, in_sp500=False, in_r1000=False, in_r3000=False,
        listed_date=None, delisted_date=None,
    )

def test_default_filter_aapl(aapl):
    assert DEFAULT_UNIVERSE_FILTER(aapl, date(2026, 1, 1)) is True

def test_default_filter_unknown(unknown_pink):
    assert DEFAULT_UNIVERSE_FILTER(unknown_pink, date(2026, 1, 1)) is False

def test_candidate_set_count():
    assert len(CANDIDATE_PREDICATES) == 12

def test_each_candidate_callable(aapl):
    for name, fn in CANDIDATE_PREDICATES.items():
        result = fn(aapl, date(2026, 1, 1))
        assert isinstance(result, bool), f"{name} returned non-bool"

def test_options_eligible_only_filters_unknown(unknown_pink, aapl):
    assert options_eligible_only(unknown_pink, date(2026, 1, 1)) is False
    assert options_eligible_only(aapl, date(2026, 1, 1)) is True

def test_no_otc_filters_pink(unknown_pink):
    assert no_otc(unknown_pink, date(2026, 1, 1)) is False

def test_top500_by_adv_handles_none_adv():
    m = TickerMetadata(
        symbol="ABCD", asset_class="us_equity", exchange="NASDAQ",
        status="active", tradable=True, shortable=True,
        fractionable=True, easy_to_borrow=True,
        market_cap=1e9, adv_usd_20d=None,
        sector="X", industry="Y",
        options_eligible=False, in_sp500=False, in_r1000=False, in_r3000=False,
        listed_date=date(2020, 1, 1), delisted_date=None,
    )
    assert top500_by_adv(m, date(2026, 1, 1)) is False
