import os
import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, call
import pytest

from src.pipeline.run_premarket_scan import (
    main, run_scan, ScanConfig, GateConfigError,
)
from src.sentiment.sonnet_premarket_confirmer import PremarketConfirmerResult


@pytest.fixture
def env_master_on(monkeypatch):
    monkeypatch.setenv('OPENCLAW_PREMARKET_SCAN', '1')
    monkeypatch.delenv('OPENCLAW_PREMARKET_CONFIRMER', raising=False)
    monkeypatch.delenv('OPENCLAW_PREMARKET_AUTOCLOSE', raising=False)


def test_gate_hierarchy_autoclose_without_confirmer_raises(monkeypatch):
    monkeypatch.setenv('OPENCLAW_PREMARKET_SCAN', '1')
    monkeypatch.delenv('OPENCLAW_PREMARKET_CONFIRMER', raising=False)
    monkeypatch.setenv('OPENCLAW_PREMARKET_AUTOCLOSE', '1')
    with pytest.raises(GateConfigError) as exc:
        ScanConfig.from_env()
    assert 'confirmer' in str(exc.value).lower()


def test_master_gate_off_exits_zero_silently(monkeypatch):
    monkeypatch.delenv('OPENCLAW_PREMARKET_SCAN', raising=False)
    rc = main(['--scan-label', '07:30'])
    assert rc == 0


@patch('src.pipeline.run_premarket_scan.is_trading_day_in_et', return_value=False)
def test_holiday_exits_zero_no_db_writes(mock_calendar, env_master_on):
    rc = main(['--scan-label', '07:30'])
    assert rc == 0
    mock_calendar.assert_called_once()


@patch('src.pipeline.run_premarket_scan._post_discord')
@patch('src.pipeline.run_premarket_scan._persist_alert_rows')
@patch('src.pipeline.run_premarket_scan.resolve_premarket_webhook',
       return_value='https://discord.com/api/webhooks/test')
@patch('src.pipeline.run_premarket_scan.score_news_for_tickers')
@patch('src.pipeline.run_premarket_scan.load_open_equity_positions')
@patch('src.pipeline.run_premarket_scan.is_trading_day_in_et', return_value=True)
def test_rules_only_path_persists_and_posts_when_score_above_threshold(
    _cal, _load, mock_news, _webhook, _persist, _post, env_master_on,
):
    _load.return_value = [
        {'symbol': 'GLW', 'qty': 100, 'avg_entry_price': 32.5, 'market_value': 3210},
    ]
    mock_news.return_value = [{
        'ticker': 'GLW', 'news_count_24h': 3, 'news_finbert_neg': 0.8,
        'news_finbert_pos': 0.1, 'news_finbert_neu': 0.1, 'news_mean_score': -0.7,
        'news_top_headlines': ['CFO departs', 'Guidance cut', 'Downgrade'],
        'evidence_uuids': ['u1', 'u2', 'u3'],
    }]
    rc = main(['--scan-label', '07:30'])

    assert rc == 0
    rows = _persist.call_args[0][0]
    assert len(rows) == 1
    assert rows[0]['ticker'] == 'GLW'
    assert rows[0]['panic_score'] > 35
    assert rows[0]['advisory_fired'] is True
    assert rows[0]['sonnet_verdict'] is None   # confirmer gate OFF
    _post.assert_called_once()


@patch('src.pipeline.run_premarket_scan._post_discord')
@patch('src.pipeline.run_premarket_scan._persist_alert_rows')
@patch('src.pipeline.run_premarket_scan.confirm_panic')
@patch('src.pipeline.run_premarket_scan.score_news_for_tickers')
@patch('src.pipeline.run_premarket_scan.load_open_equity_positions')
@patch('src.pipeline.run_premarket_scan.is_trading_day_in_et', return_value=True)
def test_confirmer_path_calls_sonnet_only_for_above_threshold(
    _cal, _load, mock_news, mock_sonnet, _persist, _post, monkeypatch,
):
    monkeypatch.setenv('OPENCLAW_PREMARKET_SCAN', '1')
    monkeypatch.setenv('OPENCLAW_PREMARKET_CONFIRMER', '1')
    _load.return_value = [
        {'symbol': 'GLW',  'qty': 100, 'avg_entry_price': 32.5, 'market_value': 3210},
        {'symbol': 'AAPL', 'qty': 50,  'avg_entry_price': 200.0, 'market_value': 10000},
    ]
    mock_news.side_effect = [
        # GLW: above threshold
        [{'ticker': 'GLW', 'news_count_24h': 3, 'news_finbert_neg': 0.8,
          'news_finbert_pos': 0.1, 'news_finbert_neu': 0.1, 'news_mean_score': -0.7,
          'news_top_headlines': ['x', 'y', 'z'], 'evidence_uuids': ['u1', 'u2', 'u3']}],
        # AAPL: below
        [{'ticker': 'AAPL', 'news_count_24h': 1, 'news_finbert_neg': 0.0,
          'news_finbert_pos': 0.8, 'news_finbert_neu': 0.2, 'news_mean_score': 0.6,
          'news_top_headlines': ['k'], 'evidence_uuids': ['u9']}],
    ]
    mock_sonnet.return_value = PremarketConfirmerResult(
        verdict='bearish_news_driven', severity=5,
        rationale='hard catalyst', evidence_uuids=['u1', 'u2'], cost_usd=0.013,
    )

    main(['--scan-label', '07:30'])

    # Sonnet called for GLW only
    assert mock_sonnet.call_count == 1
    assert mock_sonnet.call_args[0][0].ticker == 'GLW'


