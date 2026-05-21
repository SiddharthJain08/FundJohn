"""src/pipeline/run_sentiment_step.py — orchestrator entry point for the
daily ``sentiment`` pipeline step.

Lives in ``src/pipeline/`` (not ``scripts/``) so that
``src.execution.pipeline_orchestrator._resolve_script`` discovers it via
its standard ``src/pipeline/<script>.py`` lookup without any code
changes in the orchestrator.

Stages (in execution order):
  1. Resolve the runtime universe (`current_universe`). Fatal on failure.
  2. Fetch Reddit posts from r/wallstreetbets + r/stocks + r/investing,
     filtering ``ticker_mentions`` to the universe set.
  3. Select the sparse-StockTwits subset (>=3 Reddit mentions) and pull
     StockTwits streams for those tickers only.
  4. Aggregate social rows per ticker (`aggregate_all`).
  5. Load today's ``market_news`` rows for the universe and expand
     multi-ticker rows so the same headline contributes to every
     universe ticker it mentions (primary_ticker OR related_tickers).
  6. Score the expanded news rows via FinBERT (`score_news_rows`).
  7. Outer-join social + news on ticker (`_merge_social_and_news`).
  8. Persist: `upsert_postgres` then `append_parquet`.

Exit codes:
  0 — success
  1 — partial (some sources failed but data was persisted)
  2 — abort (universe lookup failed; nothing to persist)

Multi-ticker news expansion (design note):
  The ``market_news`` table stores a row per article with a
  ``primary_ticker`` plus a ``related_tickers[]`` array. A single
  headline can therefore be sentiment-relevant to N tickers in the
  universe. We expand each row to ``(ticker, headline, summary)``
  tuples — one per universe-overlapping mention — so FinBERT sees and
  scores the article once per ticker it should attribute to. This is
  the advisor-approved design in §1166-§1364 of the D1 plan.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.ingestion.alpaca_news import ingest_alpaca_news
from src.ingestion.resolve_sentiment_universe import current_universe
from src.ingestion.reddit_client import fetch_multiple_subreddits
from src.ingestion.stocktwits_client import fetch_many_tickers
from src.ingestion.social_sentiment_aggregator import (
    aggregate_all,
    select_sparse_tickers,
)
from src.ingestion.news_finbert_scorer import score_news_rows
from src.ingestion.sentiment_storage import append_parquet, upsert_postgres

logger = logging.getLogger(__name__)

SUBREDDITS = ('wallstreetbets', 'stocks', 'investing')


def _load_todays_news(postgres_uri: str, run_date: str,
                      universe: List[str]) -> List[Dict]:
    """Pull today's market_news rows scoped to the universe.

    SCHEMA NOTES (per D1 plan §1166-§1364 corrections):
      * Filter by ``published_at >= %s::date AND published_at <
        (%s::date + INTERVAL '1 day')`` — UTC-anchored half-open window,
        NOT ``published_at::date = %s::date`` which would behave oddly
        across timezones.
      * Include rows where the universe ticker is in ``related_tickers[]``
        as well as ``primary_ticker`` (``primary_ticker = ANY(%s) OR
        related_tickers && %s``).
      * Multi-ticker rows are then expanded in Python so the same
        headline scores against every universe ticker it mentions.

    Returns a list of ``{'ticker', 'headline', 'summary'}`` dicts where
    one source row may produce multiple output dicts.
    """
    import psycopg2
    import psycopg2.extras

    universe_set = set(universe)
    universe_list = list(universe)

    with psycopg2.connect(postgres_uri) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                SELECT COALESCE(primary_ticker, '') AS primary_ticker,
                       related_tickers,
                       title   AS headline,
                       summary,
                       published_at
                  FROM market_news
                 WHERE (primary_ticker = ANY(%s) OR related_tickers && %s)
                   AND title IS NOT NULL
                   AND published_at >= %s::date
                   AND published_at <  (%s::date + INTERVAL '1 day')
                """,
                (universe_list, universe_list, run_date, run_date),
            )
            raw_rows = [dict(r) for r in cur.fetchall()]

    expanded: List[Dict] = []
    for r in raw_rows:
        primary = (r.get('primary_ticker') or '').upper()
        related = r.get('related_tickers') or []
        headline = r.get('headline') or ''
        summary = r.get('summary') or ''
        emitted: set[str] = set()
        if primary and primary in universe_set:
            expanded.append({'ticker': primary, 'headline': headline,
                             'summary': summary})
            emitted.add(primary)
        for rt in related:
            t = (rt or '').upper()
            if t and t in universe_set and t not in emitted:
                expanded.append({'ticker': t, 'headline': headline,
                                 'summary': summary})
                emitted.add(t)
    return expanded


