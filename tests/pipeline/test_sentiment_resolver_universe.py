"""SP-7 Phase C Task 13 — sentiment universe widened by adopted-union (gated)."""
from datetime import date


def test_widen_helper_unions_and_bridges(monkeypatch):
    import src.pipeline.run_sentiment_step as step

    class FakeResolver:
        def union_universe(self, as_of, states):
            assert states == ("live", "candidate")
            return ["BRK.B", "ADOPTED1"]

    monkeypatch.setenv("OPENCLAW_SENTIMENT_RESOLVER_UNIVERSE", "1")
    out = step._widen_with_resolver(["AAPL"], today=date(2026, 6, 8),
                                    resolver=FakeResolver())
    assert set(out) == {"AAPL", "BRK-B", "ADOPTED1"}   # union + dot→dash


def test_gate_off_identity(monkeypatch):
    import src.pipeline.run_sentiment_step as step
    monkeypatch.delenv("OPENCLAW_SENTIMENT_RESOLVER_UNIVERSE", raising=False)
    out = step._widen_with_resolver(["AAPL"], today=date(2026, 6, 8),
                                    resolver=None)
    assert out == ["AAPL"]


def test_resolver_failure_fails_open(monkeypatch):
    import src.pipeline.run_sentiment_step as step

    class Boom:
        def union_universe(self, *a, **k):
            raise RuntimeError("down")

    monkeypatch.setenv("OPENCLAW_SENTIMENT_RESOLVER_UNIVERSE", "1")
    out = step._widen_with_resolver(["AAPL"], today=date(2026, 6, 8),
                                    resolver=Boom())
    assert out == ["AAPL"]                              # sentiment must not die
