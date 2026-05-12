"""Unit tests for src/backtest/regime_blended_backtest.py (v2 — strategy_regime_backtests source)."""
from __future__ import annotations
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest.regime_blended_backtest import (  # noqa: E402
    portfolio_per_regime,
    aggregate_across_regimes,
    regime_day_frequency,
    CANONICAL_REGIMES,
    SHARPE_REGRESSION_TOLERANCE_ABS,
    run_walkforward,
)


def _backtest_df(rows):
    """Build a strategy_regime_backtests-shaped DataFrame."""
    cols = ['strategy_id', 'regime_state', 'sharpe', 'max_dd',
            'total_return_pct', 'trade_count', 'oos_days', 'window_count',
            'note', 'declared_regimes']
    return pd.DataFrame(rows, columns=cols)


def _flat_freq() -> dict[str, float]:
    return {r: 0.25 for r in CANONICAL_REGIMES}


def test_eligibility_filter_drops_strategy_in_wrong_regime():
    """A strategy with eligible_regimes=['LOW_VOL'] contributes only to LOW_VOL."""
    df = _backtest_df([
        ('S1', 'LOW_VOL',       2.0, 0.05, 10.0, 100, 200, 1, None, ['LOW_VOL']),
        ('S1', 'HIGH_VOL',      1.0, 0.10,  5.0,  50, 100, 1, None, ['LOW_VOL']),
        ('S2', 'LOW_VOL',       1.5, 0.04,  8.0,  80, 200, 1, None, None),  # no eligibility → passes everywhere
        ('S2', 'HIGH_VOL',      0.5, 0.08,  3.0,  40, 100, 1, None, None),
        ('S1', 'TRANSITIONING', 0.0, 0.0,   0.0,   0,  50, 1, 'not_declared', None),
        ('S1', 'CRISIS',        0.0, 0.0,   0.0,   0,  50, 1, 'not_declared', None),
        ('S2', 'TRANSITIONING', 0.0, 0.0,   0.0,   0,  50, 1, 'not_declared', None),
        ('S2', 'CRISIS',        0.0, 0.0,   0.0,   0,  50, 1, 'not_declared', None),
    ])
    manifest = {'strategies': {
        'S1': {'eligible_regimes': ['LOW_VOL']},
        'S2': {},  # no eligibility — backward-compat: passes
    }}

    prod = portfolio_per_regime(df, manifest, eligibility_filter=False)
    blend = portfolio_per_regime(df, manifest, eligibility_filter=True)

    # Production HIGH_VOL: both S1 and S2 contribute.
    assert prod['HIGH_VOL']['strategy_count'] == 2
    # Blended HIGH_VOL: S1 dropped (not eligible), S2 stays.
    assert blend['HIGH_VOL']['strategy_count'] == 1
    # LOW_VOL: same in both (S1+S2).
    assert prod['LOW_VOL']['strategy_count'] == 2
    assert blend['LOW_VOL']['strategy_count'] == 2


def test_zero_trade_strategies_excluded_from_portfolio():
    """trade_count=0 rows don't contribute even if no note."""
    df = _backtest_df([
        ('S1', 'LOW_VOL', 0.0, 0.0, 0.0,   0, 100, 1, None, None),
        ('S2', 'LOW_VOL', 1.5, 0.04, 5.0, 50, 100, 1, None, None),
        ('S1', 'TRANSITIONING', 0.0, 0.0, 0.0, 0, 0, 1, 'not_declared', None),
        ('S1', 'HIGH_VOL', 0.0, 0.0, 0.0, 0, 0, 1, 'not_declared', None),
        ('S1', 'CRISIS', 0.0, 0.0, 0.0, 0, 0, 1, 'not_declared', None),
        ('S2', 'TRANSITIONING', 0.0, 0.0, 0.0, 0, 0, 1, 'not_declared', None),
        ('S2', 'HIGH_VOL', 0.0, 0.0, 0.0, 0, 0, 1, 'not_declared', None),
        ('S2', 'CRISIS', 0.0, 0.0, 0.0, 0, 0, 1, 'not_declared', None),
    ])
    res = portfolio_per_regime(df, {'strategies': {}}, eligibility_filter=False)
    assert res['LOW_VOL']['strategy_count'] == 1  # only S2 had trades
    assert res['LOW_VOL']['sharpe'] == 1.5


