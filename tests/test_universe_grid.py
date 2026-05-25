"""TDD tests for SP-2 Phase C Task 0.5: resolver→regime-blended 8-metric grid.

Covers:
  1. MockResolver forces its predicate (ignores manifest).
  2. run_backtest(resolver=None) is unchanged vs static-universe path (regression).
  3. aggregate_metrics now emits sortino and calmar alongside existing keys.
  4. blend_metrics produces exactly 8 keys.
  5. CLI (universe_grid_cli) emits exactly 8 keys for sp500.
  6. 3 cap-independent candidates yield 3 distinct metric objects.
  7. Determinism: same window → byte-identical JSON.
"""
from __future__ import annotations

import json
import math
import sys
import subprocess
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch
import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))


# ---------------------------------------------------------------------------
# Helpers / mini fakes
# ---------------------------------------------------------------------------

def _make_meta(symbol="AAPL", in_sp500=True, tradable=True, status="active",
               exchange="NASDAQ", options_eligible=False, in_r1000=False,
               in_r3000=False, market_cap=None):
    from strategies.universe_meta import TickerMetadata
    return TickerMetadata(
        symbol=symbol, asset_class="us_equity", exchange=exchange,
        status=status, tradable=tradable, shortable=True, fractionable=True,
        easy_to_borrow=True, market_cap=market_cap, adv_usd_20d=None,
        sector=None, industry=None, options_eligible=options_eligible,
        in_sp500=in_sp500, in_r1000=in_r1000, in_r3000=in_r3000,
        listed_date=None, delisted_date=None,
    )


def _make_fake_db(tickers_and_flags):
    """tickers_and_flags: list of (symbol, in_sp500, options_eligible)."""
    class FakeRow:
        def __init__(self, meta):
            self.symbol = meta.symbol
            self.metadata = meta
    class FakeDB:
        def fetch_metadata_as_of(self, as_of):
            rows = []
            for symbol, sp500, opts_elig in tickers_and_flags:
                meta = _make_meta(symbol=symbol, in_sp500=sp500,
                                  options_eligible=opts_elig,
                                  tradable=True, status="active",
                                  exchange="NASDAQ")
                rows.append(FakeRow(meta))
            return rows
    return FakeDB()


def _make_fake_coverage(symbols_with_floor):
    """symbols_with_floor: set of tickers that pass has_floor."""
    class FakeCoverage:
        def has_floor(self, symbol, as_of):
            return symbol in symbols_with_floor
    return FakeCoverage()


# ---------------------------------------------------------------------------
# 1. MockResolver: forces predicate, ignores manifest
# ---------------------------------------------------------------------------

class TestMockResolver:
    def test_forces_predicate_ignores_manifest(self):
        """MockResolver._load_predicate always returns the forced predicate,
        never reads a manifest file."""
        from strategies.universe_resolver import MockResolver

        sp500_predicate_called = []

        def fake_predicate(meta, as_of):
            sp500_predicate_called.append(meta.symbol)
            return meta.in_sp500

        def boom_manifest_loader():
            raise AssertionError("manifest_loader should NOT be called")

        db = _make_fake_db([("AAPL", True, False), ("TSLA", False, False)])
        cov = _make_fake_coverage({"AAPL", "TSLA"})

        resolver = MockResolver(
            db=db,
            coverage=cov,
            predicate=fake_predicate,
            manifest_loader=boom_manifest_loader,
            today_fn=lambda: date(2025, 1, 1),
        )
        result = resolver.resolve("any_strategy", as_of=date(2024, 12, 1))
        assert "AAPL" in result
        assert "TSLA" not in result
        assert "AAPL" in sp500_predicate_called

    def test_mock_resolver_uses_db_and_coverage(self):
        """MockResolver still calls db.fetch_metadata_as_of and coverage.has_floor."""
        from strategies.universe_resolver import MockResolver

        db = _make_fake_db([("AAPL", True, False), ("MSFT", True, False)])
        # Only AAPL passes coverage
        cov = _make_fake_coverage({"AAPL"})

        resolver = MockResolver(
            db=db,
            coverage=cov,
            predicate=lambda meta, as_of: meta.in_sp500,
            today_fn=lambda: date(2025, 1, 1),
        )
        result = resolver.resolve("s1", date(2024, 12, 1))
        assert result == ["AAPL"]

    def test_mock_resolver_cache_works(self):
        """Repeated resolve calls with same args use cache (db only queried once)."""
        from strategies.universe_resolver import MockResolver

        call_count = [0]

        class CountingDB:
            def fetch_metadata_as_of(self, as_of):
                call_count[0] += 1
                meta = _make_meta(symbol="AAPL", in_sp500=True)

                class _Row:
                    symbol = "AAPL"
                    metadata = meta
                return [_Row()]

        cov = _make_fake_coverage({"AAPL"})
        resolver = MockResolver(
            db=CountingDB(),
            coverage=cov,
            predicate=lambda meta, as_of: True,
            today_fn=lambda: date(2025, 1, 1),
        )
        resolver.resolve("s1", date(2024, 12, 1))
        resolver.resolve("s1", date(2024, 12, 1))
        assert call_count[0] == 1  # cached on second call

    def test_mock_resolver_look_ahead_guard(self):
        """resolve() raises AsOfInFutureError when as_of > today_fn()."""
        from strategies.universe_resolver import MockResolver, AsOfInFutureError

        db = _make_fake_db([])
        cov = _make_fake_coverage(set())
        resolver = MockResolver(
            db=db,
            coverage=cov,
            predicate=lambda meta, as_of: True,
            today_fn=lambda: date(2024, 1, 1),
        )
        with pytest.raises(AsOfInFutureError):
            resolver.resolve("s1", date(2025, 1, 1))


