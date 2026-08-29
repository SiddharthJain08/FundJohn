"""tests/backtest/test_benchmark_baseline.py — src/backtest/benchmark_baseline.py
(regime_benchmark_sharpe): a regime-conditioned SPY baseline.

2026-08-29 (benchmark-relative sizing spec, D1): the benchmark leg that used
to gate promotion/activation/eligibility on this value was REMOVED — see
tests/backtest/test_no_benchmark_gate.py. This module and its Sharpe output
stay: SPY's regime Sharpe is now a sizing input only.

Synthetic parquet fixtures only, written under tmp_path — NEVER touches real
data/master/*.parquet. ZZT-prefixed ticker for the synthetic benchmark
symbol, matching this repo's synthetic-test-ticker convention (AAA is a
real ETF; ZZT is not a real symbol). Loaders are swapped via monkeypatch on
benchmark_baseline's module-level path constants / functions, per that
module's "loaders as separate functions so tests monkeypatch them" design.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import benchmark_baseline as bb              # noqa: E402

BENCHMARK_TICKER = 'ZZT_SPY'


def _write_regimes(path, dates, regimes):
    pd.DataFrame({
        'date': [d.date() for d in dates],
        'vix': 15.0,
        'vix_smoothed': 15.0,
        'regime': regimes,
    }).to_parquet(path)


def _write_prices(path, dates, closes, ticker=BENCHMARK_TICKER):
    pd.DataFrame({
        'ticker': ticker,
        'date': [d.strftime('%Y-%m-%d') for d in dates],
        'open': closes, 'high': closes, 'low': closes, 'close': closes,
        'volume': 1000.0, 'vwap': closes, 'transactions': 10.0,
        'source': 'synthetic',
    }).to_parquet(path)


def _alternating_closes(n, r=0.01, start=100.0):
    """Deterministic close series whose daily returns alternate +r/-r.
    Arithmetic mean of an alternating +r/-r series is exactly 0 over an
    EVEN number of terms; an odd count (e.g. a block whose leading return
    was dropped at a window boundary) leaves a small residual. Tests below
    treat "benchmark flat" loosely (see test docstring) for exactly this
    reason — only the gate OUTCOMES are asserted exactly."""
    price = start
    closes = [price]
    for i in range(1, n):
        step = r if i % 2 == 1 else -r
        price = price * (1 + step)
        closes.append(price)
    return closes


# ── Amendment 1 (spec docs/specs/2026-08-29-bench-sizing-amendment-1-spec.md §1) ──
def _excess_sharpe_ref(xs):
    """Independent reference: excess (rf 5 %/252) annualized Sharpe, ddof=1."""
    n = len(xs)
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return (m - 0.05 / 252) / sd * math.sqrt(252)


@pytest.fixture
def alternating_252(tmp_path, monkeypatch):
    """252 business days, six alternating 42-day LOW_VOL/HIGH_VOL blocks
    (TRANSITIONING/CRISIS never tagged); ZZT_SPY alternates +-1% daily."""
    dates = pd.bdate_range('2020-01-02', periods=252)
    block = 42
    labels = ['LOW_VOL', 'HIGH_VOL']
    regimes = [labels[(i // block) % 2] for i in range(len(dates))]
    closes = _alternating_closes(len(dates))
    regimes_path = tmp_path / 'historical_regimes.parquet'
    prices_path = tmp_path / 'prices.parquet'
    _write_regimes(regimes_path, dates, regimes)
    _write_prices(prices_path, dates, closes)
    monkeypatch.setattr(bb, 'REGIMES_PARQUET', str(regimes_path))
    monkeypatch.setattr(bb, 'PRICES_PARQUET', str(prices_path))
    return dates, closes, regimes


@pytest.fixture
def thin_crisis_fixture(tmp_path, monkeypatch):
    """60 days: first 10 tagged CRISIS (-> 9 usable close-to-close returns,
    well under min_obs=40), remaining 50 tagged LOW_VOL (-> 50 usable
    returns, clears min_obs)."""
    dates = pd.bdate_range('2021-01-04', periods=60)
    regimes = ['CRISIS'] * 10 + ['LOW_VOL'] * 50
    regimes_path = tmp_path / 'historical_regimes.parquet'
    prices_path = tmp_path / 'prices.parquet'
    _write_regimes(regimes_path, dates, regimes)
    _write_prices(prices_path, dates, _alternating_closes(len(dates), r=0.005))
    monkeypatch.setattr(bb, 'REGIMES_PARQUET', str(regimes_path))
    monkeypatch.setattr(bb, 'PRICES_PARQUET', str(prices_path))
    return dates


# ── regime_benchmark_sharpe ──────────────────────────────────────────────────

def _ref_h1(closes, regimes, regime):
    """Reference H=1 statistic: the return INTO the day after each tagged close."""
    rets = [None] + [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
    xs = [rets[i + 1] for i in range(len(closes) - 1) if regimes[i] == regime]
    return _excess_sharpe_ref(xs)


def test_regime_benchmark_sharpe_matches_reference_and_untagged_are_none(alternating_252):
    dates, closes, regimes = alternating_252
    out = bb.regime_benchmark_sharpe(dates[0], dates[-1], benchmark=BENCHMARK_TICKER, min_obs=40)
    assert out['LOW_VOL'] == pytest.approx(_ref_h1(closes, regimes, 'LOW_VOL'))
    assert out['HIGH_VOL'] == pytest.approx(_ref_h1(closes, regimes, 'HIGH_VOL'))
    # Never tagged in this fixture -> 0 mark-days -> thin -> None.
    assert out['TRANSITIONING'] is None
    assert out['CRISIS'] is None


def test_thin_regime_10_tagged_days_is_none(thin_crisis_fixture):
    dates = thin_crisis_fixture
    out = bb.regime_benchmark_sharpe(dates[0], dates[-1], benchmark=BENCHMARK_TICKER, min_obs=40)
    assert out['CRISIS'] is None          # 10 tagged days -> 9 usable returns < min_obs
    assert out['LOW_VOL'] is not None     # 50 tagged days -> 50 usable returns >= min_obs


def test_load_failure_returns_empty_dict(monkeypatch):
    def _raise(*a, **kw):
        raise RuntimeError('synthetic parquet read failure')
    monkeypatch.setattr(bb, 'load_regime_tags', _raise)
    assert bb.regime_benchmark_sharpe('2020-01-01', '2020-12-31', benchmark=BENCHMARK_TICKER) == {}


def test_load_failure_in_prices_loader_also_returns_empty_dict(monkeypatch):
    def _raise(*a, **kw):
        raise RuntimeError('synthetic prices read failure')
    monkeypatch.setattr(bb, 'load_benchmark_closes', _raise)
    assert bb.regime_benchmark_sharpe('2020-01-01', '2020-12-31', benchmark=BENCHMARK_TICKER) == {}


# ── Amendment 1 (spec docs/specs/2026-08-29-bench-sizing-amendment-1-spec.md §1) ──


@pytest.fixture
def two_block_8(tmp_path, monkeypatch):
    """8 business days d0..d7; d0..d3 LOW_VOL, d4..d7 HIGH_VOL; close-to-close
    returns r1..r7 = +1, -1, +2, -2, +3, -3, +4 % (r_i is the return INTO d_i)."""
    dates = pd.bdate_range('2022-01-03', periods=8)
    rets = [None, .01, -.01, .02, -.02, .03, -.03, .04]
    closes = [100.0]
    for r in rets[1:]:
        closes.append(closes[-1] * (1 + r))
    regimes = ['LOW_VOL'] * 4 + ['HIGH_VOL'] * 4
    regimes_path = tmp_path / 'historical_regimes.parquet'
    prices_path = tmp_path / 'prices.parquet'
    _write_regimes(regimes_path, dates, regimes)
    _write_prices(prices_path, dates, closes)
    monkeypatch.setattr(bb, 'REGIMES_PARQUET', str(regimes_path))
    monkeypatch.setattr(bb, 'PRICES_PARQUET', str(prices_path))
    return dates, rets


def test_by_horizon_h1_is_the_next_day_return_set(two_block_8):
    dates, rets = two_block_8
    out = bb.regime_benchmark_sharpe_by_horizon(dates[0], dates[-1], benchmark=BENCHMARK_TICKER,
                                                min_obs=1, horizons=(1, 2, 21))
    # LOW_VOL entries at the closes of d0..d3 -> H=1 mark-days d1..d4 -> r1..r4
    assert out['LOW_VOL'][1] == pytest.approx(_excess_sharpe_ref(rets[1:5]))
    # H=2 -> mark-days d1..d5 (union of overlapping 2-day lots)
    assert out['LOW_VOL'][2] == pytest.approx(_excess_sharpe_ref(rets[1:6]))
    # H=21 truncates at end of data -> d1..d7
    assert out['LOW_VOL'][21] == pytest.approx(_excess_sharpe_ref(rets[1:8]))
    # HIGH_VOL entries d4..d7 -> H=1 mark-days d5..d7 (d8 does not exist)
    assert out['HIGH_VOL'][1] == pytest.approx(_excess_sharpe_ref(rets[5:8]))
    # never tagged -> None at every H, but the regime keys are still present
    assert out['TRANSITIONING'] == {1: None, 2: None, 21: None}
    assert out['CRISIS'] == {1: None, 2: None, 21: None}


def test_forward_h1_differs_from_the_old_contemporaneous_statistic(two_block_8):
    dates, rets = two_block_8
    out = bb.regime_benchmark_sharpe_by_horizon(dates[0], dates[-1], benchmark=BENCHMARK_TICKER,
                                                min_obs=1, horizons=(1,))
    # pre-amendment: returns ON the tagged days d1..d3 (d0 has no return) = r1..r3
    contemporaneous = _excess_sharpe_ref(rets[1:4])
    assert out['LOW_VOL'][1] != pytest.approx(contemporaneous)


def test_flat_wrapper_returns_the_h1_column(two_block_8):
    dates, _ = two_block_8
    by_h = bb.regime_benchmark_sharpe_by_horizon(dates[0], dates[-1], benchmark=BENCHMARK_TICKER, min_obs=1)
    flat = bb.regime_benchmark_sharpe(dates[0], dates[-1], benchmark=BENCHMARK_TICKER, min_obs=1)
    assert set(flat) == set(bb.CANONICAL_REGIMES)
    for r in bb.CANONICAL_REGIMES:
        assert flat[r] == by_h[r][bb.DEFAULT_HORIZON]
    assert bb.DEFAULT_HORIZON == 1 and bb.BENCH_HORIZONS == (1, 2, 3, 5, 10, 21)


def test_min_obs_counts_mark_days_per_horizon(two_block_8):
    dates, _ = two_block_8
    # LOW_VOL has 4 mark-days at H=1 and 5 at H=2: min_obs=5 nulls H=1 only
    out = bb.regime_benchmark_sharpe_by_horizon(dates[0], dates[-1], benchmark=BENCHMARK_TICKER,
                                                min_obs=5, horizons=(1, 2))
    assert out['LOW_VOL'][1] is None and out['LOW_VOL'][2] is not None


def test_by_horizon_load_failure_returns_empty_dict(monkeypatch):
    def _raise(*a, **kw):
        raise RuntimeError('synthetic parquet read failure')
    monkeypatch.setattr(bb, 'load_regime_tags', _raise)
    assert bb.regime_benchmark_sharpe_by_horizon('2020-01-01', '2020-12-31', benchmark=BENCHMARK_TICKER) == {}


def test_risk_free_constant_matches_the_engine():
    from backtest import unified_backtest as ub
    from execution import trade_handoff_builder as thb
    assert bb.RISK_FREE_ANNUAL == 0.05
    assert bb.RISK_FREE_DAILY == pytest.approx(ub.RISK_FREE_DAILY)
    assert bb.RISK_FREE_DAILY == pytest.approx(thb.RISK_FREE_DAILY)