def test_aggregate_across_regimes_weighted():
    """Aggregate respects regime day frequencies."""
    per_regime = {
        'LOW_VOL':       {'sharpe': 2.0, 'max_dd': 0.05, 'total_return_pct': 10.0,
                          'strategy_count': 1, 'trade_count': 100},
        'TRANSITIONING': {'sharpe': 1.0, 'max_dd': 0.07, 'total_return_pct': 5.0,
                          'strategy_count': 1, 'trade_count': 80},
        'HIGH_VOL':      {'sharpe': 0.5, 'max_dd': 0.15, 'total_return_pct': 2.0,
                          'strategy_count': 1, 'trade_count': 30},
        'CRISIS':        {'sharpe': 0.0, 'max_dd': 0.20, 'total_return_pct': 0.0,
                          'strategy_count': 1, 'trade_count': 10},
    }
    freq = {'LOW_VOL': 0.4, 'TRANSITIONING': 0.4, 'HIGH_VOL': 0.15, 'CRISIS': 0.05}
    agg = aggregate_across_regimes(per_regime, freq)
    assert agg['sharpe'] == pytest.approx(0.4 * 2.0 + 0.4 * 1.0 + 0.15 * 0.5 + 0.05 * 0.0, abs=1e-6)
    # max_dd is worst single-regime DD, not weighted average
    assert agg['max_dd'] == 0.20


def test_eligibility_empty_list_means_strict_deny():
    """Explicit eligible_regimes=[] is different from missing — denies all regimes."""
    df = _backtest_df([
        ('S1', 'LOW_VOL', 2.0, 0.05, 10.0, 100, 200, 1, None, []),
        ('S1', 'TRANSITIONING', 0.0, 0.0, 0.0, 0, 0, 1, 'not_declared', []),
        ('S1', 'HIGH_VOL', 0.0, 0.0, 0.0, 0, 0, 1, 'not_declared', []),
        ('S1', 'CRISIS', 0.0, 0.0, 0.0, 0, 0, 1, 'not_declared', []),
    ])
    manifest = {'strategies': {'S1': {'eligible_regimes': []}}}
    blend = portfolio_per_regime(df, manifest, eligibility_filter=True)
    # S1 with empty eligibility contributes to no regime
    assert blend['LOW_VOL']['strategy_count'] == 0


def test_eligibility_missing_is_backward_compat_pass():
    """A strategy without eligible_regimes is treated as eligible everywhere."""
    df = _backtest_df([
        ('S1', 'LOW_VOL', 2.0, 0.05, 10.0, 100, 200, 1, None, None),
        ('S1', 'HIGH_VOL', 1.0, 0.10, 5.0, 50, 100, 1, None, None),
        ('S1', 'TRANSITIONING', 0.0, 0.0, 0.0, 0, 0, 1, 'not_declared', None),
        ('S1', 'CRISIS', 0.0, 0.0, 0.0, 0, 0, 1, 'not_declared', None),
    ])
    manifest = {'strategies': {'S1': {}}}  # no eligible_regimes key
    blend = portfolio_per_regime(df, manifest, eligibility_filter=True)
    assert blend['LOW_VOL']['strategy_count'] == 1
    assert blend['HIGH_VOL']['strategy_count'] == 1


def test_regime_day_frequency_falls_back_when_parquet_missing(tmp_path):
    """Missing parquet → uniform weights, no crash."""
    freq = regime_day_frequency(tmp_path / 'does_not_exist.parquet')
    assert sum(freq.values()) == pytest.approx(1.0, abs=1e-6)
    assert all(0 < v < 1 for v in freq.values())


