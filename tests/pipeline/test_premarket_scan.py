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


# C4 (item 16): run_scan now reads ticker_sentiment_daily. src/pipeline modules
# load .env at import, so an unpatched loader would reach the REAL Postgres from
# a unit test. Every run_scan test stubs it to {} (= "no social row", the
# pre-item-16 behaviour these assertions were written against).
@patch('src.pipeline.run_premarket_scan._load_social_for_tickers', return_value={})
@patch('src.pipeline.run_premarket_scan._post_discord')
@patch('src.pipeline.run_premarket_scan._persist_alert_rows')
@patch('src.pipeline.run_premarket_scan.resolve_premarket_webhook',
       return_value='https://discord.com/api/webhooks/test')
@patch('src.pipeline.run_premarket_scan.score_news_for_tickers')
@patch('src.pipeline.run_premarket_scan.load_open_equity_positions')
@patch('src.pipeline.run_premarket_scan.is_trading_day_in_et', return_value=True)
def test_rules_only_path_persists_and_posts_when_score_above_threshold(
    _cal, _load, mock_news, _webhook, _persist, _post, _social, env_master_on,
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


@patch('src.pipeline.run_premarket_scan._load_social_for_tickers', return_value={})
@patch('src.pipeline.run_premarket_scan.resolve_premarket_webhook',
       return_value='https://discord.com/api/webhooks/test')
@patch('src.pipeline.run_premarket_scan._post_discord')
@patch('src.pipeline.run_premarket_scan._persist_alert_rows')
@patch('src.pipeline.run_premarket_scan.confirm_panic')
@patch('src.pipeline.run_premarket_scan.score_news_for_tickers')
@patch('src.pipeline.run_premarket_scan.load_open_equity_positions')
@patch('src.pipeline.run_premarket_scan.is_trading_day_in_et', return_value=True)
def test_confirmer_path_calls_sonnet_only_for_above_threshold(
    _cal, _load, mock_news, mock_sonnet, _persist, _post, _webhook, _social, monkeypatch,
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


@patch('src.pipeline.run_premarket_scan._load_social_for_tickers', return_value={})
@patch('src.pipeline.run_premarket_scan._market_is_rth', return_value=True)   # 2026-11-25: closes fire only in RTH
@patch('src.pipeline.run_premarket_scan.close_subset')
@patch('src.pipeline.run_premarket_scan._post_discord')
@patch('src.pipeline.run_premarket_scan._persist_alert_rows')
@patch('src.pipeline.run_premarket_scan.confirm_panic')
@patch('src.pipeline.run_premarket_scan.score_news_for_tickers')
@patch('src.pipeline.run_premarket_scan.load_open_equity_positions')
@patch('src.pipeline.run_premarket_scan.is_trading_day_in_et', return_value=True)
def test_autoclose_fires_only_when_gate_on_and_strict_severity_met(
    _cal, _load, mock_news, mock_sonnet, _persist, _post, mock_close, _rth, _social, monkeypatch, tmp_path,
):
    import src.pipeline.run_premarket_scan as _rps
    monkeypatch.setattr(_rps, 'DEFERRED_EXITS_PATH', tmp_path / 'deferred.json')
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
    assert kwargs.get('rth') is True


@patch('src.pipeline.run_premarket_scan._load_social_for_tickers', return_value={})
@patch('src.pipeline.run_premarket_scan.close_subset')
@patch('src.pipeline.run_premarket_scan._post_discord')
@patch('src.pipeline.run_premarket_scan._persist_alert_rows')
@patch('src.pipeline.run_premarket_scan.confirm_panic')
@patch('src.pipeline.run_premarket_scan.score_news_for_tickers')
@patch('src.pipeline.run_premarket_scan.load_open_equity_positions')
@patch('src.pipeline.run_premarket_scan.is_trading_day_in_et', return_value=True)
def test_autoclose_skipped_on_llm_error_even_when_gate_on(
    _cal, _load, mock_news, mock_sonnet, _persist, _post, mock_close, _social, monkeypatch,
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


def test_heartbeat_uses_timed_client_seam(monkeypatch):
    """F2 / review I-3: the startup heartbeat write must go through
    pipeline_orchestrator's dedicated, timed get_heartbeat_redis() seam
    (socket_timeout=2, socket_connect_timeout=2) instead of an untimed
    redis.from_url() — this is a oneshot unit with no TimeoutStartSec, so a
    half-open Redis must not be able to hang the scan silently."""
    monkeypatch.delenv('OPENCLAW_PREMARKET_SCAN', raising=False)
    calls = []

    def _spy_from_url(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError('no real redis in tests')

    monkeypatch.setattr('redis.from_url', _spy_from_url)
    rc = main(['--scan-label', '07:30'])
    assert rc == 0  # heartbeat failure must stay non-fatal
    assert calls, 'expected get_heartbeat_redis() to construct a client via redis.from_url'
    assert calls[0].get('socket_timeout') == 2
    assert calls[0].get('socket_connect_timeout') == 2


def test_persisted_uuids_filter_out_non_uuid_strings():
    """market_news.uuid is text; not all values are valid UUIDs."""
    from src.pipeline.run_premarket_scan import _filter_uuid_strs
    valid = '12345678-1234-1234-1234-123456789abc'
    out = _filter_uuid_strs([valid, 'alpaca-news-12345', None, ''])
    assert out == [valid]
    assert _filter_uuid_strs([]) is None
    assert _filter_uuid_strs(['only-bad', 'also-bad']) is None


# ── 2026-11-25 operator ruling: news-veto exits defer to the open ────────────
_NEWS = [{
    'ticker': 'GLW', 'news_count_24h': 3, 'news_finbert_neg': 0.8,
    'news_finbert_pos': 0.1, 'news_finbert_neu': 0.1, 'news_mean_score': -0.7,
    'news_top_headlines': ['x', 'y', 'z'], 'evidence_uuids': ['u1', 'u2', 'u3'],
}]
_PANIC = PremarketConfirmerResult(verdict='bearish_news_driven', severity=5,
                                  rationale='hard catalyst', evidence_uuids=['u1'], cost_usd=0.013)


def _autoclose_env(monkeypatch, tmp_path):
    import src.pipeline.run_premarket_scan as rps
    monkeypatch.setenv('OPENCLAW_PREMARKET_SCAN', '1')
    monkeypatch.setenv('OPENCLAW_PREMARKET_CONFIRMER', '1')
    monkeypatch.setenv('OPENCLAW_PREMARKET_AUTOCLOSE', '1')
    monkeypatch.setattr(rps, 'DEFERRED_EXITS_PATH', tmp_path / 'deferred.json')
    return rps


@patch('src.pipeline.run_premarket_scan._market_is_rth', return_value=False)
@patch('src.pipeline.run_premarket_scan._load_social_for_tickers', return_value={})
@patch('src.pipeline.run_premarket_scan.close_subset')
@patch('src.pipeline.run_premarket_scan._post_discord')
@patch('src.pipeline.run_premarket_scan._persist_alert_rows')
@patch('src.pipeline.run_premarket_scan.resolve_premarket_webhook',
       return_value='https://discord.com/api/webhooks/test')
@patch('src.pipeline.run_premarket_scan.confirm_panic', return_value=_PANIC)
@patch('src.pipeline.run_premarket_scan.score_news_for_tickers', return_value=_NEWS)
@patch('src.pipeline.run_premarket_scan.load_open_equity_positions')
@patch('src.pipeline.run_premarket_scan.is_trading_day_in_et', return_value=True)
def test_premarket_veto_is_journaled_not_closed(
    _cal, _load, _news, _sonnet, _webhook, _persist, mock_post, mock_close, _social, _rth,
    monkeypatch, tmp_path,
):
    rps = _autoclose_env(monkeypatch, tmp_path)
    _load.return_value = [{'symbol': 'GLW', 'qty': 100, 'avg_entry_price': 32.5, 'market_value': 3210}]
    main(['--scan-label', '09:00'])
    mock_close.assert_not_called()
    journal = json.loads((tmp_path / 'deferred.json').read_text())
    (day, entries), = journal.items()
    assert 'GLW' in entries and entries['GLW']['scan_label'] == '09:00' and entries['GLW']['severity'] == 5
    rows = _persist.call_args.args[0]
    assert rows[0]['autoclose_fired'] is False and rows[0].get('autoclose_deferred') is True
    assert 'deferred to the open' in mock_post.call_args.args[1]


@patch('src.pipeline.run_premarket_scan._market_is_rth', return_value=True)
@patch('src.pipeline.run_premarket_scan._load_social_for_tickers', return_value={})
@patch('src.pipeline.run_premarket_scan.close_subset')
@patch('src.pipeline.run_premarket_scan._post_discord')
@patch('src.pipeline.run_premarket_scan._persist_alert_rows')
@patch('src.pipeline.run_premarket_scan.resolve_premarket_webhook',
       return_value='https://discord.com/api/webhooks/test')
@patch('src.pipeline.run_premarket_scan.confirm_panic', return_value=_PANIC)
@patch('src.pipeline.run_premarket_scan.score_news_for_tickers', return_value=_NEWS)
@patch('src.pipeline.run_premarket_scan.load_open_equity_positions')
@patch('src.pipeline.run_premarket_scan.is_trading_day_in_et', return_value=True)
def test_rth_scan_drains_journal_with_rth_closes(
    _cal, _load, _news, _sonnet, _webhook, _persist, mock_post, mock_close, _social, _rth,
    monkeypatch, tmp_path,
):
    from datetime import datetime as _dt
    rps = _autoclose_env(monkeypatch, tmp_path)
    today = _dt.now(rps._ET).date().isoformat()
    (tmp_path / 'deferred.json').write_text(json.dumps({
        today: {'ABC': {'scan_label': '07:30', 'severity': 4}, 'XYZ': {'scan_label': '09:00', 'severity': 5}},
        '2020-01-01': {'OLD': {}},
    }))
    _load.return_value = [{'symbol': 'GLW', 'qty': 100, 'avg_entry_price': 32.5, 'market_value': 3210}]
    mock_close.return_value = [
        {'ticker': 'GLW', 'status': 'filled', 'liquidation_id': 1},
        {'ticker': 'ABC', 'status': 'filled', 'liquidation_id': 2},
        {'ticker': 'XYZ', 'status': 'submit_error', 'error': 'boom'},
    ]
    main(['--scan-label', '09:35'])
    mock_close.assert_called_once()
    args, kwargs = mock_close.call_args
    assert args[0] == ['GLW', 'ABC', 'XYZ'] and kwargs == {'reason': 'PREMARKET_PANIC', 'rth': True}
    rows = _persist.call_args.args[0]
    assert rows[0]['autoclose_fired'] is True and rows[0]['autoclose_liquidation_id'] == 1
    journal = json.loads((tmp_path / 'deferred.json').read_text())
    assert journal == {today: {'XYZ': {'scan_label': '09:00', 'severity': 5}}}   # failed one stays, stale day dropped
    msg = mock_post.call_args.args[1]
    assert 'ABC' in msg and 'deferred veto exit' in msg and 'AUTO-CLOSE submitted' in msg


@patch('src.pipeline.run_premarket_scan._market_is_rth', return_value=True)
@patch('src.pipeline.run_premarket_scan._load_social_for_tickers', return_value={})
@patch('src.pipeline.run_premarket_scan.close_subset')
@patch('src.pipeline.run_premarket_scan._post_discord')
@patch('src.pipeline.run_premarket_scan._persist_alert_rows')
@patch('src.pipeline.run_premarket_scan.resolve_premarket_webhook', return_value=None)
@patch('src.pipeline.run_premarket_scan.confirm_panic')
@patch('src.pipeline.run_premarket_scan.score_news_for_tickers')
@patch('src.pipeline.run_premarket_scan.load_open_equity_positions')
@patch('src.pipeline.run_premarket_scan.is_trading_day_in_et', return_value=True)
def test_rth_scan_with_nothing_flagged_or_journaled_closes_nothing(
    _cal, _load, mock_news, _sonnet, _webhook, _persist, _post, mock_close, _social, _rth,
    monkeypatch, tmp_path,
):
    _autoclose_env(monkeypatch, tmp_path)
    _load.return_value = [{'symbol': 'GLW', 'qty': 100, 'avg_entry_price': 32.5, 'market_value': 3210}]
    mock_news.return_value = [{'ticker': 'GLW', 'news_count_24h': 0, 'news_finbert_neg': 0.0,
                               'news_finbert_pos': 0.0, 'news_finbert_neu': 0.0, 'news_mean_score': 0.0,
                               'news_top_headlines': [], 'evidence_uuids': []}]
    main(['--scan-label', '09:35'])
    mock_close.assert_not_called()
    assert not (tmp_path / 'deferred.json').exists()
