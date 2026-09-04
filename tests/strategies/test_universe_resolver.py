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
    assert ("S5", date(2026, 6, 1), True) in resolver._cache

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


def test_union_universe_aggregates(db, monkeypatch):
    from datetime import date
    resolver = UniverseResolver(db=db, coverage=FakeCoverage({}),
                                today_fn=lambda: date(2026, 7, 1))
    monkeypatch.setattr(resolver, "_load_predicate", lambda sid: sp500)
    monkeypatch.setattr(resolver, "_live_strategy_ids", lambda states: ["S5", "S15"])
    result = resolver.union_universe(date(2026, 6, 1), states=("live",))
    assert result == ["AAPL"]  # both S5 and S15 resolve to {AAPL}, union dedupes


def test_union_universe_writes_audit(db, monkeypatch):
    from datetime import date
    audit_rows = []
    resolver = UniverseResolver(db=db, coverage=FakeCoverage({}),
                                today_fn=lambda: date(2026, 7, 1),
                                audit_writer=lambda row: audit_rows.append(row))
    monkeypatch.setattr(resolver, "_load_predicate", lambda sid: sp500)
    monkeypatch.setattr(resolver, "_live_strategy_ids", lambda states: ["S5"])
    monkeypatch.setattr(resolver, "_alpaca_universe_size", lambda: 8421)
    resolver.union_universe(date(2026, 6, 1), states=("live",))
    assert len(audit_rows) == 1
    a = audit_rows[0]
    assert a["resolved_for_date"] == date(2026, 6, 1)
    assert a["union_size"] == 1
    assert a["per_strategy_sizes"] == {"S5": 1}
    assert a["alpaca_universe_size"] == 8421


# ── Liquid cap (operator ruling 2026-09-04) ──────────────────────────────────
# tier_liquid is the LARGEST universe any strategy may resolve — a predicate
# wider than the ladder's top tier (no_otc matched the whole tape, 12,124
# names incl. SPAC units/warrants) is intersected down to it, live AND
# backtest. Kill switch: OPENCLAW_UNIVERSE_LIQUID_CAP=0.

def _row(symbol, **kw):
    base = dict(
        symbol=symbol, asset_class="us_equity", exchange="NASDAQ",
        status="active", tradable=True, shortable=True,
        fractionable=True, easy_to_borrow=True,
        market_cap=1e9, adv_usd_20d=1e7,
        sector="IT", industry="CE",
        options_eligible=False, in_sp500=False, in_r1000=False, in_r3000=False,
        listed_date=date(2020, 1, 1), delisted_date=None,
    )
    base.update(kw)
    r = type("Row", (), {})()
    r.snapshot_date = date(2026, 1, 1)
    r.metadata = TickerMetadata(**base)
    r.symbol = symbol
    return r


@pytest.fixture
def db_with_warrant():
    """AAPL (index member) + a warrant-shaped listing: active, tradable, NOT
    OTC — passes no_otc — but neither fractionable, easy-to-borrow, nor an
    index member, so it fails tier_liquid."""
    from src.strategies.universe_default import no_otc  # noqa: F401 (doc)
    return FakeDB([
        _row("AAPL", market_cap=3.5e12, adv_usd_20d=1.8e10,
             options_eligible=True, in_sp500=True, in_r1000=True, in_r3000=True),
        _row("AACOW", fractionable=False, easy_to_borrow=False, shortable=False,
             market_cap=None, adv_usd_20d=1e4),
    ])


def test_liquid_cap_binds_wide_predicates(db_with_warrant, monkeypatch):
    from src.strategies.universe_default import no_otc
    monkeypatch.delenv("OPENCLAW_UNIVERSE_LIQUID_CAP", raising=False)
    resolver = UniverseResolver(db=db_with_warrant, coverage=FakeCoverage({}),
                                today_fn=lambda: _FIXED_TODAY)
    monkeypatch.setattr(resolver, "_load_predicate", lambda sid: no_otc)
    assert resolver.resolve("S_wide", as_of=date(2026, 6, 1)) == ["AAPL"]


def test_liquid_cap_applies_to_envelope_too(db_with_warrant, monkeypatch):
    from src.strategies.universe_default import no_otc
    monkeypatch.delenv("OPENCLAW_UNIVERSE_LIQUID_CAP", raising=False)
    resolver = UniverseResolver(db=db_with_warrant, coverage=FakeCoverage({}),
                                today_fn=lambda: _FIXED_TODAY)
    monkeypatch.setattr(resolver, "_load_predicate", lambda sid: no_otc)
    monkeypatch.setattr(resolver, "_live_strategy_ids", lambda states: ["S_wide"])
    assert resolver.envelope_universe(date(2026, 6, 1)) == ["AAPL"]


def test_liquid_cap_never_shrinks_nested_index_predicates(db_with_warrant, monkeypatch):
    """sp500 ⊂ tier_r3000 ⊂ tier_liquid by construction — the cap is a no-op
    for every ladder-nested predicate."""
    monkeypatch.delenv("OPENCLAW_UNIVERSE_LIQUID_CAP", raising=False)
    resolver = UniverseResolver(db=db_with_warrant, coverage=FakeCoverage({}),
                                today_fn=lambda: _FIXED_TODAY)
    monkeypatch.setattr(resolver, "_load_predicate", lambda sid: sp500)
    assert resolver.resolve("S5", as_of=date(2026, 6, 1)) == ["AAPL"]


def test_liquid_cap_kill_switch(db_with_warrant, monkeypatch):
    from src.strategies.universe_default import no_otc
    monkeypatch.setenv("OPENCLAW_UNIVERSE_LIQUID_CAP", "0")
    resolver = UniverseResolver(db=db_with_warrant, coverage=FakeCoverage({}),
                                today_fn=lambda: _FIXED_TODAY)
    monkeypatch.setattr(resolver, "_load_predicate", lambda sid: no_otc)
    assert resolver.resolve("S_wide", as_of=date(2026, 6, 1)) == ["AACOW", "AAPL"]
