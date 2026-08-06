"""historical_regimes._load_cached rebuilds a stale cache (2026-08-06 fix).

rebuild_cache() had zero scheduled callers and the master froze 07-22→08-06
while macro.parquet kept advancing. _load_cached must now (a) rebuild when
the cache lags macro's VIX max date, (b) leave a current cache alone, and
(c) fall back to the stale cache rather than raise when a rebuild fails.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import historical_regimes as hr  # noqa: E402


def _write_macro(path, dates):
    pd.DataFrame({
        'date': [str(d) for d in dates],
        'series': ['VIX'] * len(dates),
        'value': [15.0 + i for i in range(len(dates))],
    }).to_parquet(path, index=False)


def _write_cache(path, dates):
    pd.DataFrame({
        'date': [str(d) for d in dates],
        'vix': [15.0] * len(dates),
        'vix_smoothed': [15.0] * len(dates),
        'regime': ['LOW_VOL'] * len(dates),
    }).to_parquet(path, index=False)


@pytest.fixture
def paths(tmp_path, monkeypatch):
    macro = tmp_path / 'macro.parquet'
    cache = tmp_path / 'historical_regimes.parquet'
    monkeypatch.setattr(hr, 'MACRO', macro)
    monkeypatch.setattr(hr, 'CACHE', cache)
    monkeypatch.setattr(hr, '_cache', None)
    return macro, cache


def test_stale_cache_triggers_rebuild(paths):
    macro, cache = paths
    _write_macro(macro, [date(2026, 8, 4), date(2026, 8, 5)])
    _write_cache(cache, [date(2026, 7, 22)])
    df = hr._load_cached()
    assert df['date'].max() == date(2026, 8, 5), \
        'a cache lagging macro VIX must be rebuilt on load'


def test_current_cache_not_rebuilt(paths):
    macro, cache = paths
    _write_macro(macro, [date(2026, 8, 5)])
    _write_cache(cache, [date(2026, 8, 5)])
    calls = []
    orig = hr.rebuild_cache
    hr.rebuild_cache = lambda *a, **k: calls.append(1) or orig(*a, **k)
    try:
        hr._load_cached()
    finally:
        hr.rebuild_cache = orig
    assert not calls, 'a current cache must not trigger a rebuild'


def test_failed_rebuild_falls_back_to_stale_cache(paths, monkeypatch):
    macro, cache = paths
    _write_macro(macro, [date(2026, 8, 5)])
    _write_cache(cache, [date(2026, 7, 22)])
    monkeypatch.setattr(hr, 'rebuild_cache',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    df = hr._load_cached()
    assert df['date'].max() == date(2026, 7, 22), \
        'a failed rebuild must serve the stale cache, not raise'
