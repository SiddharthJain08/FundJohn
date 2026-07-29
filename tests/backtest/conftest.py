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
