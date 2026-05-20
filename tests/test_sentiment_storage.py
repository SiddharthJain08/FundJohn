"""tests/test_sentiment_storage.py"""
from __future__ import annotations
from unittest.mock import patch, MagicMock
from pathlib import Path
import tempfile

import pandas as pd


SAMPLE_ROWS = [
    {'ticker': 'AAPL', 'social_posts_24h': 23, 'social_bull_ratio': 0.43,
     'social_bear_ratio': 0.17, 'social_unique_authors': 5,
     'social_top_themes': {'wallstreetbets': 2, 'stocks': 1},
     'news_count_24h': 3, 'news_finbert_pos': 2, 'news_finbert_neu': 0,
     'news_finbert_neg': 1, 'news_mean_score': 0.26,
     'news_top_headlines': ['Apple recalls iPhones', 'Apple beats earnings', 'Apple launches']},
    {'ticker': 'TSLA', 'social_posts_24h': 10, 'social_bull_ratio': 0.5,
     'social_bear_ratio': 0.5, 'social_unique_authors': 4,
     'social_top_themes': {'wallstreetbets': 1},
     'news_count_24h': 1, 'news_finbert_pos': 0, 'news_finbert_neu': 0,
     'news_finbert_neg': 1, 'news_mean_score': -0.91,
     'news_top_headlines': ['Tesla recalls 200k vehicles']},
]


def test_upsert_postgres_calls_execute_per_row():
    from src.ingestion.sentiment_storage import upsert_postgres
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__  = MagicMock(return_value=False)
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__  = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    with patch('src.ingestion.sentiment_storage.psycopg2.connect', return_value=conn):
        upsert_postgres(SAMPLE_ROWS, run_date='2026-05-20', postgres_uri='pg://fake')
    # 2 rows -> 2 cursor.execute() calls
    assert cur.execute.call_count == 2


def test_append_parquet_creates_new_file_when_missing(tmp_path: Path):
    from src.ingestion.sentiment_storage import append_parquet
    target = tmp_path / 'sentiment.parquet'
    append_parquet(SAMPLE_ROWS, run_date='2026-05-20', parquet_path=target)
    assert target.exists()
    df = pd.read_parquet(target)
    assert len(df) == 2
    assert set(df['ticker']) == {'AAPL', 'TSLA'}
    assert (df['date'].astype(str) == '2026-05-20').all()


def test_append_parquet_appends_to_existing(tmp_path: Path):
    from src.ingestion.sentiment_storage import append_parquet
    target = tmp_path / 'sentiment.parquet'
    # First append
    append_parquet(SAMPLE_ROWS, run_date='2026-05-20', parquet_path=target)
    # Second append (different date, same tickers)
    append_parquet(SAMPLE_ROWS, run_date='2026-05-21', parquet_path=target)
    df = pd.read_parquet(target)
    assert len(df) == 4
    assert set(df['date'].astype(str)) == {'2026-05-20', '2026-05-21'}
