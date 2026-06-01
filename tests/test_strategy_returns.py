"""Tests for strategy_returns — differencing cumulative marks to daily returns."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import strategy_returns as sr  # noqa: E402


def test_difference_marks_first_day_is_level_from_zero():
    # marks: list of (date_str, cumulative_unrealized_pct, realized_or_none)
    marks = [('2026-05-01', 0.02, None), ('2026-05-02', 0.05, None), ('2026-05-03', 0.04, None)]
    out = sr.difference_signal_marks(marks)
    assert abs(out['2026-05-01'] - 0.02) < 1e-9
    assert abs(out['2026-05-02'] - 0.03) < 1e-9
    assert abs(out['2026-05-03'] - (-0.01)) < 1e-9


def test_difference_marks_close_uses_realized_minus_last_unrealized():
    marks = [('2026-05-01', 0.02, None), ('2026-05-02', 0.05, 0.06)]  # closed on day 2
    out = sr.difference_signal_marks(marks)
    # day1 delta = 0.02; close-day delta = realized(0.06) - last_unrealized(0.02) = 0.04
    assert out['2026-05-01'] == 0.02
    assert abs(out['2026-05-02'] - 0.04) < 1e-9


def test_aggregate_equal_weight_across_open_signals():
    # two signals' per-date daily delta -> strategy daily return = mean across signals present that day
    per_signal = {
        'sigA': {'2026-05-01': 0.02, '2026-05-02': 0.04},
        'sigB': {'2026-05-02': -0.02},
    }
    out = sr.aggregate_strategy_daily(per_signal)
    assert out['2026-05-01'] == 0.02            # only A present
    assert abs(out['2026-05-02'] - 0.01) < 1e-9  # mean(0.04, -0.02)
