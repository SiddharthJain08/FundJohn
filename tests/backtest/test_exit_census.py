# tests/backtest/test_exit_census.py
"""A3 (spec 2026-09-12 §1): every run records what its exits actually were and
what the modelled cost took out. Pure functions — no DB, no parquet."""
from __future__ import annotations

import pytest

import backtest.unified_backtest as ub

THREE = [
    {'ticker': 'AAA', 'exit_reason': 'stop',   'pnl_pct': -0.020, 'holding_days': 3},
    {'ticker': 'BBB', 'exit_reason': 'target', 'pnl_pct': +0.050, 'holding_days': 9},
    {'ticker': 'AAA', 'exit_reason': 'stop',   'pnl_pct': -0.040, 'holding_days': 5},
]


class TestCensus:
    def test_three_trade_census(self):
        out = ub.exit_reason_census(THREE)
        assert set(out) == {'stop', 'target'}
        assert out['stop'] == {'n': 2, 'mean_pnl_pct': pytest.approx(-0.030),
                               'median_hold_days': pytest.approx(4.0)}
        assert out['target'] == {'n': 1, 'mean_pnl_pct': pytest.approx(0.050),
                                 'median_hold_days': pytest.approx(9.0)}

    def test_hook_reasons_keep_their_prefix(self):
        out = ub.exit_reason_census(
            THREE + [{'ticker': 'CCC', 'exit_reason': 'strategy_exit:pair_decohered',
                      'pnl_pct': 0.01, 'holding_days': 2}])
        assert out['strategy_exit:pair_decohered']['n'] == 1

    def test_non_finite_pnl_counts_in_n_but_not_in_the_mean(self):
        out = ub.exit_reason_census(
            THREE + [{'ticker': 'DDD', 'exit_reason': 'stop',
                      'pnl_pct': float('nan'), 'holding_days': 1}])
        assert out['stop']['n'] == 3
        assert out['stop']['mean_pnl_pct'] == pytest.approx(-0.030)

    def test_missing_reason_is_unknown_and_empty_is_empty(self):
        assert ub.exit_reason_census([{'ticker': 'AAA', 'pnl_pct': 0.0, 'holding_days': 1}]) \
            == {'unknown': {'n': 1, 'mean_pnl_pct': 0.0, 'median_hold_days': 1.0}}
        assert ub.exit_reason_census([]) == {}
        assert ub.exit_reason_census(None) == {}


class TestCostDrag:
    def test_flat_bps_over_three_trades(self):
        # cost_i = 2 * 10 / 1e4 = 0.002 each => sum 0.006
        # gross_i = pnl_i + 0.002 => -0.018, +0.052, -0.038 => sum|.| = 0.108
        got = ub.cost_drag_bps(THREE, flat_bps=10.0)
        assert got == pytest.approx(1e4 * 0.006 / 0.108, rel=1e-9)

    def test_per_ticker_map_overrides_the_flat_fallback(self):
        got = ub.cost_drag_bps(THREE, cost_bps_by_ticker={'AAA': 30.0}, flat_bps=10.0)
        # AAA 0.006 + AAA 0.006 + BBB 0.002 = 0.014
        # gross: -0.014, +0.052, -0.034 => 0.100
        assert got == pytest.approx(1e4 * 0.014 / 0.100, rel=1e-9)

    def test_zero_cost_is_zero_not_none(self):
        assert ub.cost_drag_bps(THREE, flat_bps=0.0) == pytest.approx(0.0)

    def test_no_finite_trades_is_none(self):
        assert ub.cost_drag_bps([], flat_bps=10.0) is None
        assert ub.cost_drag_bps(None, flat_bps=10.0) is None
        assert ub.cost_drag_bps([{'ticker': 'AAA', 'pnl_pct': float('nan')}], flat_bps=10.0) is None

    def test_zero_gross_denominator_is_none_not_a_zero_division(self):
        # pnl exactly cancels the cost => gross 0 for every trade
        trades = [{'ticker': 'AAA', 'pnl_pct': -0.002, 'holding_days': 1}]
        assert ub.cost_drag_bps(trades, flat_bps=10.0) is None
