"""SP-1: doctor preflight expansion tests."""
from unittest.mock import patch
import pytest

from src.maintenance.doctor import (
    _check_alpaca_aat_plus_tier,
    _check_options_archive_freshness,
    _check_cboe_vol_indices_freshness,
)
from src.maintenance import doctor


@patch('src.maintenance.doctor._run_alpaca_cli')
def test_aat_plus_tier_passes_when_chain_returns_greeks(mock_cli):
    mock_cli.side_effect = [
        # chain probe
        {'snapshots': {'SPY260618C00742000': {'greeks': {'delta': 0.5, 'gamma': 0.01, 'theta': -0.2, 'vega': 0.8, 'rho': 0.3}}}},
        # news probe
        {'news': [{'id': 1, 'headline': 'test', 'symbols': ['AAPL']}]},
    ]
    result = _check_alpaca_aat_plus_tier()
    assert result['severity'] == 'pass'


@patch('src.maintenance.doctor._run_alpaca_cli')
def test_aat_plus_tier_fails_when_all_greeks_zero(mock_cli):
    mock_cli.side_effect = [
        {'snapshots': {'SPY260618C00742000': {'greeks': {'delta': 0, 'gamma': 0, 'theta': 0, 'vega': 0, 'rho': 0}}}},
        {'news': []},
    ]
    result = _check_alpaca_aat_plus_tier()
    assert result['severity'] == 'fail'


@patch('src.maintenance.doctor._parquet_last_date')
def test_options_archive_freshness_warns_at_2d(mock_last):
    from datetime import date, timedelta
    mock_last.return_value = date.today() - timedelta(days=2)
    result = _check_options_archive_freshness()
    assert result['severity'] == 'warn'


@patch('src.maintenance.doctor._parquet_last_date')
def test_options_archive_freshness_fails_at_4d(mock_last):
    from datetime import date, timedelta
    mock_last.return_value = date.today() - timedelta(days=4)
    result = _check_options_archive_freshness()
    assert result['severity'] == 'fail'


# ── SP-5.3: option_expiry_floor ─────────────────────────────────────────────

def _occ_days_out(root, days_out, right='C'):
    from datetime import date, timedelta
    exp = date.today() + timedelta(days=days_out)
    return f"{root}{exp.strftime('%y%m%d')}{right}00500000"


@patch('src.maintenance.doctor._run_alpaca_cli')
def test_option_expiry_floor_passes_no_legs(mock_cli):
    mock_cli.return_value = [
        {'symbol': 'AAPL', 'qty': '100'},
        {'symbol': 'BTC/USD', 'qty': '0.5'},
    ]
    res = doctor._check_option_expiry_floor()
    assert res['severity'] == 'pass'
    assert 'no option legs' in res['detail']


@patch('src.maintenance.doctor._run_alpaca_cli')
def test_option_expiry_floor_passes_far_dated(mock_cli):
    mock_cli.return_value = [{'symbol': _occ_days_out('SPY', 20), 'qty': '1'}]
    res = doctor._check_option_expiry_floor()
    assert res['severity'] == 'pass'
    assert 'min DTE=20' in res['detail']


@patch('src.maintenance.doctor._run_alpaca_cli')
def test_option_expiry_floor_fails_at_dte_3(mock_cli):
    """DTE<=3 means the sizer's T-7 daily-retry close failed 4+ sessions (or the
    EOD cycle is dead) — FAIL loud."""
    mock_cli.return_value = [
        {'symbol': _occ_days_out('SPY', 3), 'qty': '1'},
        {'symbol': _occ_days_out('SPY', 20, right='P'), 'qty': '1'},
    ]
    res = doctor._check_option_expiry_floor()
    assert res['severity'] == 'fail'
    assert 'DTE<=3' in res['detail']
    assert '1 held leg(s)' in res['detail']


@patch('src.maintenance.doctor._run_alpaca_cli')
def test_option_expiry_floor_warns_on_fetch_failure(mock_cli):
    mock_cli.side_effect = RuntimeError('alpaca cli rc=1')
    res = doctor._check_option_expiry_floor()
    assert res['severity'] == 'warn'
    assert 'broker fetch failed' in res['detail']


@patch('src.maintenance.doctor._run_alpaca_cli')
def test_option_expiry_floor_warns_on_non_list_payload(mock_cli):
    """A dict-shaped error payload (CLI exit 0 but error JSON) must WARN, not crash."""
    mock_cli.return_value = {'error': 'internal', 'code': 500}
    res = doctor._check_option_expiry_floor()
    assert res['severity'] == 'warn'
    assert 'non-list' in res['detail']
