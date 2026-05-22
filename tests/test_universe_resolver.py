from datetime import date
import pytest
from src.strategies.universe_resolver import UniverseResolver, AsOfInFutureError
from src.strategies.universe_meta import TickerMetadata
from src.strategies.universe_default import sp500, options_eligible_only

class FakeDB:
    def __init__(self, snapshots):
        self.snapshots = snapshots
    def fetch_metadata_as_of(self, as_of):
        rows = [s for s in self.snapshots if s.snapshot_date <= as_of]
        latest = {}
        for s in sorted(rows, key=lambda r: r.snapshot_date):
            latest[s.symbol] = s
        return list(latest.values())

class FakeCoverage:
    def __init__(self, coverage_map):
        self.cov = coverage_map
    def has_floor(self, symbol, as_of):
        return self.cov.get(symbol, True)

@pytest.fixture
def db():
    s1 = type("Row", (), {})()
    s1.snapshot_date = date(2026, 1, 1)
    s1.metadata = TickerMetadata(
        symbol="AAPL", asset_class="us_equity", exchange="NASDAQ",
        status="active", tradable=True, shortable=True,
        fractionable=True, easy_to_borrow=True,
        market_cap=3.5e12, adv_usd_20d=1.8e10,
        sector="IT", industry="CE",
        options_eligible=True, in_sp500=True, in_r1000=True, in_r3000=True,
        listed_date=date(1980, 12, 12), delisted_date=None,
    )
    s1.symbol = "AAPL"
    return FakeDB([s1])

_FIXED_TODAY = date(2026, 7, 1)  # pinned "today" so as_of=2026-06-01 is not future


def test_resolve_returns_predicate_matches(db, monkeypatch):
    resolver = UniverseResolver(db=db, coverage=FakeCoverage({}),
                                today_fn=lambda: _FIXED_TODAY)
    monkeypatch.setattr(resolver, "_load_predicate", lambda sid: sp500)
    result = resolver.resolve("S5", as_of=date(2026, 6, 1))
    assert result == ["AAPL"]

def test_resolve_cache_hit(db, monkeypatch):
    resolver = UniverseResolver(db=db, coverage=FakeCoverage({}),
                                today_fn=lambda: _FIXED_TODAY)
    monkeypatch.setattr(resolver, "_load_predicate", lambda sid: sp500)
    resolver.resolve("S5", date(2026, 6, 1))
    resolver.resolve("S5", date(2026, 6, 1))
    assert ("S5", date(2026, 6, 1)) in resolver._cache

def test_resolve_refuses_future(db, monkeypatch):
    resolver = UniverseResolver(db=db, coverage=FakeCoverage({}))
    monkeypatch.setattr(resolver, "_load_predicate", lambda sid: sp500)
    from datetime import timedelta, date as _d
    future = _d.today() + timedelta(days=1)
    with pytest.raises(AsOfInFutureError):
        resolver.resolve("S5", as_of=future)

def test_resolve_excludes_no_coverage(db, monkeypatch):
    resolver = UniverseResolver(
        db=db, coverage=FakeCoverage({"AAPL": False}),
        today_fn=lambda: _FIXED_TODAY,
    )
    monkeypatch.setattr(resolver, "_load_predicate", lambda sid: sp500)
    result = resolver.resolve("S5", as_of=date(2026, 6, 1))
    assert result == []
