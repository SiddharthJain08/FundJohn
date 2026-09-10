"""BaseStrategy.compute_stops_and_targets — target geometry (2026-09-10).

Legacy ('flat'): t1/t2/t3 = ±5 % / ±10 % / ±20 % of price regardless of vol,
while the stop is 2×ATR14 × REGIME_ATR_SCALE. That made reward:risk INVERSELY
proportional to vol (SPY 6:1, a 3 %-ATR name 0.8:1) — 143/156 strategies inherit
it. New ('atr_r'): targets are R-multiples of the SAME stop distance, so the
geometry is fixed by construction and scales with vol and regime.
"""
import math
import numpy as np
import pandas as pd
import pytest

from strategies.base import BaseStrategy, REGIME_ATR_SCALE, TARGET_MODE_ENV


class _S(BaseStrategy):
    id = 'T'
    def generate_signals(self, *a, **k):  # pragma: no cover - abstract stub
        return []


class _S_custom_r(_S):
    target_r_multiples = (1.5, 3.0, 6.0)


def _series(step=1.0, n=40, start=100.0):
    # Alternating ±step closes -> mean |diff| == step exactly.
    vals = [start + (step if i % 2 else 0.0) for i in range(n)]
    return pd.Series(vals, dtype=float)


def _px(s):
    return float(s.iloc[-1])


def test_flat_mode_is_legacy_byte_identical(monkeypatch):
    monkeypatch.setenv(TARGET_MODE_ENV, 'flat')
    s = _series(step=1.0)
    px = _px(s)
    out = _S().compute_stops_and_targets(s, 'LONG', px)
    assert out['t1'] == round(px * 1.05, 4)
    assert out['t2'] == round(px * 1.10, 4)
    assert out['t3'] == round(px * 1.20, 4)
    assert out['stop'] == round(px - 1.0 * 2.0, 4)
    sh = _S().compute_stops_and_targets(s, 'SHORT', px)
    assert sh['t1'] == round(px * 0.95, 4) and sh['stop'] == round(px + 2.0, 4)


def test_default_mode_is_flat_until_fleet_flip(monkeypatch):
    # Rollout discipline: the new geometry goes live only via the gated flip
    # (scripts/target_mode_flip_after_fleet.sh) once the fleet is re-gated.
    monkeypatch.delenv(TARGET_MODE_ENV, raising=False)
    s = _series(step=1.0)
    px = _px(s)
    assert _S().compute_stops_and_targets(s, 'LONG', px)['t1'] == round(px * 1.05, 4)


def test_atr_r_long_targets_are_r_multiples_of_stop_distance(monkeypatch):
    monkeypatch.setenv(TARGET_MODE_ENV, 'atr_r')
    s = _series(step=1.0)                        # ATR == 1.0
    px = _px(s)
    out = _S().compute_stops_and_targets(s, 'LONG', px, atr_multiplier=2.0,
                                         regime_state='LOW_VOL')
    d = 1.0 * 2.0 * REGIME_ATR_SCALE['LOW_VOL']   # stop distance
    assert out['stop'] == round(px - d, 4)
    assert out['t1'] == round(px + 2.0 * d, 4)
    assert out['t2'] == round(px + 4.0 * d, 4)
    assert out['t3'] == round(px + 8.0 * d, 4)


def test_atr_r_short_is_mirrored(monkeypatch):
    monkeypatch.setenv(TARGET_MODE_ENV, 'atr_r')
    s = _series(step=1.0)
    px = _px(s)
    out = _S().compute_stops_and_targets(s, 'SHORT', px, atr_multiplier=2.0)
    d = 2.0
    assert out['stop'] == round(px + d, 4)
    assert out['t1'] == round(px - 2.0 * d, 4)
    assert out['t2'] == round(px - 4.0 * d, 4)
    assert out['t3'] == round(px - 8.0 * d, 4)


def test_atr_r_geometry_is_regime_invariant(monkeypatch):
    # REGIME_ATR_SCALE tightens stop AND targets together: t1 gap / stop gap
    # is the same R in every regime (the old flat target broke this).
    monkeypatch.setenv(TARGET_MODE_ENV, 'atr_r')
    s = _series(step=1.0)
    px = _px(s)
    for regime in REGIME_ATR_SCALE:
        out = _S().compute_stops_and_targets(s, 'LONG', px, regime_state=regime)
        stop_gap = px - out['stop']
        assert stop_gap > 0
        assert math.isclose((out['t1'] - px) / stop_gap, 2.0, rel_tol=1e-6)


def test_atr_r_explicit_bull_bear_target_still_wins_t3(monkeypatch):
    monkeypatch.setenv(TARGET_MODE_ENV, 'atr_r')
    s = _series(step=1.0)
    px = _px(s)
    assert _S().compute_stops_and_targets(s, 'LONG', px, bull_target=123.4)['t3'] == 123.4
    assert _S().compute_stops_and_targets(s, 'SHORT', px, bear_target=80.0)['t3'] == 80.0


def test_atr_r_per_strategy_override(monkeypatch):
    monkeypatch.setenv(TARGET_MODE_ENV, 'atr_r')
    s = _series(step=1.0)
    px = _px(s)
    out = _S_custom_r().compute_stops_and_targets(s, 'LONG', px)
    d = 2.0
    assert out['t1'] == round(px + 1.5 * d, 4)
    assert out['t2'] == round(px + 3.0 * d, 4)
    assert out['t3'] == round(px + 6.0 * d, 4)


def test_atr_r_nan_atr_fallback_two_percent(monkeypatch):
    monkeypatch.setenv(TARGET_MODE_ENV, 'atr_r')
    s = pd.Series([100.0] * 5)                  # too short for ATR14 -> NaN -> 2 %
    out = _S().compute_stops_and_targets(s, 'LONG', 100.0)
    d = 100.0 * 0.02 * 2.0
    assert out['stop'] == round(100.0 - d, 4)
    assert out['t1'] == round(100.0 + 2.0 * d, 4)


def test_unknown_mode_raises(monkeypatch):
    monkeypatch.setenv(TARGET_MODE_ENV, 'bogus')
    with pytest.raises(ValueError):
        _S().compute_stops_and_targets(_series(), 'LONG', 100.0)