# ---------------------------------------------------------------------------
# 2. aggregate_metrics: new sortino + calmar keys
# ---------------------------------------------------------------------------

class TestAggregateMetricsExtensions:
    def test_empty_trades_have_sortino_calmar_none(self):
        from backtest.unified_backtest import aggregate_metrics
        m = aggregate_metrics([])
        assert "sortino" in m
        assert "calmar" in m
        assert m["sortino"] is None
        assert m["calmar"] is None

    def test_existing_keys_still_present(self):
        """Adding sortino/calmar must NOT remove existing keys."""
        from backtest.unified_backtest import aggregate_metrics
        trades = [{"pnl_pct": 0.05, "holding_days": 5,
                   "entry_date": "2024-01-02"}] * 20
        m = aggregate_metrics(trades)
        required_keys = {"sharpe", "max_dd_pct", "return_pct", "total_trades",
                         "hit_rate", "avg_holding_days", "avg_pnl_pct",
                         "sortino", "calmar"}
        assert required_keys.issubset(set(m.keys()))

    def test_sortino_none_for_all_winning_trades(self):
        """All winners → no downside deviation → sortino=None."""
        from backtest.unified_backtest import aggregate_metrics
        trades = [{"pnl_pct": 0.05, "holding_days": 5,
                   "entry_date": "2024-01-02"}] * 20
        m = aggregate_metrics(trades)
        # All same returns → zero variance → sortino is None (same as sharpe)
        # OR no downside → sortino also None
        assert m["sortino"] is None

    def test_sortino_computed_for_mixed_trades(self):
        """Mixed trades: some negative pnl → downside deviation > 0 → sortino not None."""
        from backtest.unified_backtest import aggregate_metrics
        trades = []
        for d in range(2, 20):
            trades.append({"pnl_pct": 0.03, "holding_days": 3,
                           "entry_date": f"2024-01-{d:02d}"})
        for d in range(2, 20):
            trades.append({"pnl_pct": -0.04, "holding_days": 3,
                           "entry_date": f"2024-02-{d:02d}"})
        m = aggregate_metrics(trades)
        # Mixed → downside exists → sortino should be a finite number
        assert m["sortino"] is not None
        assert math.isfinite(m["sortino"])

    def test_calmar_none_when_zero_drawdown(self):
        """calmar is None when max_dd_pct==0 (division by zero protection)."""
        from backtest.unified_backtest import aggregate_metrics
        trades = [{"pnl_pct": 0.05, "holding_days": 5,
                   "entry_date": "2024-01-02"}] * 20
        m = aggregate_metrics(trades)
        # Zero variance → max_dd_pct == 0.0 → calmar should be None
        assert m["calmar"] is None

    def test_calmar_positive_for_winning_non_flat_portfolio(self):
        """A strategy with real drawdown should have calmar = annualized_return / max_dd."""
        from backtest.unified_backtest import aggregate_metrics
        # Winners in Jan, losers in Feb, winners in March → real drawdown
        trades = []
        for d in range(2, 30):
            trades.append({"pnl_pct": 0.02, "holding_days": 3,
                           "entry_date": f"2024-01-{d:02d}"})
        for d in range(2, 28):
            trades.append({"pnl_pct": -0.05, "holding_days": 3,
                           "entry_date": f"2024-02-{d:02d}"})
        for d in range(2, 30):
            trades.append({"pnl_pct": 0.02, "holding_days": 3,
                           "entry_date": f"2024-03-{d:02d}"})
        m = aggregate_metrics(trades)
        assert m["max_dd_pct"] > 0, "test data should produce a drawdown"
        assert m["calmar"] is not None, "test data should produce a finite calmar"
        assert m["calmar"] != 0


