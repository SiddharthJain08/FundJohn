# tests/pipeline/test_premarket_helpers.py
import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
import pytest

from src.pipeline.premarket_helpers import (
    resolve_premarket_webhook,
    is_trading_day_in_et,
    load_open_equity_positions,
)


# ---- resolve_premarket_webhook ----

@patch('src.pipeline.premarket_helpers._load_channel_webhooks')
def test_resolve_premarket_webhook_uses_configured_name(mock_load, monkeypatch):
    mock_load.return_value = {
        'premarket-watch': 'https://discord.com/api/webhooks/PRE',
        'trade-reports':   'https://discord.com/api/webhooks/TR',
    }
    monkeypatch.delenv('OPENCLAW_PREMARKET_DISCORD_WEBHOOK_NAME', raising=False)
    assert resolve_premarket_webhook() == 'https://discord.com/api/webhooks/PRE'


@patch('src.pipeline.premarket_helpers._load_channel_webhooks')
def test_resolve_premarket_webhook_falls_back_to_trade_reports(mock_load, monkeypatch):
    mock_load.return_value = {'trade-reports': 'https://discord.com/api/webhooks/TR'}
    monkeypatch.delenv('OPENCLAW_PREMARKET_DISCORD_WEBHOOK_NAME', raising=False)
    assert resolve_premarket_webhook() == 'https://discord.com/api/webhooks/TR'


@patch('src.pipeline.premarket_helpers._load_channel_webhooks')
def test_resolve_premarket_webhook_returns_none_when_neither_present(
    mock_load, monkeypatch,
):
    mock_load.return_value = {'other': 'x'}
    monkeypatch.delenv('OPENCLAW_PREMARKET_DISCORD_WEBHOOK_NAME', raising=False)
    assert resolve_premarket_webhook() is None


# ---- is_trading_day_in_et ----

@patch('src.pipeline.premarket_helpers._run_cli')
def test_is_trading_day_true_when_next_open_today(mock_cli):
    mock_cli.return_value = (True, {
        'is_open': False,
        'next_open': '2026-05-28T13:30:00Z',
        'next_close': '2026-05-28T20:00:00Z',
        'timestamp': '2026-05-28T11:30:00Z',
    }, None)
    assert is_trading_day_in_et() is True


@patch('src.pipeline.premarket_helpers._run_cli')
def test_is_trading_day_false_when_next_open_is_later_date(mock_cli):
    mock_cli.return_value = (True, {
        'is_open': False,
        'next_open': '2026-05-29T13:30:00Z',
        'next_close': '2026-05-29T20:00:00Z',
        'timestamp': '2026-05-28T11:30:00Z',
    }, None)
    assert is_trading_day_in_et() is False


@patch('src.pipeline.premarket_helpers._run_cli')
def test_is_trading_day_false_on_cli_error(mock_cli):
    mock_cli.return_value = (False, None, 'cli timeout')
    assert is_trading_day_in_et() is False


# ---- load_open_equity_positions ----

@patch('src.pipeline.premarket_helpers._run_cli')
def test_load_open_equity_positions_filters_to_us_equity(mock_cli):
    mock_cli.return_value = (True, [
        {'symbol': 'GLW',  'qty': '100',  'asset_class': 'us_equity',
         'avg_entry_price': '32.50', 'market_value': '3210.00'},
        {'symbol': 'BTCUSD', 'qty': '0.5', 'asset_class': 'crypto',
         'avg_entry_price': '68000', 'market_value': '34000'},
        {'symbol': 'SPY230721C00450000', 'qty': '5', 'asset_class': 'us_option',
         'avg_entry_price': '2.10', 'market_value': '1050'},
    ], None)

    out = load_open_equity_positions()
    symbols = [p['symbol'] for p in out]
    assert symbols == ['GLW']
    assert out[0]['qty'] == 100.0
    assert out[0]['avg_entry_price'] == 32.50


@patch('src.pipeline.premarket_helpers._run_cli')
def test_load_open_equity_positions_returns_empty_on_cli_error(mock_cli):
    mock_cli.return_value = (False, None, 'whatever')
    assert load_open_equity_positions() == []
