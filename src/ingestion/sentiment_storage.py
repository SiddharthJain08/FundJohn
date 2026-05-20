"""src/ingestion/sentiment_storage.py — Postgres upsert + parquet append for
ticker_sentiment_daily rows.

Parquet is append-only per the NEVER-DELETE invariant. Postgres is the fast
lookup path; on idempotent re-run (same date), upsert overwrites.
"""
from __future__ import annotations
import json
import logging
from datetime import date
from pathlib import Path
from typing import Dict, List

import pandas as pd
import psycopg2

logger = logging.getLogger(__name__)

DEFAULT_PARQUET = Path('/root/openclaw/data/master/sentiment.parquet')


_UPSERT_SQL = """
INSERT INTO ticker_sentiment_daily (
    ticker, date,
    social_posts_24h, social_bull_ratio, social_bear_ratio,
    social_unique_authors, social_top_themes,
    news_count_24h, news_finbert_pos, news_finbert_neu, news_finbert_neg,
    news_mean_score, news_top_headlines,
    updated_at
) VALUES (
    %(ticker)s, %(date)s,
    %(social_posts_24h)s, %(social_bull_ratio)s, %(social_bear_ratio)s,
    %(social_unique_authors)s, %(social_top_themes)s,
    %(news_count_24h)s, %(news_finbert_pos)s, %(news_finbert_neu)s, %(news_finbert_neg)s,
    %(news_mean_score)s, %(news_top_headlines)s,
    NOW()
)
ON CONFLICT (ticker, date) DO UPDATE SET
    social_posts_24h        = EXCLUDED.social_posts_24h,
    social_bull_ratio       = EXCLUDED.social_bull_ratio,
    social_bear_ratio       = EXCLUDED.social_bear_ratio,
    social_unique_authors   = EXCLUDED.social_unique_authors,
    social_top_themes       = EXCLUDED.social_top_themes,
    news_count_24h          = EXCLUDED.news_count_24h,
    news_finbert_pos        = EXCLUDED.news_finbert_pos,
    news_finbert_neu        = EXCLUDED.news_finbert_neu,
    news_finbert_neg        = EXCLUDED.news_finbert_neg,
    news_mean_score         = EXCLUDED.news_mean_score,
    news_top_headlines      = EXCLUDED.news_top_headlines,
    updated_at              = NOW();
"""


def _to_row(r: Dict, run_date: str) -> Dict:
    """Prepare a row dict for upsert — JSON-encode list/dict fields."""
    return {
        'ticker':                r['ticker'],
        'date':                  run_date,
        'social_posts_24h':      r.get('social_posts_24h', 0),
        'social_bull_ratio':     r.get('social_bull_ratio'),
        'social_bear_ratio':     r.get('social_bear_ratio'),
        'social_unique_authors': r.get('social_unique_authors', 0),
        'social_top_themes':     json.dumps(r.get('social_top_themes') or {}),
        'news_count_24h':        r.get('news_count_24h', 0),
        'news_finbert_pos':      r.get('news_finbert_pos', 0),
        'news_finbert_neu':      r.get('news_finbert_neu', 0),
        'news_finbert_neg':      r.get('news_finbert_neg', 0),
        'news_mean_score':       r.get('news_mean_score'),
        'news_top_headlines':    json.dumps(r.get('news_top_headlines') or []),
    }


def upsert_postgres(rows: List[Dict], run_date: str, postgres_uri: str) -> int:
    """Upsert rows into ticker_sentiment_daily. Returns count written."""
    if not rows:
        return 0
    payloads = [_to_row(r, run_date) for r in rows]
    with psycopg2.connect(postgres_uri) as conn:
        with conn.cursor() as cur:
            for p in payloads:
                cur.execute(_UPSERT_SQL, p)
        conn.commit()
    logger.info('sentiment_storage: upserted %d rows for %s', len(payloads), run_date)
    return len(payloads)


def append_parquet(rows: List[Dict], run_date: str,
                   parquet_path: Path = DEFAULT_PARQUET) -> int:
    """Append rows to the master parquet (creates if missing). Returns count."""
    if not rows:
        return 0
    df_new = pd.DataFrame([_to_row(r, run_date) for r in rows])
    # social_top_themes + news_top_headlines stay as JSON strings in parquet.
    # Decoding to Python dicts/lists triggers pyarrow's struct-type inference,
    # which fails on empty dicts ("Cannot write struct type ... with no child
    # field"). Strings round-trip cleanly; readers can json.loads on demand.
    df_new['date']                 = pd.to_datetime(df_new['date']).dt.date

    if parquet_path.exists():
        df_old = pd.read_parquet(parquet_path)
        combined = pd.concat([df_old, df_new], ignore_index=True)
    else:
        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        combined = df_new
    combined.to_parquet(parquet_path, index=False)
    logger.info('sentiment_storage: parquet now %d rows', len(combined))
    return len(df_new)