@patch('src.pipeline.run_premarket_scan.close_subset')
@patch('src.pipeline.run_premarket_scan._post_discord')
@patch('src.pipeline.run_premarket_scan._persist_alert_rows')
@patch('src.pipeline.run_premarket_scan.confirm_panic')
@patch('src.pipeline.run_premarket_scan.score_news_for_tickers')
@patch('src.pipeline.run_premarket_scan.load_open_equity_positions')
@patch('src.pipeline.run_premarket_scan.is_trading_day_in_et', return_value=True)
def test_autoclose_fires_only_when_gate_on_and_strict_severity_met(
    _cal, _load, mock_news, mock_sonnet, _persist, _post, mock_close, monkeypatch,
):
    monkeypatch.setenv('OPENCLAW_PREMARKET_SCAN', '1')
    monkeypatch.setenv('OPENCLAW_PREMARKET_CONFIRMER', '1')
    monkeypatch.setenv('OPENCLAW_PREMARKET_AUTOCLOSE', '1')
    _load.return_value = [
        {'symbol': 'GLW', 'qty': 100, 'avg_entry_price': 32.5, 'market_value': 3210},
    ]
    mock_news.return_value = [{
        'ticker': 'GLW', 'news_count_24h': 3, 'news_finbert_neg': 0.8,
        'news_finbert_pos': 0.1, 'news_finbert_neu': 0.1, 'news_mean_score': -0.7,
        'news_top_headlines': ['x', 'y', 'z'], 'evidence_uuids': ['u1', 'u2', 'u3'],
    }]
    mock_sonnet.return_value = PremarketConfirmerResult(
        verdict='bearish_news_driven', severity=5,
        rationale='hard catalyst', evidence_uuids=['u1'], cost_usd=0.013,
    )
    mock_close.return_value = [{'ticker': 'GLW', 'status': 'filled',
                                'liquidation_id': 42}]

    main(['--scan-label', '09:00'])

    mock_close.assert_called_once()
    args, kwargs = mock_close.call_args
    assert args[0] == ['GLW']
    assert kwargs.get('reason', args[1] if len(args) > 1 else None) == 'PREMARKET_PANIC'


@patch('src.pipeline.run_premarket_scan.close_subset')
@patch('src.pipeline.run_premarket_scan._post_discord')
@patch('src.pipeline.run_premarket_scan._persist_alert_rows')
@patch('src.pipeline.run_premarket_scan.confirm_panic')
@patch('src.pipeline.run_premarket_scan.score_news_for_tickers')
@patch('src.pipeline.run_premarket_scan.load_open_equity_positions')
@patch('src.pipeline.run_premarket_scan.is_trading_day_in_et', return_value=True)
def test_autoclose_skipped_on_llm_error_even_when_gate_on(
    _cal, _load, mock_news, mock_sonnet, _persist, _post, mock_close, monkeypatch,
):
    monkeypatch.setenv('OPENCLAW_PREMARKET_SCAN', '1')
    monkeypatch.setenv('OPENCLAW_PREMARKET_CONFIRMER', '1')
    monkeypatch.setenv('OPENCLAW_PREMARKET_AUTOCLOSE', '1')
    _load.return_value = [
        {'symbol': 'GLW', 'qty': 100, 'avg_entry_price': 32.5, 'market_value': 3210},
    ]
    mock_news.return_value = [{
        'ticker': 'GLW', 'news_count_24h': 3, 'news_finbert_neg': 0.8,
        'news_finbert_pos': 0.1, 'news_finbert_neu': 0.1, 'news_mean_score': -0.7,
        'news_top_headlines': ['x', 'y', 'z'], 'evidence_uuids': ['u1'],
    }]
    mock_sonnet.return_value = PremarketConfirmerResult(
        verdict='llm_error', severity=None,
        rationale='budget exceeded', evidence_uuids=[], cost_usd=None,
    )
    main(['--scan-label', '09:00'])
    mock_close.assert_not_called()


def test_persisted_uuids_filter_out_non_uuid_strings():
    """market_news.uuid is text; not all values are valid UUIDs."""
    from src.pipeline.run_premarket_scan import _filter_uuid_strs
    valid = '12345678-1234-1234-1234-123456789abc'
    out = _filter_uuid_strs([valid, 'alpaca-news-12345', None, ''])
    assert out == [valid]
    assert _filter_uuid_strs([]) is None
    assert _filter_uuid_strs(['only-bad', 'also-bad']) is None
