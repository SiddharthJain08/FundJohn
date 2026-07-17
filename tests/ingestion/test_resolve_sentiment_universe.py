"""tests/test_resolve_sentiment_universe.py — unit tests for the 3-source
sentiment universe resolver.
"""
from __future__ import annotations
from unittest.mock import patch, MagicMock
import pytest


def _mock_cursor(rows_by_query):
    """Build a cursor whose fetchall returns the next list in sequence per execute().

    `rows_by_query` is a list of lists-of-tuples (one list per expected query).
    """
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    calls = {'i': 0}

    def fetchall_side(*a, **kw):
        idx = calls['i']
        calls['i'] += 1
        return rows_by_query[idx] if idx < len(rows_by_query) else []

    def execute_side(sql, params=None):
        return None

    cur.execute = MagicMock(side_effect=execute_side)
    cur.fetchall = MagicMock(side_effect=fetchall_side)
    return cur


def test_universe_union_dedupes_and_sorts():
    from src.ingestion.resolve_sentiment_universe import current_universe
    # 3 queries: universe_config(active), open positions, recent-week signals
    rows = [
        [('AAPL',), ('MSFT',), ('NVDA',)],          # universe_config active
        [('NVDA',), ('TSLA',)],                      # currently held (open positions)
        [('TSLA',), ('GOOGL',), ('AMD',)],          # recent-week signals
    ]
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=_mock_cursor(rows))
    with patch('src.ingestion.resolve_sentiment_universe.psycopg2.connect',
               return_value=conn):
        result = current_universe(postgres_uri='postgres://fake')
    assert result == ['AAPL', 'AMD', 'GOOGL', 'MSFT', 'NVDA', 'TSLA']


def test_universe_handles_empty_subqueries():
    from src.ingestion.resolve_sentiment_universe import current_universe
    rows = [[('aapl',)], [], []]  # lowercase to exercise defensive .upper()
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=_mock_cursor(rows))
    with patch('src.ingestion.resolve_sentiment_universe.psycopg2.connect',
               return_value=conn):
        result = current_universe(postgres_uri='postgres://fake')
    assert result == ['AAPL']


def test_universe_raises_on_db_failure():
    from src.ingestion.resolve_sentiment_universe import current_universe
    with patch('src.ingestion.resolve_sentiment_universe.psycopg2.connect',
               side_effect=ConnectionError('db down')):
        with pytest.raises(ConnectionError):
            current_universe(postgres_uri='postgres://fake')
