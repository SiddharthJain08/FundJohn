"""tests/test_stocktwits_client.py"""
from __future__ import annotations
from unittest.mock import patch, MagicMock
import json


SAMPLE_RESP = {
    "messages": [
        {"id": 1, "body": "AAPL to the moon!", "user": {"username": "u1"},
         "entities": {"sentiment": {"basic": "Bullish"}}},
        {"id": 2, "body": "Selling my AAPL puts", "user": {"username": "u2"},
         "entities": {"sentiment": {"basic": "Bearish"}}},
        {"id": 3, "body": "Just watching", "user": {"username": "u3"},
         "entities": None},  # no sentiment tag
        {"id": 4, "body": "Bullish AAPL", "user": {"username": "u4"},
         "entities": {"sentiment": {"basic": "Bullish"}}},
    ]
}


def test_fetch_ticker_stream_aggregates_sentiment():
    from src.ingestion.stocktwits_client import fetch_ticker_stream
    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.read = MagicMock(return_value=json.dumps(SAMPLE_RESP).encode())
    fake_resp.__enter__ = MagicMock(return_value=fake_resp)
    fake_resp.__exit__  = MagicMock(return_value=False)
    with patch('src.ingestion.stocktwits_client.urllib.request.urlopen', return_value=fake_resp):
        agg = fetch_ticker_stream('AAPL')
    assert agg['ticker']    == 'AAPL'
    assert agg['bull_count'] == 2
    assert agg['bear_count'] == 1
    assert agg['neutral_count'] == 1
    assert agg['total_posts'] == 4
    assert set(agg['authors']) == {'u1', 'u2', 'u3', 'u4'}


def test_fetch_handles_404_returns_empty():
    from src.ingestion.stocktwits_client import fetch_ticker_stream
    from urllib.error import HTTPError
    with patch('src.ingestion.stocktwits_client.urllib.request.urlopen',
               side_effect=HTTPError('u', 404, 'not found', {}, None)):
        agg = fetch_ticker_stream('FAKE')
    assert agg['total_posts'] == 0
    assert agg['bull_count']  == 0


def test_fetch_empty_messages_returns_zeros():
    from src.ingestion.stocktwits_client import fetch_ticker_stream
    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.read = MagicMock(return_value=json.dumps({"messages": []}).encode())
    fake_resp.__enter__ = MagicMock(return_value=fake_resp)
    fake_resp.__exit__  = MagicMock(return_value=False)
    with patch('src.ingestion.stocktwits_client.urllib.request.urlopen', return_value=fake_resp):
        agg = fetch_ticker_stream('AAPL')
    assert agg['total_posts'] == 0
