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
    production options-surface, vol-indices, or corporate-actions masters.
    Point all three env overrides at non-existent tmp paths (a test that needs
    a fixture master sets the env itself, which wins) and clear the
    per-process caches so nothing cached from another test leaks through.

    The rate source is pinned too (final review M3): the synthetic engine now
    passes `as_of` to `risk_free.rf_annual_asof`, so under
    OPENCLAW_RF_SOURCE=macro these tests would read the production
    macro.parquet — 'const' keeps them hermetic and deterministic."""
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE_PATH', str(tmp_path / 'no_surface.parquet'))
    monkeypatch.setenv('OPENCLAW_VOL_INDICES_PARQUET', str(tmp_path / 'no_vol_indices.parquet'))
    monkeypatch.setenv('OPENCLAW_CORPORATE_ACTIONS_PARQUET', str(tmp_path / 'no_corporate_actions.parquet'))
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'const')
    from backtest import dividends, risk_free, synthetic_iv, vol_index
    synthetic_iv.clear_cache()
    vol_index._vix9d_series.cache_clear()
    dividends.clear_cache()
    risk_free.clear_cache()
    yield
    synthetic_iv.clear_cache()
    vol_index._vix9d_series.cache_clear()
    dividends.clear_cache()
    risk_free.clear_cache()


@pytest.fixture(autouse=True)
def _isolate_shadow_log(monkeypatch, tmp_path):
    """lib.shadow_log.record() defaults to ROOT/'logs' — the SAME directory
    scripts/rf_flip_after_fleet.sh and scripts/options_surface_flip_after_shadow.sh
    read (logs/rf_shadow.log, logs/options_surface_shadow.log). Route it into
    a per-test tmp dir so backtest tests that exercise the rf_shadow emitters
    (benchmark_baseline, bench_realized) never write spurious lines into a
    live flip-gate log. A test that wants to assert on the file overrides
    this env var itself, which wins (monkeypatch is last-write-wins)."""
    monkeypatch.setenv('OPENCLAW_SHADOW_LOG_DIR', str(tmp_path))
