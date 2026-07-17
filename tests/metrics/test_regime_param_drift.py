"""Tests for regime_param_drift — severity thresholds + summary."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from metrics import regime_param_drift as drift  # noqa: E402


def _stub_live(monkeypatch, rows):
    monkeypatch.setattr(drift, '_load_live_rollup', lambda: list(rows))


def _stub_priors(monkeypatch, by_pair):
    monkeypatch.setattr(drift, '_load_priors', lambda: dict(by_pair))


def test_severity_ok_within_bounds(monkeypatch):
    # live sharpe 1.5 vs prior 1.5 → delta 0 → OK
    _stub_live(monkeypatch, [{
        'strategy_id': 's1', 'regime_state': 'LOW_VOL',
        'sharpe_proxy': 1.5, 'win_rate': 0.6, 'avg_pnl_pct': 0.01,
        'trade_count': 30,
    }])
    _stub_priors(monkeypatch, {('s1', 'LOW_VOL'): {
        'expected_sharpe': 1.5, 'expected_win_rate': 0.6,
        'expected_avg_pnl_pct': 0.01, 'source': 'Asness 2013',
    }})
    out = drift.compute_drift()
    assert out[0]['severity'] == 'OK'


def test_severity_warn_in_band(monkeypatch):
    # sharpe delta -0.7 → WARN (in [0.5, 1.0])
    _stub_live(monkeypatch, [{
        'strategy_id': 's1', 'regime_state': 'LOW_VOL',
        'sharpe_proxy': 0.8, 'win_rate': 0.6, 'avg_pnl_pct': 0.01,
        'trade_count': 30,
    }])
    _stub_priors(monkeypatch, {('s1', 'LOW_VOL'): {
        'expected_sharpe': 1.5, 'expected_win_rate': 0.6,
        'expected_avg_pnl_pct': 0.01, 'source': 'Asness 2013',
    }})
    out = drift.compute_drift()
    assert out[0]['severity'] == 'WARN'


def test_severity_fail_above_band(monkeypatch):
    # sharpe delta -1.2 → FAIL
    _stub_live(monkeypatch, [{
        'strategy_id': 's1', 'regime_state': 'LOW_VOL',
        'sharpe_proxy': 0.3, 'win_rate': 0.6, 'avg_pnl_pct': 0.01,
        'trade_count': 30,
    }])
    _stub_priors(monkeypatch, {('s1', 'LOW_VOL'): {
        'expected_sharpe': 1.5, 'expected_win_rate': 0.6,
        'expected_avg_pnl_pct': 0.01, 'source': 'Asness 2013',
    }})
    out = drift.compute_drift()
    assert out[0]['severity'] == 'FAIL'


def test_insufficient_data_skipped(monkeypatch):
    # trade_count < 10 → INSUFFICIENT, never WARN/FAIL
    _stub_live(monkeypatch, [{
        'strategy_id': 's1', 'regime_state': 'LOW_VOL',
        'sharpe_proxy': 0.0, 'win_rate': 0.3, 'avg_pnl_pct': -0.05,
        'trade_count': 3,
    }])
    _stub_priors(monkeypatch, {('s1', 'LOW_VOL'): {
        'expected_sharpe': 1.5, 'expected_win_rate': 0.6,
        'expected_avg_pnl_pct': 0.01, 'source': 'paper',
    }})
    out = drift.compute_drift()
    assert out[0]['severity'] == 'INSUFFICIENT'


def test_missing_prior_returns_no_baseline(monkeypatch):
    """No prior + no approved-baseline → no drift signal emitted."""
    _stub_live(monkeypatch, [{
        'strategy_id': 's1', 'regime_state': 'LOW_VOL',
        'sharpe_proxy': 0.5, 'win_rate': 0.4, 'avg_pnl_pct': -0.01,
        'trade_count': 30,
    }])
    _stub_priors(monkeypatch, {})
    monkeypatch.setattr(drift, '_load_applied_baselines', lambda: {})
    out = drift.compute_drift()
    assert out == []


def test_latest_summary_aggregates(monkeypatch):
    _stub_live(monkeypatch, [
        # one OK
        {'strategy_id': 's1', 'regime_state': 'LOW_VOL',
         'sharpe_proxy': 1.5, 'win_rate': 0.6, 'avg_pnl_pct': 0.01, 'trade_count': 30},
        # one WARN
        {'strategy_id': 's2', 'regime_state': 'HIGH_VOL',
         'sharpe_proxy': 0.8, 'win_rate': 0.6, 'avg_pnl_pct': 0.01, 'trade_count': 30},
    ])
    _stub_priors(monkeypatch, {
        ('s1', 'LOW_VOL'): {'expected_sharpe': 1.5, 'expected_win_rate': 0.6,
                            'expected_avg_pnl_pct': 0.01, 'source': 'p'},
        ('s2', 'HIGH_VOL'): {'expected_sharpe': 1.5, 'expected_win_rate': 0.6,
                              'expected_avg_pnl_pct': 0.01, 'source': 'p'},
    })
    s = drift.latest_drift_summary()
    assert s['OK'] == 1
    assert s['WARN'] == 1
    assert s['FAIL'] == 0
