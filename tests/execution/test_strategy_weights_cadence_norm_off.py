# tests/execution/test_strategy_weights_cadence_norm_off.py
"""D2 (2026-08-29 spec): daily_weight = effective_sharpe. The √hold divisor is
the REVERT path behind OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM=1."""
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import execution.strategy_weights as sw  # noqa: E402


def test_default_no_normalization(monkeypatch):
    monkeypatch.delenv(sw.CADENCE_WEIGHT_NORM_ENV, raising=False)
    w, dw = sw._regime_weight(2.0, 21.0)
    assert (w, dw) == (2.0, 2.0)


def test_revert_flag_restores_sqrt_hold(monkeypatch):
    monkeypatch.setenv(sw.CADENCE_WEIGHT_NORM_ENV, '1')
    w, dw = sw._regime_weight(2.0, 21.0)
    assert w == 2.0
    assert dw == 2.0 / math.sqrt(21.0)


def test_hold_floor_still_applies_under_revert(monkeypatch):
    monkeypatch.setenv(sw.CADENCE_WEIGHT_NORM_ENV, '1')
    assert sw._regime_weight(1.5, 0.2) == (1.5, 1.5)   # floored at 1 day
