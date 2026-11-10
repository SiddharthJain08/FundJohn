"""Crypto engine Phase 3 Task 2: S_m for BTC-USD on the crypto regime
partition, annualised on 365 days (rf per calendar day = annual / 365).

Synthetic only — the crypto regime series is monkeypatched on the
strategies.crypto_historical_regimes module (date-indexed Series, 'UNKNOWN'
outside the span, exactly the real shape) so load_table never touches
data/master; the BTC closes loader is monkeypatched on benchmark_baseline.
"""
from __future__ import annotations

import math
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import benchmark_baseline as bb          # noqa: E402
from strategies import crypto_historical_regimes as chr_  # noqa: E402

START = date(2024, 1, 1)
N_DAYS = 200


def _fixture():
    rng = np.random.default_rng(7)
    days = [START + timedelta(days=i) for i in range(N_DAYS)]
    rets = rng.normal(0.002, 0.03, N_DAYS)
    closes, p = {}, 100.0
    for d, r in zip(days, rets):
        p *= (1.0 + r)
        closes[d.isoformat()] = p
    # alternate 10-day blocks LOW_VOL / HIGH_VOL; the last 5 days are outside the table span
    labels = {d: ('LOW_VOL' if (i // 10) % 2 == 0 else 'HIGH_VOL') for i, d in enumerate(days[:-5])}
    return days, closes, labels


@pytest.fixture
def crypto_fixture(monkeypatch):
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'const')
    days, closes, labels = _fixture()
    seen = {}

    def fake_regime_series(dates):
        keys = pd.to_datetime(list(dates)).date
        seen['n'] = len(keys)
        return pd.Series([labels.get(k, 'UNKNOWN') for k in keys], index=keys, name='regime')

    def no_table():
        raise AssertionError('load_table must never be reached in tests')

    monkeypatch.setattr(chr_, 'regime_series', fake_regime_series)
    monkeypatch.setattr(chr_, 'load_table', no_table)
    monkeypatch.setattr(bb, 'load_benchmark_closes',
                        lambda s, e, b: {k: v for k, v in closes.items()} if b == 'BTC-USD' else {})

    def equity_tags(*a, **k):
        raise AssertionError('crypto source must not read historical_regimes.parquet')
    monkeypatch.setattr(bb, 'load_regime_tags', equity_tags)
    return days, closes, labels, seen


def _direct(days, closes, labels, regime, ann):
    ds = [d.isoformat() for d in days]
    xs = []
    for i in range(1, len(ds)):
        if labels.get(days[i - 1]) == regime:
            xs.append(closes[ds[i]] / closes[ds[i - 1]] - 1.0)
    r = np.asarray(xs)
    return float((r - 0.05 / ann).mean() / r.std(ddof=1) * math.sqrt(ann)), len(xs)


def test_sharpe_by_horizon_crypto_uses_365_and_crypto_partition(crypto_fixture):
    days, closes, labels, seen = crypto_fixture
    out = bb.regime_benchmark_sharpe_by_horizon(START.isoformat(), days[-1].isoformat(),
                                                benchmark='BTC-USD', record_shadow=False,
                                                regime_source='crypto')
    assert seen['n'] > 0
    for regime in ('LOW_VOL', 'HIGH_VOL'):
        exp365, n = _direct(days, closes, labels, regime, 365)
        exp252, _ = _direct(days, closes, labels, regime, 252)
        assert n >= 40
        assert out[regime][1] == pytest.approx(exp365, rel=1e-9)
        assert out[regime][1] != pytest.approx(exp252, rel=1e-6)
    # regimes absent from the crypto partition are None; UNKNOWN tail never tags
    assert out['CRISIS'][1] is None and out['TRANSITIONING'][1] is None
    assert set(out['LOW_VOL']) == set(bb.BENCH_HORIZONS)


def test_crypto_trading_days_override_and_equity_default_untouched(crypto_fixture):
    days, closes, labels, _ = crypto_fixture
    out = bb.regime_benchmark_sharpe_by_horizon(START.isoformat(), days[-1].isoformat(),
                                                benchmark='BTC-USD', record_shadow=False,
                                                regime_source='crypto', trading_days=252,
                                                horizons=(1,))
    exp252, _ = _direct(days, closes, labels, 'LOW_VOL', 252)
    assert out['LOW_VOL'][1] == pytest.approx(exp252, rel=1e-9)


def test_unknown_regime_source_fails_open(crypto_fixture):
    assert bb.regime_benchmark_sharpe_by_horizon('2024-01-01', '2024-07-01', benchmark='BTC-USD',
                                                 regime_source='bogus', record_shadow=False) == {}
