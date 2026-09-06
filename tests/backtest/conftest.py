"""Suite-wide defaults for tests/backtest.

Fill-timing pin (2026-07-29 same-day pivot): the engine's standing default
became 'same_close' (signal[t] fills at close[t]). The historical suite
asserts t+1 fill geometry that predates the pivot, so the legacy model is
pinned here — mirroring the tests/execution/conftest.py pattern of pinning
production gates OFF and letting behavior-specific tests opt back in.
Tests that exercise the new default (TestRegression/TestSameCloseFill in
test_backtest_fill_model.py) pass fill_model= explicitly or patch the env
themselves, which overrides this pin.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _pin_legacy_fill_model(monkeypatch):
    if 'OPENCLAW_BT_FILL_MODEL' not in os.environ:
        monkeypatch.setenv('OPENCLAW_BT_FILL_MODEL', 'close')


@pytest.fixture(autouse=True)
def _isolate_iv_masters(monkeypatch, tmp_path):
    """Spec 2026-09-06 B.3 / global constraint: backtest tests never read the
    production surface or vol-indices masters. Point both env overrides at
    non-existent tmp paths (a test that needs a fixture master sets the env
    itself, which wins) and clear the per-process caches so nothing cached
    from another test leaks through."""
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE_PATH', str(tmp_path / 'no_surface.parquet'))
    monkeypatch.setenv('OPENCLAW_VOL_INDICES_PARQUET', str(tmp_path / 'no_vol_indices.parquet'))
    from backtest import synthetic_iv, vol_index
    synthetic_iv.clear_cache()
    vol_index._vix9d_series.cache_clear()
    yield
    synthetic_iv.clear_cache()
    vol_index._vix9d_series.cache_clear()