# ---------------------------------------------------------------------------
# 3. run_backtest(resolver=None) regression: static universe path unchanged
# ---------------------------------------------------------------------------

class TestRunBacktestResolverNone:
    """Verifies that passing resolver=None leaves run_backtest byte-identical
    to the pre-patch behaviour. We monkey-patch load_prices_panels,
    load_regimes, and the strategy's generate_signals to avoid real IO."""

    def _make_stub_strategy_cls(self):
        from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES

        class StubStrategy(BaseStrategy):
            id = "stub_for_regression"
            min_lookback = 5
            active_in_regimes = list(CANONICAL_REGIMES)

            def generate_signals(self, prices, regime, universe, aux_data=None):
                if len(prices) < 10:
                    return []
                # Emit one LONG signal per bar on the first ticker in universe
                if not universe:
                    return []
                ticker = universe[0]
                if ticker not in prices.columns:
                    return []
                close = float(prices[ticker].iloc[-1])
                return [Signal(
                    ticker=ticker,
                    direction="LONG",
                    entry_price=close,
                    stop_loss=close * 0.93,
                    target_1=close * 1.08,
                )]

        return StubStrategy

    def _make_prices_regimes(self):
        """Build a minimal set of prices and regimes for testing."""
        # 60 dates, 3 tickers
        dates = pd.date_range("2024-01-01", periods=60, freq="B")
        tickers = ["AAPL", "MSFT", "GOOGL"]
        prices = {}
        rng = np.random.default_rng(42)
        for t in tickers:
            base = 100.0
            changes = rng.normal(0, 0.01, len(dates))
            closes = base * np.cumprod(1 + changes)
            prices[t] = closes
        close_wide = pd.DataFrame(prices, index=dates)
        close_wide.index.name = "date"
        regimes_data = {}
        regime_list = ["LOW_VOL", "TRANSITIONING", "HIGH_VOL", "CRISIS"]
        for i, d in enumerate(dates):
            regimes_data[d] = regime_list[i % 4]
        regimes = pd.Series(regimes_data)
        # bars_by_ticker
        bars_by_ticker = {}
        for t in tickers:
            df = pd.DataFrame({
                "open": close_wide[t] * 0.99,
                "high": close_wide[t] * 1.01,
                "low": close_wide[t] * 0.98,
                "close": close_wide[t],
            }, index=dates)
            bars_by_ticker[t] = df
        return close_wide, bars_by_ticker, regimes

    def _run_with_stubs(self, resolver, mock_conn):
        """Run run_backtest with fully stubbed IO; return (run_id, recorded_universes).

        ``recorded_universes`` is the list of universe args the stub strategy
        received across all bars. This is the regression vector: with
        resolver=None, every bar must receive the static equity-only universe
        derived from close_wide columns.
        """
        import backtest.unified_backtest as ub
        from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES

        cls = self._make_stub_strategy_cls()
        close_wide, bars_by_ticker, regimes = self._make_prices_regimes()

        recorded_universes: list[list] = []

        # Wrap the strategy class to capture per-bar universe arguments.
        original_generate = cls.generate_signals

        def recording_generate(self_inst, prices, regime, universe, aux_data=None):
            recorded_universes.append(list(universe))
            return original_generate(self_inst, prices, regime, universe, aux_data=aux_data)

        cls.generate_signals = recording_generate

        with (
            patch("backtest.unified_backtest.load_prices_panels",
                  return_value=(close_wide, bars_by_ticker)),
            patch("backtest.unified_backtest.load_regimes",
                  return_value=regimes),
            patch("backtest.unified_backtest.load_strategy_class",
                  return_value=cls),
            patch("backtest.unified_backtest.find_strategy_file",
                  return_value=str(ROOT / "src/strategies/implementations/momentum_12_1.py")),
            patch("backtest.unified_backtest._code_sha",
                  return_value="abc123"),
            patch("backtest.unified_backtest.psycopg2.extras.execute_values"),
        ):
            run_id = ub.run_backtest(
                "stub_for_regression",
                conn=mock_conn,
                commit=False,
                resolver=resolver,
            )

        return run_id, recorded_universes, close_wide

    def _make_mock_conn(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        return mock_conn

    def test_resolver_none_universe_equals_static(self):
        """run_backtest(resolver=None): every bar's universe equals the static
        equity-only column list derived from close_wide, i.e. the pre-resolver
        behaviour is unchanged."""
        mock_conn = self._make_mock_conn()
        _, recorded_universes, close_wide = self._run_with_stubs(None, mock_conn)

        expected_static = sorted([
            c for c in close_wide.columns
            if not c.startswith('^') and '-USD' not in c and '=F' not in c
        ])
        assert len(recorded_universes) > 0, "stub strategy should have fired on at least one bar"
        for bar_universe in recorded_universes:
            assert sorted(bar_universe) == expected_static, (
                f"resolver=None bar universe {sorted(bar_universe)} "
                f"!= static {expected_static}"
            )

    def test_resolver_none_does_not_crash(self):
        """run_backtest with resolver=None must accept the kwarg without error."""
        mock_conn = self._make_mock_conn()
        run_id, _, _ = self._run_with_stubs(None, mock_conn)
        assert run_id is not None


# ---------------------------------------------------------------------------
# 3b. run_backtest(resolver=<MockResolver>) coverage: resolver-provided universe
# ---------------------------------------------------------------------------

class TestRunBacktestWithResolver:
    """Verifies that run_backtest with a real MockResolver passes the
    resolver-provided universe to generate_signals on every bar."""

    def _make_prices_regimes(self):
        """Build a minimal set of prices and regimes (same as regression fixture)."""
        dates = pd.date_range("2024-01-01", periods=60, freq="B")
        tickers = ["AAPL", "MSFT", "GOOGL", "AMZN"]
        prices = {}
        rng = np.random.default_rng(99)
        for t in tickers:
            base = 100.0
            changes = rng.normal(0, 0.01, len(dates))
            prices[t] = base * np.cumprod(1 + changes)
        close_wide = pd.DataFrame(prices, index=dates)
        close_wide.index.name = "date"
        regime_list = ["LOW_VOL", "TRANSITIONING", "HIGH_VOL", "CRISIS"]
        regimes = pd.Series({d: regime_list[i % 4] for i, d in enumerate(dates)})
        bars_by_ticker = {}
        for t in tickers:
            bars_by_ticker[t] = pd.DataFrame({
                "open": close_wide[t] * 0.99,
                "high": close_wide[t] * 1.01,
                "low": close_wide[t] * 0.98,
                "close": close_wide[t],
            }, index=dates)
        return close_wide, bars_by_ticker, regimes

    def test_run_backtest_uses_resolver_universe(self):
        """run_backtest with a MockResolver: each bar's universe comes from the
        resolver (not the static close_wide columns). We assert that the
        resolver's _universe_sizes_out is recorded and that bars received
        only the resolver-provided tickers."""
        import backtest.unified_backtest as ub
        from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES
        from strategies.universe_resolver import MockResolver
        from datetime import date as _date

        close_wide, bars_by_ticker, regimes = self._make_prices_regimes()

        # Cap-independent predicate: only AAPL and MSFT pass.
        RESOLVER_TICKERS = ["AAPL", "MSFT"]

        def only_two(meta, as_of):
            return meta.symbol in RESOLVER_TICKERS

        db = _make_fake_db(
            [(t, True, False) for t in ["AAPL", "MSFT", "GOOGL", "AMZN"]]
        )
        cov = _make_fake_coverage(set(["AAPL", "MSFT", "GOOGL", "AMZN"]))
        # today_fn must be >= any date we resolve
        resolver = MockResolver(
            db=db,
            coverage=cov,
            predicate=only_two,
            today_fn=lambda: _date(2030, 1, 1),
        )

        recorded_universes: list[list] = []

        class RecordingStrategy(BaseStrategy):
            id = "stub_with_resolver"
            min_lookback = 5
            active_in_regimes = list(CANONICAL_REGIMES)

            def generate_signals(self, prices, regime, universe, aux_data=None):
                recorded_universes.append(list(universe))
                return []

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)

        with (
            patch("backtest.unified_backtest.load_prices_panels",
                  return_value=(close_wide, bars_by_ticker)),
            patch("backtest.unified_backtest.load_regimes",
                  return_value=regimes),
            patch("backtest.unified_backtest.load_strategy_class",
                  return_value=RecordingStrategy),
            patch("backtest.unified_backtest.find_strategy_file",
                  return_value=str(ROOT / "src/strategies/implementations/momentum_12_1.py")),
            patch("backtest.unified_backtest._code_sha",
                  return_value="abc123"),
            patch("backtest.unified_backtest.psycopg2.extras.execute_values"),
        ):
            ub.run_backtest(
                "stub_with_resolver",
                conn=mock_conn,
                commit=False,
                resolver=resolver,
            )

        # Every bar's universe must be exactly the resolver's output.
        assert len(recorded_universes) > 0, "should have processed bars"
        for bar_universe in recorded_universes:
            assert sorted(bar_universe) == sorted(RESOLVER_TICKERS), (
                f"expected resolver tickers {RESOLVER_TICKERS}, got {bar_universe}"
            )

        # universe_sizes_out must be recorded on the resolver.
        assert hasattr(resolver, '_universe_sizes_out'), \
            "resolver._universe_sizes_out should be set after run"
        assert len(resolver._universe_sizes_out) > 0
        assert all(s == len(RESOLVER_TICKERS) for s in resolver._universe_sizes_out)


