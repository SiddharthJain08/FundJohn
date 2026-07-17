from datetime import date
import pytest


class StubResolver:
    def __init__(self):
        self.calls = []

    def resolve(self, sid, as_of):
        self.calls.append((sid, as_of))
        return ["AAPL", "MSFT"]


class StubStrategy:
    id = "S_test"

    def generate(self, bar_date, universe):
        return [{"ticker": universe[0], "size": 1}]


def test_unified_resolver_called_per_bar():
    from src.backtest.unified_backtest import run_backtest_with_resolver
    res = StubResolver()
    out = run_backtest_with_resolver(
        StubStrategy(), start=date(2024, 1, 1), end=date(2024, 1, 5),
        resolver=res,
    )
    assert len(res.calls) >= 3  # at least 3 business days in window
    assert all(c[0] == "S_test" for c in res.calls)


def test_unified_resolver_passes_bar_date():
    from src.backtest.unified_backtest import run_backtest_with_resolver
    res = StubResolver()
    run_backtest_with_resolver(
        StubStrategy(), start=date(2024, 6, 14), end=date(2024, 6, 14),
        resolver=res,
    )
    # 2024-06-14 is a Friday (business day)
    assert res.calls[0][1] == date(2024, 6, 14)


def test_quick_backtest_uses_resolver():
    from src.backtest.quick_backtest import run_with_resolver
    res = StubResolver()
    run_with_resolver(StubStrategy(), start=date(2024, 1, 1), end=date(2024, 1, 3), resolver=res)
    assert len(res.calls) >= 1


def test_regime_blended_uses_resolver():
    from src.backtest.regime_blended_backtest import run_with_resolver
    res = StubResolver()
    run_with_resolver(StubStrategy(), start=date(2024, 1, 1), end=date(2024, 1, 3), resolver=res)
    assert len(res.calls) >= 1


def test_intraday_regime_uses_resolver():
    from src.backtest.intraday_regime_backtest import run_with_resolver
    res = StubResolver()
    run_with_resolver(StubStrategy(), start=date(2024, 1, 1), end=date(2024, 1, 3), resolver=res)
    assert len(res.calls) >= 1


def test_regime_performance_uses_resolver():
    from src.backtest.regime_performance_analyzer import run_with_resolver
    res = StubResolver()
    run_with_resolver(StubStrategy(), start=date(2024, 1, 1), end=date(2024, 1, 3), resolver=res)
    assert len(res.calls) >= 1
