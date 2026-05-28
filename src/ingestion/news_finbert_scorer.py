"""src/ingestion/news_finbert_scorer.py — scores `market_news` rows via the
local FinBERT-Tone service and aggregates per (ticker, date).

Output one dict per ticker with counts in each polarity bucket plus a
signed mean polarity score.
"""
from __future__ import annotations
import logging
import os
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional

import psycopg2

from src.services.finbert.client import FinbertClient

logger = logging.getLogger(__name__)

_POLARITY_SIGN = {'Positive': +1, 'Negative': -1, 'Neutral': 0}


def _signed_score(label: str, score: float) -> float:
    return _POLARITY_SIGN.get(label, 0) * score


def score_news_rows(news_rows: List[Dict]) -> List[Dict]:
    """For each ticker in news_rows, return one aggregated dict.

    Each input row needs at least: ticker, headline. summary is optional.
    On any FinBERT error, the affected ticker's news fields are zeros + None.
    """
    if not news_rows:
        return []
    client = FinbertClient()
    per_ticker: Dict[str, Dict] = defaultdict(lambda: {
        'count': 0, 'pos': 0, 'neu': 0, 'neg': 0,
        'signed_sum': 0.0, 'scored_headlines': [],
        'error': False,
    })
    for r in news_rows:
        ticker = (r.get('ticker') or '').upper()
        text   = (r.get('headline') or '').strip()
        if r.get('summary'):
            text = (text + '. ' + r['summary'])[:512]
        if not ticker or not text:
            continue
        # Touch the ticker so it appears in output even if all calls fail.
        bucket = per_ticker[ticker]
        try:
            scored = client.score(text)
        except Exception as e:
            logger.warning('finbert score failed for %s: %s', ticker, e)
            bucket['error'] = True
            continue
        label = scored.get('label', 'Neutral')
        score = float(scored.get('score', 0.0))
        bucket['count'] += 1
        if label == 'Positive':
            bucket['pos'] += 1
        elif label == 'Negative':
            bucket['neg'] += 1
        else:
            bucket['neu'] += 1
        bucket['signed_sum'] += _signed_score(label, score)
        bucket['scored_headlines'].append((abs(score), text[:200]))

    out: List[Dict] = []
    for ticker, b in per_ticker.items():
        if b['error']:
            out.append({
                'ticker':              ticker,
                'news_count_24h':      0,
                'news_finbert_pos':    0,
                'news_finbert_neu':    0,
                'news_finbert_neg':    0,
                'news_mean_score':     None,
                'news_top_headlines':  [],
            })
            continue
        top = sorted(b['scored_headlines'], key=lambda x: x[0], reverse=True)[:3]
        mean_score: Optional[float] = (b['signed_sum'] / b['count']) if b['count'] > 0 else None
        out.append({
            'ticker':              ticker,
            'news_count_24h':      b['count'],
            'news_finbert_pos':    b['pos'],
            'news_finbert_neu':    b['neu'],
            'news_finbert_neg':    b['neg'],
            'news_mean_score':     mean_score,
            'news_top_headlines':  [h for _, h in top],
        })
    return out


# ---------------------------------------------------------------------------
# Sibling helper: fetch + score by ticker list + time window
# ---------------------------------------------------------------------------

_NEWS_FETCH_SQL = """
    SELECT primary_ticker, related_tickers, title, summary, uuid
      FROM market_news
     WHERE (primary_ticker = ANY(%s) OR related_tickers && %s::text[])
       AND published_at >= %s
"""


def score_news_for_tickers(tickers: List[str], since_ts: datetime) -> List[Dict]:
    """Fetch market_news for `tickers` since `since_ts`, score with FinBERT,
    return one aggregated dict per ticker (same shape as score_news_rows)
    plus an `evidence_uuids` list for downstream confirmer citation.

    A single headline can match multiple queried tickers (via primary_ticker
    or related_tickers); it is attributed once per matched ticker so each
    ticker's count reflects all news mentioning it, not just primary coverage.

    Returns [] if no news rows are found or tickers is empty. No DB writes.
    """
    if not tickers:
        return []

    queried = set(tickers)
    dsn = os.environ['POSTGRES_URI']
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(_NEWS_FETCH_SQL, (tickers, tickers, since_ts))
        rows = cur.fetchall()

    if not rows:
        return []

    news_rows: List[Dict] = []
    uuids_by_ticker: Dict[str, List[str]] = {}
    for primary_ticker, related_tickers, title, summary, uuid in rows:
        # Determine which queried tickers this row should be attributed to.
        article_tickers = {primary_ticker} | set(related_tickers or [])
        matched = queried & article_tickers
        for ticker in matched:
            news_rows.append({
                'ticker':   ticker,
                'headline': title or '',
                'summary':  summary or '',
            })
            uuids_by_ticker.setdefault(ticker, []).append(str(uuid))

    if not news_rows:
        return []

    aggregated = score_news_rows(news_rows)
    for entry in aggregated:
        entry['evidence_uuids'] = uuids_by_ticker.get(entry['ticker'], [])
    return aggregated