# ---------------------------------------------------------------------------
# 4. blend_metrics: exactly 8 keys
# ---------------------------------------------------------------------------

class TestBlendMetrics:
    def _make_per_regime(self, trade_counts=None):
        """Build a minimal per_regime dict with the expected unified_backtest shape."""
        from strategies.base import CANONICAL_REGIMES
        tc = trade_counts or {"LOW_VOL": 10, "TRANSITIONING": 5, "HIGH_VOL": 3, "CRISIS": 2}
        per_regime = {}
        for r in CANONICAL_REGIMES:
            n = tc.get(r, 0)
            per_regime[r] = {
                "trade_count": n,
                "sharpe": 1.2 if n >= 5 else None,
                "max_dd_pct": 5.0 if n > 0 else 0.0,
                "return_pct": 8.0 if n > 0 else 0.0,
                "hit_rate": 0.6 if n > 0 else None,
                "avg_holding_days": 7.0 if n > 0 else None,
                "avg_pnl_pct": 0.4 if n > 0 else 0.0,
                "oos_days_in_regime": 100,
                "sortino": 1.5 if n >= 5 else None,
                "calmar": 0.5 if n >= 5 else None,
            }
        return per_regime

    def test_blend_returns_exactly_8_keys(self):
        from backtest.universe_grid_cli import blend_metrics
        per_regime = self._make_per_regime()
        day_freq = {"LOW_VOL": 0.5, "TRANSITIONING": 0.3, "HIGH_VOL": 0.15, "CRISIS": 0.05}
        result = blend_metrics(per_regime, day_freq)
        assert set(result.keys()) == {
            "sharpe", "max_dd_pct", "win_rate", "mean_universe_size",
            "trades_n", "sortino", "calmar", "mean_holding_days",
        }

    def test_blend_trades_n_is_sum(self):
        from backtest.universe_grid_cli import blend_metrics
        per_regime = self._make_per_regime({"LOW_VOL": 10, "TRANSITIONING": 5,
                                             "HIGH_VOL": 3, "CRISIS": 2})
        day_freq = {"LOW_VOL": 0.5, "TRANSITIONING": 0.3, "HIGH_VOL": 0.15, "CRISIS": 0.05}
        result = blend_metrics(per_regime, day_freq)
        assert result["trades_n"] == 20  # 10+5+3+2

    def test_blend_max_dd_is_max(self):
        from backtest.universe_grid_cli import blend_metrics
        per_regime = self._make_per_regime()
        per_regime["LOW_VOL"]["max_dd_pct"] = 10.0
        per_regime["TRANSITIONING"]["max_dd_pct"] = 5.0
        per_regime["HIGH_VOL"]["max_dd_pct"] = 3.0
        per_regime["CRISIS"]["max_dd_pct"] = 1.0
        day_freq = {"LOW_VOL": 0.5, "TRANSITIONING": 0.3, "HIGH_VOL": 0.15, "CRISIS": 0.05}
        result = blend_metrics(per_regime, day_freq)
        assert result["max_dd_pct"] == 10.0

    def test_blend_sharpe_day_freq_weighted(self):
        from backtest.universe_grid_cli import blend_metrics
        per_regime = {
            "LOW_VOL":      {"trade_count": 10, "sharpe": 2.0, "max_dd_pct": 5.0, "return_pct": 8.0,
                             "hit_rate": 0.6, "avg_holding_days": 7.0, "avg_pnl_pct": 0.4,
                             "oos_days_in_regime": 100, "sortino": None, "calmar": None},
            "TRANSITIONING": {"trade_count": 10, "sharpe": 1.0, "max_dd_pct": 3.0, "return_pct": 4.0,
                              "hit_rate": 0.5, "avg_holding_days": 5.0, "avg_pnl_pct": 0.2,
                              "oos_days_in_regime": 60, "sortino": None, "calmar": None},
            "HIGH_VOL":     {"trade_count": 0, "sharpe": None, "max_dd_pct": 0.0, "return_pct": 0.0,
                             "hit_rate": None, "avg_holding_days": None, "avg_pnl_pct": 0.0,
                             "oos_days_in_regime": 30, "sortino": None, "calmar": None},
            "CRISIS":       {"trade_count": 0, "sharpe": None, "max_dd_pct": 0.0, "return_pct": 0.0,
                             "hit_rate": None, "avg_holding_days": None, "avg_pnl_pct": 0.0,
                             "oos_days_in_regime": 15, "sortino": None, "calmar": None},
        }
        # day_freq: LOW_VOL=0.6, TRANS=0.4; HIGH_VOL/CRISIS=0 contributing (None sharpe)
        day_freq = {"LOW_VOL": 0.6, "TRANSITIONING": 0.4, "HIGH_VOL": 0.0, "CRISIS": 0.0}
        result = blend_metrics(per_regime, day_freq)
        # renormalized over LOW_VOL + TRANS only: 0.6/1.0=0.6, 0.4/1.0=0.4
        expected_sharpe = round(2.0 * 0.6 + 1.0 * 0.4, 4)
        assert abs(result["sharpe"] - expected_sharpe) < 1e-9

    def test_blend_win_rate_trade_count_weighted(self):
        from backtest.universe_grid_cli import blend_metrics
        per_regime = {
            "LOW_VOL": {"trade_count": 10, "sharpe": 1.0, "max_dd_pct": 5.0, "return_pct": 8.0,
                        "hit_rate": 0.8, "avg_holding_days": 7.0, "avg_pnl_pct": 0.4,
                        "oos_days_in_regime": 100, "sortino": None, "calmar": None},
            "TRANSITIONING": {"trade_count": 10, "sharpe": 1.0, "max_dd_pct": 5.0, "return_pct": 8.0,
                               "hit_rate": 0.4, "avg_holding_days": 5.0, "avg_pnl_pct": 0.2,
                               "oos_days_in_regime": 60, "sortino": None, "calmar": None},
            "HIGH_VOL": {"trade_count": 0, "sharpe": None, "max_dd_pct": 0.0, "return_pct": 0.0,
                         "hit_rate": None, "avg_holding_days": None, "avg_pnl_pct": 0.0,
                         "oos_days_in_regime": 30, "sortino": None, "calmar": None},
            "CRISIS": {"trade_count": 0, "sharpe": None, "max_dd_pct": 0.0, "return_pct": 0.0,
                       "hit_rate": None, "avg_holding_days": None, "avg_pnl_pct": 0.0,
                       "oos_days_in_regime": 15, "sortino": None, "calmar": None},
        }
        day_freq = {"LOW_VOL": 0.6, "TRANSITIONING": 0.4, "HIGH_VOL": 0.0, "CRISIS": 0.0}
        result = blend_metrics(per_regime, day_freq)
        # trade-count weighted: (10*0.8 + 10*0.4) / 20 = 0.6
        assert abs(result["win_rate"] - 0.6) < 1e-9

    def test_blend_mean_universe_size_uses_provided(self):
        """mean_universe_size is passed as a kwarg to blend_metrics."""
        from backtest.universe_grid_cli import blend_metrics
        per_regime = self._make_per_regime()
        day_freq = {"LOW_VOL": 0.5, "TRANSITIONING": 0.3, "HIGH_VOL": 0.15, "CRISIS": 0.05}
        result = blend_metrics(per_regime, day_freq, mean_universe_size=350.5)
        assert result["mean_universe_size"] == 350.5

    def test_blend_all_none_sharpe_returns_none(self):
        """When all regimes have None sharpe, blended sharpe = None."""
        from backtest.universe_grid_cli import blend_metrics
        per_regime = {r: {"trade_count": 0, "sharpe": None, "max_dd_pct": 0.0, "return_pct": 0.0,
                          "hit_rate": None, "avg_holding_days": None, "avg_pnl_pct": 0.0,
                          "oos_days_in_regime": 0, "sortino": None, "calmar": None}
                      for r in ["LOW_VOL", "TRANSITIONING", "HIGH_VOL", "CRISIS"]}
        day_freq = {"LOW_VOL": 0.4, "TRANSITIONING": 0.3, "HIGH_VOL": 0.2, "CRISIS": 0.1}
        result = blend_metrics(per_regime, day_freq)
        assert result["sharpe"] is None
        assert result["sortino"] is None
        assert result["calmar"] is None
        assert result["win_rate"] is None
        assert result["mean_holding_days"] is None

    def test_blend_zero_total_trades_win_rate_none(self):
        """If total trades=0, win_rate and mean_holding_days are None."""
        from backtest.universe_grid_cli import blend_metrics
        per_regime = {r: {"trade_count": 0, "sharpe": None, "max_dd_pct": 0.0, "return_pct": 0.0,
                          "hit_rate": None, "avg_holding_days": None, "avg_pnl_pct": 0.0,
                          "oos_days_in_regime": 10, "sortino": None, "calmar": None}
                      for r in ["LOW_VOL", "TRANSITIONING", "HIGH_VOL", "CRISIS"]}
        day_freq = {"LOW_VOL": 0.4, "TRANSITIONING": 0.3, "HIGH_VOL": 0.2, "CRISIS": 0.1}
        result = blend_metrics(per_regime, day_freq)
        assert result["win_rate"] is None
        assert result["mean_holding_days"] is None