def _merge_social_and_news(social_rows: List[Dict],
                           news_rows: List[Dict]) -> List[Dict]:
    """Outer-join social and news rows on ticker."""
    by_ticker: Dict[str, Dict] = {}
    for r in social_rows:
        by_ticker[r['ticker']] = dict(r)
    for r in news_rows:
        existing = by_ticker.setdefault(
            r['ticker'],
            {
                'ticker': r['ticker'],
                'social_posts_24h': 0,
                'social_unique_authors': 0,
                'social_top_themes': {},
            },
        )
        existing.update({
            'news_count_24h':     r['news_count_24h'],
            'news_finbert_pos':   r['news_finbert_pos'],
            'news_finbert_neu':   r['news_finbert_neu'],
            'news_finbert_neg':   r['news_finbert_neg'],
            'news_mean_score':    r['news_mean_score'],
            'news_top_headlines': r['news_top_headlines'],
        })
    return list(by_ticker.values())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=date.today().isoformat())
    ap.add_argument('--dry-run', action='store_true',
                    help='Skip Postgres upsert + parquet append; run scrapers + FinBERT for plumbing validation only.')
    args = ap.parse_args(argv)
    run_date = args.date
    pg_uri = os.environ['POSTGRES_URI']

    # Stage 1: universe (fatal on failure → rc=2, nothing to persist)
    try:
        universe = current_universe(pg_uri)
    except Exception as e:
        logger.error('universe lookup failed: %s', e)
        return 2
    logger.info('sentiment: universe %d tickers', len(universe))
    universe_set = set(universe)

    # Stage 2: Reddit (24h window)
    twenty_four_h_ago_utc = int(
        (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp()
    )
    reddit_posts = fetch_multiple_subreddits(SUBREDDITS, twenty_four_h_ago_utc)
    for p in reddit_posts:
        p['ticker_mentions'] = [
            t for t in p.get('ticker_mentions', []) if t in universe_set
        ]
    logger.info(
        'sentiment: %d reddit posts after universe filter', len(reddit_posts)
    )

    # Stage 3: StockTwits (sparse subset only)
    sparse_tickers = select_sparse_tickers(reddit_posts, min_mentions=3)
    st_results = fetch_many_tickers(sparse_tickers)
    stocktwits_by = {r['ticker']: r for r in st_results}
    logger.info(
        'sentiment: %d StockTwits tickers queried', len(sparse_tickers)
    )

    # Stage 4: aggregate social
    social_rows = aggregate_all(
        reddit_posts, stocktwits_by, min_mentions_for_st=3
    )
    logger.info('sentiment: %d social rows aggregated', len(social_rows))

    # Stage 5 + 6: news → expand → FinBERT
    news_rows_expanded = _load_todays_news(pg_uri, run_date, universe)
    logger.info(
        'sentiment: %d news rows after multi-ticker expansion',
        len(news_rows_expanded),
    )
    news_rows = score_news_rows(news_rows_expanded)
    logger.info('sentiment: %d news rows scored', len(news_rows))

    # Stage 6b: Alpaca News → ticker_sentiment_daily.alpaca_news_* (gate: ALPACA_NEWS_INGEST=1)
    if not args.dry_run and os.environ.get('ALPACA_NEWS_INGEST') == '1':
        try:
            ingest_alpaca_news(symbols=universe)
        except Exception as e:
            logger.warning('alpaca_news non-fatal failure: %s', e)

    # Stage 7: merge
    merged = _merge_social_and_news(social_rows, news_rows)
    logger.info(
        'sentiment: %d total ticker rows ready for persist', len(merged)
    )

    # Stage 8: persist (parquet append_parquet handles data/master/ mkdir).
    # --dry-run skips writes — scrapers + FinBERT still ran, so plumbing is validated.
    if args.dry_run:
        logger.info('sentiment: dry-run, skipping persist (would write %d rows)', len(merged))
        return 0
    upsert_postgres(merged, run_date, pg_uri)
    append_parquet(merged, run_date)
    return 0


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='[sentiment %(asctime)s] %(message)s',
    )
    sys.exit(main())