def test_gate_b_tolerance_constant_is_positive():
    """Tolerance constant exists and is a sensible magnitude."""
    assert SHARPE_REGRESSION_TOLERANCE_ABS > 0
    assert SHARPE_REGRESSION_TOLERANCE_ABS <= 1.0  # not absurdly permissive


def test_gate_b_passes_within_tolerance_when_dd_unchanged(monkeypatch, tmp_path):
    """End-to-end: small Sharpe regression + unchanged DD + LOW_VOL strategies → gate B passes."""
    df = _backtest_df([
        # Producer of profit in LOW_VOL — filter drops it (eligible only HIGH_VOL).
        ('S_PROFIT_LOW', 'LOW_VOL',      2.0, 0.02, 5.0, 100, 200, 1, None, ['HIGH_VOL']),
        ('S_PROFIT_LOW', 'TRANSITIONING', 0.0, 0.0, 0.0,  0,   0, 1, 'not_declared', ['HIGH_VOL']),
        ('S_PROFIT_LOW', 'HIGH_VOL',     0.0, 0.0, 0.0,   0,   0, 1, 'not_declared', ['HIGH_VOL']),
        ('S_PROFIT_LOW', 'CRISIS',       0.0, 0.0, 0.0,   0,   0, 1, 'not_declared', ['HIGH_VOL']),
        # Always-eligible strategy keeps the portfolio non-empty in LOW_VOL.
        ('S_STAY',       'LOW_VOL',      1.5, 0.02, 5.0,  80, 200, 1, None, None),
        ('S_STAY',       'TRANSITIONING', 0.0, 0.0, 0.0,  0,   0, 1, 'not_declared', None),
        ('S_STAY',       'HIGH_VOL',     0.0, 0.0, 0.0,   0,   0, 1, 'not_declared', None),
        ('S_STAY',       'CRISIS',       0.0, 0.0, 0.0,   0,   0, 1, 'not_declared', None),
    ])
    manifest_path = tmp_path / 'manifest.json'
    manifest_path.write_text(json.dumps({'strategies': {
        'S_PROFIT_LOW': {'eligible_regimes': ['HIGH_VOL']},
        'S_STAY': {},
    }}))
    regime_path = tmp_path / 'regimes.parquet'  # missing → falls back to uniform 0.25

    import backtest.regime_blended_backtest as mod
    monkeypatch.setattr(mod, 'load_latest_backtests', lambda uri: (df, 'test-run'))
    result = mod.run_walkforward('not-used', manifest_path, regime_path)

    # Filter regression is < tolerance (LOW_VOL drop is 2.0 → 1.5, weighted 0.25 → -0.125 Sharpe)
    assert -SHARPE_REGRESSION_TOLERANCE_ABS <= result['delta']['sharpe'] < 0
    assert result['delta']['max_dd'] == 0.0
    assert result['gate_b']['sharpe_within_tolerance'] is True
    assert result['gate_b']['max_dd_not_worse'] is True
    assert result['gate_b']['low_vol_strategies_present'] is True
    assert result['gate_b']['pass'] is True


