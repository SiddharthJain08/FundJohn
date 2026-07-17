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


def _ohlcv_trend(n=30, base=100.0, step=1.0):
    """Always-up OHLCV: each bar's close = base + i*step, open = previous close."""
    idx = pd.date_range("2026-01-02", periods=n, freq="1D", tz="America/New_York")
    closes = [base + i * step for i in range(n)]
    opens = [base] + closes[:-1]
    highs = [c + 0.5 for c in closes]
    lows = [o - 0.5 for o in opens]
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": 1000,
    }, index=idx)


def test_total_return_positive_for_winning_strategy():
    """Buy-and-hold on a synthetic always-up series → positive total_return_pct."""
    from src.strategies.strategy_template_base import StrategyTemplate, run

    class BuyAndHold(StrategyTemplate):
        def init(self): pass
        def next(self):
            if self.bar_idx == 0:
                self.buy(qty=100)

    # 30 bars trending +$1/bar, no commission, no EOD close — equity rises with price.
    result = run(BuyAndHold, _ohlcv_trend(30), initial_equity=100_000.0)
    assert result["total_return_pct"] > 0, (
        f"expected positive return on always-up series, got {result['total_return_pct']:.4f}%"
    )


def test_max_drawdown_negative_or_zero():
    """max_drawdown_pct must always be <= 0 (peak-to-trough decline)."""
    from src.strategies.strategy_template_base import StrategyTemplate, run

    class BuyAndHold(StrategyTemplate):
        def init(self): pass
        def next(self):
            if self.bar_idx == 0:
                self.buy(qty=10)

    # Flat OHLCV → equity flat → drawdown == 0.
    flat_result = run(BuyAndHold, _ohlcv(10), initial_equity=100_000.0)
    assert flat_result["max_drawdown_pct"] <= 0.0

    # Trending OHLCV → equity strictly rising → drawdown still <= 0 (== 0 here).
    up_result = run(BuyAndHold, _ohlcv_trend(20), initial_equity=100_000.0)
    assert up_result["max_drawdown_pct"] <= 0.0


def test_win_rate_100_when_all_trades_profitable():
    """Strategy that buys, holds for a few bars, sells higher — every round-trip wins."""
    from src.strategies.strategy_template_base import StrategyTemplate, run

    class BuySellRepeat(StrategyTemplate):
        def init(self): pass
        def next(self):
            # Buy on bars 0, 6, 12; sell on bars 3, 9, 15.
            # On _ohlcv_trend the price keeps climbing, so each round-trip is a win.
            if self.bar_idx in (0, 6, 12):
                self.buy(qty=10)
            elif self.bar_idx in (3, 9, 15):
                self.sell(qty=10)

    result = run(BuySellRepeat, _ohlcv_trend(20), initial_equity=100_000.0)
    assert result["n_trades"] == 3, f"expected 3 round-trips, got {result['n_trades']}"
    assert result["win_rate_pct"] == 100.0, (
        f"expected 100% win-rate on monotonically-rising prices, "
        f"got {result['win_rate_pct']:.1f}% (PnLs visible via fills_log)"
    )
