"""Phase 2 §2.6: one max-hold resolution shared by backtest and live."""
from __future__ import annotations

import os
from unittest.mock import patch

from execution import regime_param_resolver as rpr
import backtest.unified_backtest as ub


def test_gate_off_returns_default():
    with patch.dict(os.environ, {'OPENCLAW_BACKTEST_COUPLED_RECS': '0'}):
        assert rpr.configured_max_hold_days('S_x') == 21
        assert rpr.configured_max_hold_days('S_x', default=30) == 30


def test_gate_on_takes_max_over_regimes():
    vals = {'LOW_VOL': 10, 'TRANSITIONING': None, 'HIGH_VOL': 25, 'CRISIS': 5}
    with (
        patch.dict(os.environ, {'OPENCLAW_BACKTEST_COUPLED_RECS': '1'}),
        patch.object(rpr, 'max_hold_days_override', side_effect=lambda sid, r: vals[r]),
    ):
        assert rpr.configured_max_hold_days('S_x') == 25


def test_gate_on_no_values_returns_default_and_failure_logs():
    with (
        patch.dict(os.environ, {'OPENCLAW_BACKTEST_COUPLED_RECS': '1'}),
        patch.object(rpr, 'max_hold_days_override', return_value=None),
    ):
        assert rpr.configured_max_hold_days('S_x') == 21
    seen = []
    with (
        patch.dict(os.environ, {'OPENCLAW_BACKTEST_COUPLED_RECS': '1'}),
        patch.object(rpr, 'max_hold_days_override', side_effect=RuntimeError('db down')),
    ):
        assert rpr.configured_max_hold_days('S_x', log=seen.append) == 21
    assert seen and 'db down' in seen[0]


def test_backtest_delegates_to_shared_helper():
    with patch.object(rpr, 'configured_max_hold_days', return_value=17) as m:
        assert ub._configured_max_hold_days('S_x') == 17
    m.assert_called_once()
    assert m.call_args.args[0] == 'S_x'
