from datetime import datetime, timezone, timedelta
from unittest.mock import patch
import json
import sys
import pytest

from scripts.replay_premarket_panic import replay, _build_argparser


@patch('scripts.replay_premarket_panic.score_news_for_tickers')
def test_replay_returns_full_verdict_no_db_writes(mock_news):
    mock_news.return_value = [{
        'ticker': 'GLW',
        'news_count_24h': 2,
        'news_finbert_neg': 1.0, 'news_finbert_pos': 0.0, 'news_finbert_neu': 0.0,
        'news_mean_score': -0.9,
        'news_top_headlines': ['CFO departs', 'Guidance cut'],
        'evidence_uuids': ['u1', 'u2'],
    }]
    as_of = datetime(2026, 5, 28, 13, 0, tzinfo=timezone.utc)
    out = replay(ticker='GLW', as_of=as_of, with_sonnet=False)

    assert out['ticker'] == 'GLW'
    assert out['panic_score'] > 35
    assert out['advisory_would_fire'] is True
    assert out['sonnet_verdict'] is None
    assert out['headlines'] == ['CFO departs', 'Guidance cut']


@patch('scripts.replay_premarket_panic.confirm_panic')
@patch('scripts.replay_premarket_panic.score_news_for_tickers')
def test_replay_with_sonnet_calls_confirmer(mock_news, mock_sonnet):
    from src.sentiment.sonnet_premarket_confirmer import PremarketConfirmerResult
    mock_news.return_value = [{
        'ticker': 'GLW', 'news_count_24h': 2,
        'news_finbert_neg': 1.0, 'news_finbert_pos': 0.0, 'news_finbert_neu': 0.0,
        'news_mean_score': -0.9, 'news_top_headlines': ['x', 'y'],
        'evidence_uuids': ['u1', 'u2'],
    }]
    mock_sonnet.return_value = PremarketConfirmerResult(
        verdict='bearish_news_driven', severity=5,
        rationale='hard catalyst', evidence_uuids=['u1'], cost_usd=0.012,
    )
    out = replay(ticker='GLW',
                 as_of=datetime(2026, 5, 28, 13, 0, tzinfo=timezone.utc),
                 with_sonnet=True)
    assert out['sonnet_verdict'] == 'bearish_news_driven'
    mock_sonnet.assert_called_once()


@patch('scripts.replay_premarket_panic.score_news_for_tickers', return_value=[])
def test_replay_handles_no_news(_):
    out = replay(ticker='GLW',
                 as_of=datetime(2026, 5, 28, 13, 0, tzinfo=timezone.utc),
                 with_sonnet=False)
    assert out['panic_score'] == 0.0
    assert out['advisory_would_fire'] is False
    assert out['headlines'] == []


def test_argparser_requires_ticker_and_as_of():
    p = _build_argparser()
    args = p.parse_args(['--ticker', 'GLW', '--as-of', '2026-05-28T09:00:00-04:00'])
    assert args.ticker == 'GLW'
    assert args.as_of == '2026-05-28T09:00:00-04:00'
