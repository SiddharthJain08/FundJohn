"""Phase 2E — clean-room StrategyTemplate ABC tests.

Contract-equivalent to backtesting.py Strategy.init/next API but written from
scratch (no AGPL code copied).  Tests fix the contract so future
StrategyCoder-emitted strategies have an oracle."""
import pandas as pd
import pytest


def _ohlcv(n=20, base=100.0):
    idx = pd.date_range("2026-01-02", periods=n, freq="1D", tz="America/New_York")
    return pd.DataFrame({
        "open": base, "high": base + 1, "low": base - 1, "close": base + 0.5, "volume": 1000,
    }, index=idx)


def test_init_runs_once_and_next_runs_per_bar():
    from src.strategies.strategy_template_base import StrategyTemplate, run
    init_calls = []
    next_calls = []

    class S(StrategyTemplate):
        def init(self):
            init_calls.append(1)
        def next(self):
            next_calls.append(self.bar_idx)

    run(S, _ohlcv(5))
    assert sum(init_calls) == 1
    assert next_calls == [0, 1, 2, 3, 4]


def test_indicator_warmup_detection():
    from src.strategies.strategy_template_base import StrategyTemplate, run
    seen_lengths = []

    class S(StrategyTemplate):
        def init(self):
            self.sma3 = self.I(lambda c: c.rolling(3).mean(), self.data.close)
        def next(self):
            seen_lengths.append((self.bar_idx, pd.notna(self.sma3.iloc[self.bar_idx])))

    run(S, _ohlcv(6))
    assert seen_lengths == [(0, False), (1, False), (2, True), (3, True), (4, True), (5, True)]


def test_commission_callable_invoked_per_fill():
    from src.strategies.strategy_template_base import StrategyTemplate, run
    fees_paid = []

    def fee(size, price):
        fees_paid.append((size, price))
        return abs(size) * 0.005

    class S(StrategyTemplate):
        def init(self): pass
        def next(self):
            if self.bar_idx == 1:
                self.buy(qty=100)
            elif self.bar_idx == 3:
                self.sell(qty=100)

    result = run(S, _ohlcv(5), commission=fee)
    assert len(fees_paid) == 2
    assert result["total_commission"] == pytest.approx(1.0)


def test_no_overlap_signals_when_position_held():
    from src.strategies.strategy_template_base import StrategyTemplate, run

    class S(StrategyTemplate):
        def init(self): pass
        def next(self):
            self.buy(qty=10)

    result = run(S, _ohlcv(5))
    assert result["fills"] == 1


def test_eod_close_flushes_position():
    from src.strategies.strategy_template_base import StrategyTemplate, run

    class S(StrategyTemplate):
        def init(self): pass
        def next(self):
            if self.bar_idx == 0:
                self.buy(qty=10)

    result = run(S, _ohlcv(3), close_at_eod=True)
    assert result["fills"] == 2
    assert result["position_at_end"] == 0
