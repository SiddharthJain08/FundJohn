"""SP-7 Phase C Task 14 — archive universe = options-eligible ∩ live union (gated)."""


def _meta(sym, eligible):
    m = type("M", (), {})()
    m.options_eligible = eligible
    return m


class FakeResolver:
    def __init__(self):
        class DB:
            def fetch_metadata_as_of(self, as_of):
                rows = []
                for sym, el in (("AAPL", True), ("NOOPT", False)):
                    r = type("R", (), {})()
                    r.symbol = sym
                    r.metadata = _meta(sym, el)
                    rows.append(r)
                return rows
        self._db = DB()
    def union_universe(self, as_of, states):
        return ["AAPL", "NOOPT", "GHOST"]   # GHOST absent from metadata


def test_gate_off_returns_none(monkeypatch):
    monkeypatch.delenv("OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE", raising=False)
    from src.pipeline.backfillers.alpaca_options import _resolver_archive_universe
    assert _resolver_archive_universe("2026-06-08", resolver=FakeResolver()) is None


def test_gate_on_filters_options_eligible(monkeypatch):
    monkeypatch.setenv("OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE", "1")
    from src.pipeline.backfillers.alpaca_options import _resolver_archive_universe
    out = _resolver_archive_universe("2026-06-08", resolver=FakeResolver())
    assert out == ["AAPL"]      # NOOPT not eligible; GHOST no metadata → excluded


def test_failure_returns_none(monkeypatch):
    monkeypatch.setenv("OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE", "1")

    class Boom:
        def union_universe(self, *a, **k):
            raise RuntimeError("down")
        _db = None

    from src.pipeline.backfillers.alpaca_options import _resolver_archive_universe
    assert _resolver_archive_universe("2026-06-08", resolver=Boom()) is None
