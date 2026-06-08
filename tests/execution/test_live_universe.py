"""SP-7 Phase C Task 5 — mirror-clamp per-strategy universes (spec §3.3)."""
import json
from datetime import date

import pytest

AS_OF = date(2026, 6, 8)
FALLBACK = ["AAPL", "BRK-B", "RDDT", "SPY", "BTC-USD", "GLD"]

# metadata: dot-form symbols (Alpaca); AAPL+BRK.B in sp500, RDDT not; SPY is
# us_equity in Alpaca metadata but category 'etf' in universe_config.
META = {"AAPL": ("us_equity", True), "BRK.B": ("us_equity", True),
        "RDDT": ("us_equity", False), "SPY": ("us_equity", False)}
CATS = {"AAPL": "equity", "BRK-B": "equity", "RDDT": "equity",
        "SPY": "etf", "GLD": "etf"}


class FakeResolver:
    def __init__(self, per_strategy):
        self.per_strategy = per_strategy   # {sid: list[dot-form syms] | Exception}
    def resolve(self, sid, as_of):
        v = self.per_strategy[sid]
        if isinstance(v, Exception):
            raise v
        return v


@pytest.fixture
def refs(tmp_path, monkeypatch):
    # NOTE: universe_filter_ref is nested under metadata — the REAL manifest
    # shape (adoption writer + _load_predicate both use metadata.universe_filter_ref)
    manifest = {"strategies": {
        "S_default": {"state": "live"},                                   # ref absent → sp500
        "S_adopted": {"state": "live", "metadata":
                      {"universe_filter_ref": "src.strategies.universe_default:tier_r1000"}},
        "S_broken":  {"state": "live"},
    }}
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(manifest))
    import src.execution.live_universe as lu
    monkeypatch.setattr(lu, "MANIFEST_PATH", str(p))
    return lu


def test_unadopted_equals_clamp_output(refs):
    """THE load-bearing parity test: a default-predicate strategy reproduces
    clamp_universe() exactly (mirror-clamp semantics, decision D3)."""
    from src.execution.universe_clamp import clamp_universe
    import os
    os.environ["OPENCLAW_ENGINE_UNIVERSE_CLAMP"] = "sp500"
    clamp_out = clamp_universe(list(FALLBACK), lambda: META, lambda: CATS)

    resolver = FakeResolver({"S_default": ["AAPL", "BRK.B"]})  # sp500∩floor, dot-form
    out = refs.build_strategy_universes(
        ["S_default"], AS_OF, list(FALLBACK), resolver=resolver,
        meta_fetch=lambda: META, category_fetch=lambda: CATS)
    assert set(out["S_default"]["universe"]) == set(clamp_out)
    assert out["S_default"]["predicate"] == "sp500"
    assert out["S_default"]["adopted"] is False
    assert out["S_default"]["error"] is None


def test_adopted_widens_equities_only(refs):
    resolver = FakeResolver({"S_adopted": ["AAPL", "BRK.B", "RDDT"]})  # r1000 adds RDDT
    out = refs.build_strategy_universes(
        ["S_adopted"], AS_OF, list(FALLBACK), resolver=resolver,
        meta_fetch=lambda: META, category_fetch=lambda: CATS)
    u = set(out["S_adopted"]["universe"])
    assert "RDDT" in u                          # adoption took effect
    assert {"SPY", "BTC-USD", "GLD"} <= u       # passthrough intact
    assert out["S_adopted"]["adopted"] is True
    assert out["S_adopted"]["predicate"] == "tier_r1000"


def test_nonequity_passthrough_survives_any_predicate(refs):
    resolver = FakeResolver({"S_default": []})  # predicate matches NOTHING
    out = refs.build_strategy_universes(
        ["S_default"], AS_OF, list(FALLBACK), resolver=resolver,
        meta_fetch=lambda: META, category_fetch=lambda: CATS)
    u = set(out["S_default"]["universe"])
    assert {"SPY", "BTC-USD", "GLD"} <= u       # crypto/ETF never clamped out
    assert "AAPL" not in u                      # equities follow the predicate


def test_dash_dot_bridge(refs):
    """Resolver emits BRK.B (metadata form); fallback holds BRK-B (parquet form)."""
    resolver = FakeResolver({"S_default": ["BRK.B"]})
    out = refs.build_strategy_universes(
        ["S_default"], AS_OF, list(FALLBACK), resolver=resolver,
        meta_fetch=lambda: META, category_fetch=lambda: CATS)
    assert "BRK-B" in out["S_default"]["universe"]


def test_fail_open_keeps_fallback_and_records_error(refs):
    resolver = FakeResolver({"S_broken": RuntimeError("db down")})
    out = refs.build_strategy_universes(
        ["S_broken"], AS_OF, list(FALLBACK), resolver=resolver,
        meta_fetch=lambda: META, category_fetch=lambda: CATS)
    assert out["S_broken"]["universe"] == list(FALLBACK)   # never empty a live universe
    assert "db down" in out["S_broken"]["error"]


def test_universe_always_subset_of_fallback(refs):
    """Resolved names without price data (pre-C2 adopted tiers) are excluded —
    per-strategy universe ⊆ fallback so load_prices always has the columns."""
    resolver = FakeResolver({"S_adopted": ["AAPL", "NODATA1", "NODATA2"]})
    out = refs.build_strategy_universes(
        ["S_adopted"], AS_OF, list(FALLBACK), resolver=resolver,
        meta_fetch=lambda: META, category_fetch=lambda: CATS)
    assert set(out["S_adopted"]["universe"]) <= set(FALLBACK)


class _Cur:
    def __init__(self, log): self.log = log
    def execute(self, sql, params): self.log.append(params)
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Conn:
    def __init__(self): self.log = []; self.committed = False
    def cursor(self): return _Cur(self.log)
    def commit(self): self.committed = True


def test_string_as_of_normalized(refs):
    """as_of passed as ISO string must not TypeError in resolve()/has_floor()
    and must not fail-open every strategy to the shared fallback (inert C1)."""
    resolver = FakeResolver({"S_default": ["AAPL", "BRK.B"]})
    out = refs.build_strategy_universes(
        ["S_default"], "2026-06-08", list(FALLBACK), resolver=resolver,
        meta_fetch=lambda: META, category_fetch=lambda: CATS)
    assert out["S_default"]["error"] is None   # not a fail-open


def test_shadow_writer_diffs_and_upserts(refs):
    resolver = FakeResolver({"S_default": ["AAPL", "BRK.B"],
                             "S_adopted": ["AAPL", "BRK.B", "RDDT"]})
    conn = _Conn()
    refs.write_shadow_parity(AS_OF, ["S_default", "S_adopted"], list(FALLBACK),
                             conn=conn, resolver=resolver,
                             meta_fetch=lambda: META, category_fetch=lambda: CATS)
    assert conn.committed
    by_sid = {p[1]: p for p in conn.log}
    # S_default mirrors the clamp: only RDDT (non-sp500 equity) removed
    assert json.loads(by_sid["S_default"][6]) == ["RDDT"]      # removed_tickers
    assert json.loads(by_sid["S_default"][5]) == []            # added_tickers
    # S_adopted keeps RDDT → zero diff vs the 6-name fallback
    assert json.loads(by_sid["S_adopted"][6]) == []
    assert by_sid["S_adopted"][7] is True                      # is_adopted
