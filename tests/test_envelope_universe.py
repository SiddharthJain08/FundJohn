"""SP-7 Phase C Task 9 — no-floor envelope union (spec §4)."""
from datetime import date

from src.strategies.universe_meta import TickerMetadata
from src.strategies.universe_resolver import UniverseResolver

_TODAY = date(2026, 6, 8)


def _row(sym, in_sp500=True):
    r = type("Row", (), {})()
    r.snapshot_date = date(2026, 6, 1)
    r.symbol = sym
    r.metadata = TickerMetadata(
        symbol=sym, asset_class="us_equity", exchange="NYSE", status="active",
        tradable=True, shortable=True, fractionable=True, easy_to_borrow=True,
        market_cap=1e10, adv_usd_20d=1e8, sector="X", industry="Y",
        options_eligible=True, in_sp500=in_sp500, in_r1000=True, in_r3000=True,
        listed_date=date(2026, 5, 1), delisted_date=None)
    return r


class FakeDB:
    def fetch_metadata_as_of(self, as_of):
        return [_row("AAPL"), _row("NEWT")]   # NEWT: new listing, no coverage floor


class FloorOnlyAAPL:
    def has_floor(self, symbol, as_of):
        return symbol == "AAPL"


def _resolver():
    manifest = {"strategies": {"S1": {"state": "live"}}}  # default sp500 predicate
    return UniverseResolver(db=FakeDB(), coverage=FloorOnlyAAPL(),
                            manifest_loader=lambda: manifest,
                            today_fn=lambda: _TODAY)


def test_union_applies_floor_envelope_does_not():
    r = _resolver()
    assert r.union_universe(_TODAY) == ["AAPL"]              # floored (strategy resolve)
    assert r.envelope_universe(_TODAY) == ["AAPL", "NEWT"]   # no-floor (fetch envelope)


def test_resolve_public_behavior_unchanged():
    r = _resolver()
    assert r.resolve("S1", _TODAY) == ["AAPL"]


def test_cli_has_envelope_flag():
    from pathlib import Path
    src = Path("src/strategies/universe_resolver.py").read_text()
    assert "--envelope" in src
