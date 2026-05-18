"""Tests for alpaca_executor._recompute_bracket_from_quote.

The recompute re-anchors entry/stop/target to the current market quote
while preserving the signal's stop_pct + target_pct shape, so:
  - the bracket never violates Alpaca's $0.01 base_price rule (which
    was producing ~30 daily 422 rejections under the old snap-only path)
  - the strategy's risk/reward intent is preserved when the market has
    drifted between signal generation and submission
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.alpaca_executor import (  # noqa: E402
    _recompute_bracket_from_quote,
    MIN_BRACKET_GAP_PCT,
)


class TestLongRecompute:
    def test_preserves_ratios_when_base_above_entry(self):
        # Signal: entry=100, stop=95 (5% stop), target=110 (10% target)
        # Market drift: base=110
        ne, ns, nt, why = _recompute_bracket_from_quote(100.0, 95.0, 110.0, 'buy', 110.0)
        assert ne == 110.0
        assert ns == pytest.approx(104.5, rel=1e-3)   # 110 × (1 - 0.05)
        assert nt == pytest.approx(121.0, rel=1e-3)   # 110 × (1 + 0.10)
        assert why == ''

    def test_preserves_ratios_when_base_below_entry(self):
        # Market drift down: signal entry=100 stop=95 target=110, base=80
        ne, ns, nt, why = _recompute_bracket_from_quote(100.0, 95.0, 110.0, 'buy', 80.0)
        assert ne == 80.0
        assert ns == pytest.approx(76.0, rel=1e-3)    # 80 × 0.95
        assert nt == pytest.approx(88.0, rel=1e-3)    # 80 × 1.10
        assert why == ''

    def test_minimum_gap_floor(self):
        # Pathological signal: entry=100, stop=99.99 (≈0.01% stop)
        # MIN_BRACKET_GAP_PCT = 0.5% should override.
        ne, ns, nt, why = _recompute_bracket_from_quote(100.0, 99.99, 100.01, 'buy', 100.0)
        assert ne == 100.0
        # min gap floor → 0.5% on each side
        assert ns == pytest.approx(100.0 * (1 - MIN_BRACKET_GAP_PCT), rel=1e-3)
        assert nt == pytest.approx(100.0 * (1 + MIN_BRACKET_GAP_PCT), rel=1e-3)

    def test_inverted_stop_falls_back_to_min_gap(self):
        # Bug signal: long with stop ABOVE entry. Recompute should detect
        # and fall back to min gap on the stop side; target ratio preserved.
        ne, ns, nt, why = _recompute_bracket_from_quote(100.0, 105.0, 110.0, 'buy', 100.0)
        assert ne == 100.0
        assert ns == pytest.approx(100.0 * (1 - MIN_BRACKET_GAP_PCT))  # min gap
        assert nt == pytest.approx(110.0)                              # 10% target preserved
        assert 'inverted stop' in why

    def test_inverted_target_falls_back_to_min_gap(self):
        # Bug signal: long with target BELOW entry.
        ne, ns, nt, why = _recompute_bracket_from_quote(100.0, 95.0, 99.0, 'buy', 100.0)
        assert ns == pytest.approx(95.0)                               # 5% stop preserved
        assert nt == pytest.approx(100.0 * (1 + MIN_BRACKET_GAP_PCT))  # min gap
        assert 'inverted target' in why

    def test_50pct_outer_clamp_on_stop(self):
        # Pathological signal: entry=100, stop=-50 (150% stop). Clamp to 50%.
        ne, ns, nt, why = _recompute_bracket_from_quote(100.0, -50.0, 110.0, 'buy', 100.0)
        assert ns == pytest.approx(50.0)                               # 100 × (1 - 0.5)
        assert 'stop_pct clamped' in why


class TestShortRecompute:
    def test_preserves_ratios_when_base_below_entry(self):
        # Short signal: entry=100, stop=105 (5% stop ABOVE entry),
        #               target=90 (10% target BELOW entry). Market drifts to 90.
        ne, ns, nt, why = _recompute_bracket_from_quote(100.0, 105.0, 90.0, 'sell', 90.0)
        assert ne == 90.0
        assert ns == pytest.approx(94.5, rel=1e-3)    # 90 × 1.05
        assert nt == pytest.approx(81.0, rel=1e-3)    # 90 × 0.90
        assert why == ''

    def test_preserves_ratios_when_base_above_entry(self):
        # Short signal where market drifted UP against the position.
        ne, ns, nt, why = _recompute_bracket_from_quote(100.0, 105.0, 90.0, 'sell', 120.0)
        assert ne == 120.0
        assert ns == pytest.approx(126.0, rel=1e-3)   # 120 × 1.05
        assert nt == pytest.approx(108.0, rel=1e-3)   # 120 × 0.90

    def test_minimum_gap_floor_short(self):
        ne, ns, nt, why = _recompute_bracket_from_quote(100.0, 100.01, 99.99, 'sell', 100.0)
        assert ns == pytest.approx(100.0 * (1 + MIN_BRACKET_GAP_PCT), rel=1e-3)
        assert nt == pytest.approx(100.0 * (1 - MIN_BRACKET_GAP_PCT), rel=1e-3)

    def test_inverted_short_stop(self):
        # Short with stop BELOW entry (bug). Fall back to min gap.
        ne, ns, nt, why = _recompute_bracket_from_quote(100.0, 95.0, 90.0, 'sell', 100.0)
        assert ns == pytest.approx(100.0 * (1 + MIN_BRACKET_GAP_PCT))
        assert 'inverted stop' in why


class TestDegenerateInputs:
    def test_nan_input_returns_min_gap(self):
        ne, ns, nt, why = _recompute_bracket_from_quote(
            float('nan'), 95.0, 110.0, 'buy', 100.0
        )
        assert 'non-finite' in why
        # stop/target straddle base with min absolute snap
        assert ns < ne < nt

    def test_zero_base_handled(self):
        ne, ns, nt, why = _recompute_bracket_from_quote(100.0, 95.0, 110.0, 'buy', 0.0)
        # base=0 → fallback; should not crash
        assert isinstance(why, str)
        assert ne == 0.0

    def test_negative_entry(self):
        # Degenerate signal: entry ≤ 0. Fall back to min gap.
        ne, ns, nt, why = _recompute_bracket_from_quote(-5.0, -10.0, -1.0, 'buy', 100.0)
        assert 'degenerate entry' in why
        assert ns == pytest.approx(100.0 * (1 - MIN_BRACKET_GAP_PCT))
        assert nt == pytest.approx(100.0 * (1 + MIN_BRACKET_GAP_PCT))

    def test_non_numeric_input(self):
        ne, ns, nt, why = _recompute_bracket_from_quote(None, 'foo', 110.0, 'buy', 100.0)
        assert 'non-numeric' in why
        # Stop below base, target above for long
        assert ns < ne < nt


class TestAlpacaValidationGuarantee:
    """The recompute output must satisfy Alpaca's bracket rules:
       long:  stop  ≤ base − 0.01  AND  target ≥ base + 0.01
       short: stop  ≥ base + 0.01  AND  target ≤ base − 0.01
    For every valid signal we tested in production, this property must hold.
    """

    @pytest.mark.parametrize('base', [10.0, 100.0, 500.0, 1500.0, 5000.0])
    @pytest.mark.parametrize('stop_pct', [0.001, 0.005, 0.01, 0.05, 0.10, 0.20])
    @pytest.mark.parametrize('target_pct', [0.001, 0.005, 0.01, 0.05, 0.10, 0.30])
    def test_long_bracket_passes_alpaca_validation(self, base, stop_pct, target_pct):
        entry = base * 1.1   # signal taken at 10% above current market
        stop  = entry * (1 - stop_pct)
        target = entry * (1 + target_pct)
        _, ns, nt, _ = _recompute_bracket_from_quote(entry, stop, target, 'buy', base)
        assert ns <= base - 0.01, f'long stop {ns} must be ≤ {base - 0.01}'
        assert nt >= base + 0.01, f'long target {nt} must be ≥ {base + 0.01}'

    @pytest.mark.parametrize('base', [10.0, 100.0, 500.0, 1500.0, 5000.0])
    @pytest.mark.parametrize('stop_pct', [0.001, 0.005, 0.01, 0.05, 0.10, 0.20])
    @pytest.mark.parametrize('target_pct', [0.001, 0.005, 0.01, 0.05, 0.10, 0.30])
    def test_short_bracket_passes_alpaca_validation(self, base, stop_pct, target_pct):
        entry = base * 0.9
        stop  = entry * (1 + stop_pct)
        target = entry * (1 - target_pct)
        _, ns, nt, _ = _recompute_bracket_from_quote(entry, stop, target, 'sell', base)
        assert ns >= base + 0.01, f'short stop {ns} must be ≥ {base + 0.01}'
        assert nt <= base - 0.01, f'short target {nt} must be ≤ {base - 0.01}'
