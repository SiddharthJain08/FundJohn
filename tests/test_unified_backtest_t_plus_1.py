"""Tests for the signal[t] -> execute[t+1] fill model in unified_backtest.

Covers the pure bracket re-anchor helper and the _per_bar_simulate fill/exit
behavior (next-bar-close fill overriding strategy entry_price, pct-shape
bracket re-anchor, entry_date=t+1 / entry_regime=t stamping, last-bar skip,
and coupling-override composition).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub  # noqa: E402


class TestReanchorBracket:
    def test_long_preserves_pct_distances(self):
        # ref=100, stop=93 (7% below), target=108 (8% above); new fill=110
        stop, target = ub._reanchor_bracket(
            ref=100.0, entry_price=110.0, direction=1,
            stop_ref=93.0, target_ref=108.0)
        assert abs(stop - 110.0 * 0.93) < 1e-9
        assert abs(target - 110.0 * 1.08) < 1e-9

    def test_short_preserves_pct_distances(self):
        # short: ref=100, stop=107 (7% above), target=92 (8% below); new fill=90
        stop, target = ub._reanchor_bracket(
            ref=100.0, entry_price=90.0, direction=-1,
            stop_ref=107.0, target_ref=92.0)
        assert abs(stop - 90.0 * 1.07) < 1e-9
        assert abs(target - 90.0 * 0.92) < 1e-9

    def test_identity_when_fill_equals_ref(self):
        stop, target = ub._reanchor_bracket(
            ref=100.0, entry_price=100.0, direction=1,
            stop_ref=95.0, target_ref=110.0)
        assert abs(stop - 95.0) < 1e-9
        assert abs(target - 110.0) < 1e-9
