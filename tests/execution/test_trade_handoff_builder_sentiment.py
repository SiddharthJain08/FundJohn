"""tests/test_trade_handoff_builder_sentiment.py

Task 10 — handoff sentiment block injection.

Covers `_load_sentiment_for_tickers` helper: returns a ticker-keyed dict of
sentiment fields when rows exist, and short-circuits on empty input.
"""
from __future__ import annotations
from unittest.mock import patch, MagicMock


def test_load_sentiment_for_tickers_returns_dict():
    from src.execution.trade_handoff_builder import _load_sentiment_for_tickers
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__  = MagicMock(return_value=False)
    cur.execute   = MagicMock()
    cur.fetchall  = MagicMock(return_value=[
        ('AAPL', 23, 0.43, 0.17, 5, {'wallstreetbets': 2},
         3, 2, 0, 1, 0.26, ['Apple recalls', 'Apple beats', 'Apple launches']),
        ('TSLA', 10, 0.5, 0.5, 4, {'wallstreetbets': 1},
         1, 0, 0, 1, -0.91, ['Tesla recalls 200k vehicles']),
    ])
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__  = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    with patch('src.execution.trade_handoff_builder.psycopg2.connect', return_value=conn):
        result = _load_sentiment_for_tickers(['AAPL', 'TSLA'], run_date='2026-05-20',
                                              postgres_uri='pg://fake')
    assert 'AAPL' in result
    assert result['AAPL']['social_posts_24h']    == 23
    assert result['AAPL']['news_mean_score']     == 0.26
    assert result['TSLA']['news_top_headlines'][0] == 'Tesla recalls 200k vehicles'


def test_load_sentiment_returns_empty_when_no_rows():
    from src.execution.trade_handoff_builder import _load_sentiment_for_tickers
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__  = MagicMock(return_value=False)
    cur.execute   = MagicMock()
    cur.fetchall  = MagicMock(return_value=[])
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__  = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    with patch('src.execution.trade_handoff_builder.psycopg2.connect', return_value=conn):
        result = _load_sentiment_for_tickers([], run_date='2026-05-20', postgres_uri='pg://fake')
    assert result == {}
    # Empty tickers should short-circuit BEFORE opening a cursor.
    assert cur.execute.call_count == 0