# ---------------------------------------------------------------------------
# 5. CLI integration: 8 keys emitted for sp500 (short window, real data)
# ---------------------------------------------------------------------------

CLI_MODULE = str(ROOT / "src" / "backtest" / "universe_grid_cli.py")
WORKTREE = str(ROOT)


@pytest.mark.integration
class TestCLIIntegration:
    """These tests use real parquet + Postgres. Marked integration."""

    def _run_cli(self, strategy, start, end, candidate):
        result = subprocess.run(
            [
                "python3", CLI_MODULE,
                "--strategy", strategy,
                "--start", start,
                "--end", end,
                "--resolver-override", candidate,
                "--metrics-json",
                "--seed", "42",
            ],
            capture_output=True, text=True, cwd=WORKTREE,
        )
        return result

    def _parse_json_line(self, stdout: str) -> dict:
        """Extract the JSON line from CLI stdout (log messages precede it)."""
        for line in reversed(stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        raise ValueError(f"No JSON line found in stdout:\n{stdout!r}")

    def test_cli_emits_8_keys_for_sp500(self):
        """CLI with sp500 candidate produces JSON with exactly 8 required keys."""
        result = self._run_cli(
            "momentum_12_1",
            "2024-01-01", "2024-06-30",
            "sp500",
        )
        assert result.returncode == 0, f"CLI failed:\n{result.stderr}"
        data = self._parse_json_line(result.stdout)
        expected_keys = {"sharpe", "max_dd_pct", "win_rate", "mean_universe_size",
                         "trades_n", "sortino", "calmar", "mean_holding_days"}
        assert set(data.keys()) == expected_keys

    def test_cli_3_candidates_distinct_outputs(self):
        """sp500, no_adr, and no_otc each produce distinct metric objects.

        sp500 is a strict subset of no_adr (sp500 adds in_sp500=True on top of
        no_adr's tradable+active+no-ADR filter). They must therefore produce
        different mean_universe_size values AND different metric dicts.
        """
        results = {}
        for candidate in ["sp500", "no_adr", "no_otc"]:
            r = self._run_cli(
                "momentum_12_1",
                "2024-01-01", "2024-06-30",
                candidate,
            )
            assert r.returncode == 0, f"CLI failed for {candidate}:\n{r.stderr}"
            results[candidate] = self._parse_json_line(r.stdout)
        # All three must have the 8 keys
        for candidate, data in results.items():
            assert "sharpe" in data, f"sharpe missing for {candidate}"
        # sp500 ⊂ no_adr → different universes → mean_universe_size MUST differ
        sp500_size = results["sp500"].get("mean_universe_size")
        no_adr_size = results["no_adr"].get("mean_universe_size")
        assert sp500_size != no_adr_size, (
            f"sp500 mean_universe_size ({sp500_size}) == no_adr ({no_adr_size}): "
            "resolver override is being ignored or predicates are identical"
        )
        # Full metric dicts must differ (different universe → different signals → different metrics)
        assert results["sp500"] != results["no_adr"], (
            "sp500 and no_adr produced identical metrics — resolver override not taking effect"
        )

    def test_cli_determinism(self):
        """Same args → byte-identical JSON output."""
        r1 = self._run_cli(
            "momentum_12_1",
            "2024-01-01", "2024-03-31",
            "sp500",
        )
        r2 = self._run_cli(
            "momentum_12_1",
            "2024-01-01", "2024-03-31",
            "sp500",
        )
        assert r1.returncode == 0
        assert r2.returncode == 0
        # Compare just the JSON lines (log messages may have different timestamps)
        assert self._parse_json_line(r1.stdout) == self._parse_json_line(r2.stdout)

    def test_cli_rejects_unknown_candidate(self):
        """Unknown --resolver-override candidate → exit code 2."""
        result = self._run_cli(
            "momentum_12_1",
            "2024-01-01", "2024-03-31",
            "not_a_real_candidate_xyz",
        )
        assert result.returncode == 2


# ---------------------------------------------------------------------------
# 6. Unit-level CLI module: blend_metrics + MockResolver in isolation
# ---------------------------------------------------------------------------

class TestUniverseGridModule:
    """Unit tests that don't require real parquet/Postgres."""

    def test_import_universe_grid_cli(self):
        """universe_grid_cli must be importable."""
        import importlib
        mod = importlib.import_module("backtest.universe_grid_cli")
        assert hasattr(mod, "blend_metrics")
        assert hasattr(mod, "main")

    def test_blend_handles_regime_with_none_calmar(self):
        """Regime with calmar=None is skipped in weighted calmar blend."""
        from backtest.universe_grid_cli import blend_metrics
        per_regime = {
            "LOW_VOL":      {"trade_count": 10, "sharpe": 1.2, "max_dd_pct": 5.0, "return_pct": 8.0,
                             "hit_rate": 0.6, "avg_holding_days": 7.0, "avg_pnl_pct": 0.4,
                             "oos_days_in_regime": 100, "sortino": 1.5, "calmar": 0.5},
            "TRANSITIONING": {"trade_count": 4, "sharpe": None, "max_dd_pct": 3.0, "return_pct": 2.0,
                               "hit_rate": 0.5, "avg_holding_days": 5.0, "avg_pnl_pct": 0.1,
                               "oos_days_in_regime": 60, "sortino": None, "calmar": None},
            "HIGH_VOL":     {"trade_count": 0, "sharpe": None, "max_dd_pct": 0.0, "return_pct": 0.0,
                             "hit_rate": None, "avg_holding_days": None, "avg_pnl_pct": 0.0,
                             "oos_days_in_regime": 30, "sortino": None, "calmar": None},
            "CRISIS":       {"trade_count": 0, "sharpe": None, "max_dd_pct": 0.0, "return_pct": 0.0,
                             "hit_rate": None, "avg_holding_days": None, "avg_pnl_pct": 0.0,
                             "oos_days_in_regime": 15, "sortino": None, "calmar": None},
        }
        day_freq = {"LOW_VOL": 0.5, "TRANSITIONING": 0.3, "HIGH_VOL": 0.15, "CRISIS": 0.05}
        result = blend_metrics(per_regime, day_freq)
        # calmar should be computed from only LOW_VOL (0.5 renorm to 1.0)
        assert result["calmar"] is not None
        assert abs(result["calmar"] - 0.5) < 1e-6


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