def test_gate_b_max_dd_guard_is_structurally_satisfied():
    """The DD guard cannot fail under the harness's aggregation model.

    `portfolio_per_regime` aggregates max_dd as max over remaining strategies'
    max_dd. The eligibility filter only ever REMOVES strategies from a regime,
    so the surviving subset's max-of cannot exceed the original max-of. The
    aggregate across regimes is the max over per-regime values — same monotonicity.
    Therefore `blended.max_dd <= production.max_dd` is guaranteed by
    construction. The guard exists in the gate predicate as an explicit
    safety contract but is informational under this aggregation. If the
    harness's portfolio model is upgraded to compounded portfolio DD (where
    losing a hedge can worsen DD), the guard becomes constraining.
    """
    df = _backtest_df([
        # Production has high DD strategy in HIGH_VOL; filter keeps it (eligible HIGH_VOL).
        ('A', 'LOW_VOL',       1.0, 0.05, 1.0, 50, 200, 1, None, ['LOW_VOL']),
        ('A', 'HIGH_VOL',     -1.0, 0.40, -5.0, 30, 100, 1, 'not_declared', ['LOW_VOL']),
        ('A', 'TRANSITIONING', 0.0, 0.0,  0.0,  0,   0, 1, 'not_declared', ['LOW_VOL']),
        ('A', 'CRISIS',        0.0, 0.0,  0.0,  0,   0, 1, 'not_declared', ['LOW_VOL']),
        ('B', 'LOW_VOL',       0.5, 0.02, 0.5, 20, 200, 1, None, None),
        ('B', 'HIGH_VOL',      0.0, 0.0,  0.0,  0,   0, 1, 'not_declared', None),
        ('B', 'TRANSITIONING', 0.0, 0.0,  0.0,  0,   0, 1, 'not_declared', None),
        ('B', 'CRISIS',        0.0, 0.0,  0.0,  0,   0, 1, 'not_declared', None),
    ])
    manifest = {'strategies': {
        'A': {'eligible_regimes': ['LOW_VOL']},  # filter drops A from HIGH_VOL
        'B': {},                                 # B unaffected
    }}
    prod = aggregate_across_regimes(
        portfolio_per_regime(df, manifest, eligibility_filter=False), _flat_freq()
    )
    blend = aggregate_across_regimes(
        portfolio_per_regime(df, manifest, eligibility_filter=True), _flat_freq()
    )
    # Filter strictly decreases blended DD (or keeps it tied).
    assert blend['max_dd'] <= prod['max_dd']


def test_gate_b_passes_when_filter_improves_sharpe_and_keeps_dd():
    """End-to-end smoke: filter that drops a low-sharpe strategy in CRISIS passes gate B."""
    df = _backtest_df([
        ('GOOD', 'LOW_VOL',       2.0, 0.05, 10.0, 100, 200, 1, None, ['LOW_VOL']),
        ('GOOD', 'CRISIS',        0.0, 0.0,   0.0,   0,   0, 1, 'not_declared', ['LOW_VOL']),
        ('GOOD', 'TRANSITIONING', 0.0, 0.0,   0.0,   0,   0, 1, 'not_declared', ['LOW_VOL']),
        ('GOOD', 'HIGH_VOL',      0.0, 0.0,   0.0,   0,   0, 1, 'not_declared', ['LOW_VOL']),
        # BAD: declared eligible for LOW_VOL only, but happens to have a CRISIS backtest row with bad Sharpe + huge DD
        ('BAD', 'LOW_VOL',        1.0, 0.04, 5.0,  50, 200, 1, None, ['LOW_VOL']),
        ('BAD', 'CRISIS',        -2.0, 0.30, -20.0, 30, 100, 1, None, ['LOW_VOL']),
        ('BAD', 'TRANSITIONING',  0.0, 0.0,  0.0,   0,   0, 1, 'not_declared', ['LOW_VOL']),
        ('BAD', 'HIGH_VOL',       0.0, 0.0,  0.0,   0,   0, 1, 'not_declared', ['LOW_VOL']),
    ])
    manifest = {'strategies': {
        'GOOD': {'eligible_regimes': ['LOW_VOL']},
        'BAD':  {'eligible_regimes': ['LOW_VOL']},
    }}
    prod = aggregate_across_regimes(
        portfolio_per_regime(df, manifest, eligibility_filter=False),
        _flat_freq(),
    )
    blend = aggregate_across_regimes(
        portfolio_per_regime(df, manifest, eligibility_filter=True),
        _flat_freq(),
    )
    # Blended drops BAD's CRISIS contribution; production keeps it.
    # CRISIS in production: only BAD with sharpe=-2, dd=0.30. After filter: empty (CRISIS gets 0/0).
    assert prod['per_regime']['CRISIS']['sharpe'] == -2.0
    assert blend['per_regime']['CRISIS']['sharpe'] == 0.0
    assert blend['sharpe'] > prod['sharpe']
    assert blend['max_dd'] <= prod['max_dd']
