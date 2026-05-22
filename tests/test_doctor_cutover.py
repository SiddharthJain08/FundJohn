"""SP-1: doctor preflight expansion tests."""
from unittest.mock import patch
import pytest

from src.maintenance.doctor import (
    _check_alpaca_aat_plus_tier,
    _check_options_archive_freshness,
    _check_cboe_vol_indices_freshness,
)


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
