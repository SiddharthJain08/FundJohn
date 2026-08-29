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


@pytest.fixture
def alternating_252(tmp_path, monkeypatch):
    """252 business days, six alternating 42-day LOW_VOL/HIGH_VOL blocks
    (TRANSITIONING/CRISIS never tagged); ZZT_SPY alternates +-1% daily."""
    dates = pd.bdate_range('2020-01-02', periods=252)
    block = 42
    labels = ['LOW_VOL', 'HIGH_VOL']
    regimes = [labels[(i // block) % 2] for i in range(len(dates))]
    regimes_path = tmp_path / 'historical_regimes.parquet'
    prices_path = tmp_path / 'prices.parquet'
    _write_regimes(regimes_path, dates, regimes)
    _write_prices(prices_path, dates, _alternating_closes(len(dates)))
    monkeypatch.setattr(bb, 'REGIMES_PARQUET', str(regimes_path))
    monkeypatch.setattr(bb, 'PRICES_PARQUET', str(prices_path))
    return dates


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

def test_regime_benchmark_sharpe_near_zero_both_regimes_and_untagged_are_none(alternating_252):
    dates = alternating_252
    out = bb.regime_benchmark_sharpe(dates[0], dates[-1], benchmark=BENCHMARK_TICKER, min_obs=40)
    assert out['LOW_VOL'] is not None
    assert out['HIGH_VOL'] is not None
    # "benchmark flat (Sharpe ~= 0)" per the task brief -- loose, not exact.
    # An alternating +-r return series cancels to EXACTLY 0 mean only over
    # an EVEN observation count; the first block loses its leading return
    # at the window's left edge, which can leave one block at an odd count
    # (empirically ~0.13 here). Bound generously -- the exact assertions
    # this module cares about are the gate OUTCOMES below, not this number.
    assert abs(out['LOW_VOL']) < 0.5
    assert abs(out['HIGH_VOL']) < 0.5
    # Never tagged in this fixture -> 0 usable returns -> thin -> None.
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

