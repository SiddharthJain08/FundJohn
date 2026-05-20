"""tests/test_reddit_client.py"""
from __future__ import annotations
from unittest.mock import patch, MagicMock
import json
import pytest


SAMPLE_REDDIT_RESPONSE = {
    "data": {
        "children": [
            {"data": {"id": "p1", "title": "$AAPL is going to moon 🚀",
                      "selftext": "puts on TSLA", "author": "u1",
                      "created_utc": 1716000000, "score": 42}},
            {"data": {"id": "p2", "title": "Why $MSFT is undervalued",
                      "selftext": "Just bought more $MSFT", "author": "u2",
                      "created_utc": 1716000500, "score": 15}},
            {"data": {"id": "p3", "title": "calls on AAPL",
                      "selftext": "", "author": "u1",
                      "created_utc": 1716001000, "score": 7}},
        ]
    }
}


def test_fetch_subreddit_parses_ticker_mentions():
    from src.ingestion.reddit_client import fetch_subreddit_posts
    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.read = MagicMock(return_value=json.dumps(SAMPLE_REDDIT_RESPONSE).encode())
    fake_resp.__enter__ = MagicMock(return_value=fake_resp)
    fake_resp.__exit__  = MagicMock(return_value=False)
    with patch('src.ingestion.reddit_client.urllib.request.urlopen', return_value=fake_resp):
        posts = fetch_subreddit_posts('wallstreetbets', after_utc=1716000000)
    assert len(posts) == 3
    assert posts[0]['ticker_mentions'] == ['AAPL', 'TSLA']
    assert posts[1]['ticker_mentions'] == ['MSFT']
    assert posts[2]['ticker_mentions'] == ['AAPL']


def test_extract_tickers_filters_common_words():
    from src.ingestion.reddit_client import extract_tickers
    # The COMMON_WORDS denylist should filter "I", "AM", "PM", "ETF"
    text = "I think $AAPL is the next big play. PM me about ETF picks."
    assert extract_tickers(text) == ['AAPL']


def test_extract_tickers_handles_both_dollar_and_uppercase():
    from src.ingestion.reddit_client import extract_tickers
    text = "Buying NVDA and $AMD today, $MSFT later."
    assert sorted(extract_tickers(text)) == ['AMD', 'MSFT', 'NVDA']


def test_fetch_handles_http_error_returns_empty():
    from src.ingestion.reddit_client import fetch_subreddit_posts
    from urllib.error import HTTPError
    with patch('src.ingestion.reddit_client.urllib.request.urlopen',
               side_effect=HTTPError('u', 503, 'down', {}, None)):
        posts = fetch_subreddit_posts('wallstreetbets', after_utc=0)
    assert posts == []
