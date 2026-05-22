"""SP-1: Alpaca News API ingestion → ticker_sentiment_daily.alpaca_news_*

Pipeline:
    universe → chunked alpaca data news calls → FinBERT score via :7872
    → aggregate per ticker → upsert ticker_sentiment_daily

Run frequency: once per daily cycle (sentiment stage). Gate: ALPACA_NEWS_INGEST=1.

FinBERT API note:
    The live finbert-sentiment.service exposes POST /score with body {text: str}
    and returns {label: "Positive|Neutral|Negative", score: float (unsigned)}.
    We compute the signed score as sign(label) * score, mirroring
    src/ingestion/news_finbert_scorer.py.  Labels are lowercased before
    storage so aggregate tests see 'positive'/'neutral'/'negative'.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable

log = logging.getLogger(__name__)

ALPACA_BIN = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')

_POLARITY_SIGN = {'positive': +1, 'negative': -1, 'neutral': 0}


def _chunk_symbols(symbols: list[str], chunk_size: int = 50) -> Iterable[list[str]]:
    for i in range(0, len(symbols), chunk_size):
        yield symbols[i:i + chunk_size]


def _call_alpaca_cli(args: list[str]) -> str:
    """Run alpaca CLI; raise RuntimeError on non-zero or rate-limit text.
    Records each call into data_provider_health for the dashboard tile.
    """
    try:
        res = subprocess.run([ALPACA_BIN] + args, capture_output=True, text=True, timeout=30)
    except Exception as e:
        _record_call('alpaca', 'news', success=False, error=f'subprocess: {e}')
        raise
    if res.returncode != 0:
        err = (res.stderr or '').strip()
        _record_call('alpaca', 'news', success=False, error=f'rc={res.returncode}: {err[:160]}')
        raise RuntimeError(f'alpaca cli rc={res.returncode}: {err}')
    if '429' in (res.stderr or '') or 'rate limit' in (res.stderr or '').lower():
        _record_call('alpaca', 'news', success=False, error='429 rate limit')
        raise RuntimeError(f'429 rate limit: {res.stderr.strip()}')
    _record_call('alpaca', 'news', success=True)
    return res.stdout


def _record_call(provider, endpoint, *, success, error=None):
    """Lazy-imported provider_health recorder. Best-effort; never raises."""
    try:
        from src.maintenance.provider_health import record
        record(provider, endpoint, success=success, error=error)
    except Exception:
        pass


def _fetch_news_chunk(symbols: list[str], start: str, retries: int = 3) -> list[dict]:
    """Returns list of {id, symbols, headline, summary, created_at}. Empty on graceful degrade."""
    if not symbols:
        return []
    backoff = 1.0
    for attempt in range(retries):
        try:
            raw = _call_alpaca_cli([
                'data', 'news',
                '--symbols', ','.join(symbols),
                '--start', start,
                '--limit', '50',
                '--sort', 'desc',
                '--exclude-contentless',
            ])
            payload = json.loads(raw)
            return payload.get('news', []) or []
        except (RuntimeError, ValueError, json.JSONDecodeError) as e:
            log.warning('alpaca news chunk error attempt %d/%d: %s', attempt + 1, retries, e)
            if attempt < retries - 1:
                time.sleep(backoff)
                backoff *= 2
    log.warning('alpaca news chunk failed all retries for symbols=%s', symbols[:5])
    return []


def _score_with_finbert(articles: list[dict]) -> list[dict]:
    """Score each article's headline+summary via the local FinBERT-Tone service.

    The live service (finbert-sentiment.service on :7872) takes {text: str} and
    returns {label: "Positive|Neutral|Negative", score: float}.  We call it once
    per article (matching the pattern in news_finbert_scorer.py), compute a signed
    score, and store lowercase label + signed score on each article dict.
    On any scoring failure for an individual article, we fall back to neutral/0.
    """
    if not articles:
        return []
    from src.services.finbert.client import FinbertClient
    client = FinbertClient(timeout=30.0)
    for a in articles:
        headline = (a.get('headline') or '').strip()
        summary = (a.get('summary') or '')[:500]
        text = (headline + ' ' + summary).strip() if summary else headline
        if not text:
            a['finbert_label'] = 'neutral'
            a['finbert_score'] = 0.0
            continue
        try:
            result = client.score(text)
            raw_label = result.get('label', 'Neutral')
            label = raw_label.lower()
            score = float(result.get('score', 0.0))
            signed = _POLARITY_SIGN.get(label, 0) * score
        except Exception as e:
            log.warning('FinBERT scoring failed for article "%s": %s', headline[:60], e)
            label = 'neutral'
            signed = 0.0
        a['finbert_label'] = label
        a['finbert_score'] = signed
    return articles


def _aggregate_per_ticker(articles: list[dict]) -> dict[str, dict]:
    """Per ticker: count, pos/neu/neg counts, mean score, top-3 headlines by |score|."""
    by_ticker: dict[str, list[dict]] = {}
    for a in articles:
        for sym in a.get('symbols', []):
            by_ticker.setdefault(sym, []).append(a)
    out = {}
    for sym, arts in by_ticker.items():
        pos = sum(1 for a in arts if a.get('finbert_label') == 'positive')
        neu = sum(1 for a in arts if a.get('finbert_label') == 'neutral')
        neg = sum(1 for a in arts if a.get('finbert_label') == 'negative')
        scores = [a.get('finbert_score', 0.0) for a in arts]
        mean = sum(scores) / len(scores) if scores else 0.0
        top3 = sorted(arts, key=lambda a: abs(a.get('finbert_score', 0.0)), reverse=True)[:3]
        out[sym] = {
            'count_24h': len(arts),
            'finbert_pos': pos,
            'finbert_neu': neu,
            'finbert_neg': neg,
            'mean_score': mean,
            'top_headlines': [
                {'headline': a.get('headline', ''), 'score': a.get('finbert_score', 0.0)}
                for a in top3
            ],
        }
    return out


def _upsert_ticker_sentiment(rows: list[dict]) -> None:
    """Upsert alpaca_news_* columns into ticker_sentiment_daily on (ticker, date).

    Uses psycopg2.connect(POSTGRES_URI) matching the project's ingestion pattern.
    Only updates alpaca_news_* columns — disjoint from social_* / news_* columns
    written by sentiment_storage.upsert_postgres.
    """
    import psycopg2
    if not rows:
        return
    pg_uri = os.environ['POSTGRES_URI']
    with psycopg2.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    """
                    INSERT INTO ticker_sentiment_daily
                      (ticker, date, alpaca_news_count_24h, alpaca_news_finbert_pos,
                       alpaca_news_finbert_neu, alpaca_news_finbert_neg,
                       alpaca_news_mean_score, alpaca_news_top_headlines)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (ticker, date) DO UPDATE SET
                      alpaca_news_count_24h     = EXCLUDED.alpaca_news_count_24h,
                      alpaca_news_finbert_pos   = EXCLUDED.alpaca_news_finbert_pos,
                      alpaca_news_finbert_neu   = EXCLUDED.alpaca_news_finbert_neu,
                      alpaca_news_finbert_neg   = EXCLUDED.alpaca_news_finbert_neg,
                      alpaca_news_mean_score    = EXCLUDED.alpaca_news_mean_score,
                      alpaca_news_top_headlines = EXCLUDED.alpaca_news_top_headlines,
                      updated_at                = NOW()
                    """,
                    (r['ticker'], r['date'], r['alpaca_news_count_24h'],
                     r['alpaca_news_finbert_pos'], r['alpaca_news_finbert_neu'],
                     r['alpaca_news_finbert_neg'], r['alpaca_news_mean_score'],
                     json.dumps(r['alpaca_news_top_headlines'])),
                )
        conn.commit()


def ingest_alpaca_news(symbols: list[str]) -> None:
    """Main entry. Fetches 24h Alpaca news for universe, scores via FinBERT,
    writes one row per symbol to ticker_sentiment_daily.alpaca_news_* columns.
    """
    if not symbols:
        log.info('alpaca_news: empty universe; skipping')
        return
    start = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    today = datetime.now(timezone.utc).date().isoformat()

    all_articles: list[dict] = []
    for chunk in _chunk_symbols(symbols, chunk_size=50):
        all_articles.extend(_fetch_news_chunk(chunk, start=start))
    scored = _score_with_finbert(all_articles)
    agg = _aggregate_per_ticker(scored)

    rows = []
    for sym in symbols:
        agg_for = agg.get(sym, {})
        rows.append({
            'ticker': sym,
            'date': today,
            'alpaca_news_count_24h': agg_for.get('count_24h', 0),
            'alpaca_news_finbert_pos': agg_for.get('finbert_pos', 0),
            'alpaca_news_finbert_neu': agg_for.get('finbert_neu', 0),
            'alpaca_news_finbert_neg': agg_for.get('finbert_neg', 0),
            'alpaca_news_mean_score': agg_for.get('mean_score'),
            'alpaca_news_top_headlines': agg_for.get('top_headlines', []),
        })
    _upsert_ticker_sentiment(rows)
    log.info('alpaca_news: ingested %d symbols, %d articles', len(symbols), len(all_articles))
