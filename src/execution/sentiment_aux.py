"""Live sentiment aux builder — parity with backtest aux_data_loader._sentiment_day_slice.

The backtest reads sentiment.parquet's ``news_*`` columns, which were built from
Alpaca news scored by the *identical* FinBERT scorer. LIVE, that same Alpaca-news
signal lands in ``ticker_sentiment_daily.alpaca_news_*`` columns (the legacy
``news_*`` columns are a dead RSS source, ~0% covered since 2026-05-22). So the
true source parity is:

    backtest news_*  <->  live alpaca_news_*

This module remaps ``alpaca_news_*`` rows to the ``news_*`` dict keys the strategy
(``S_news_sentiment_long_short``) and scorer (``confirmation.news_flow.score``)
expect, applying the same point-in-time forward-fill + staleness cap the backtest
loader uses. Pure / deterministic — no DB, no clock. The engine owns the fetch.

Do NOT coalesce with the legacy live ``news_*`` columns: different source + scorer
would break the backtest↔live parity this whole design rests on.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Optional

# Mirror src/strategies/aux_data_loader.SENTIMENT_MAX_AGE_DAYS (7). News sentiment
# decays fast; a score older than this is noise, and without the cap a ticker that
# stops making news would drive a LONG/SHORT off a stale score indefinitely.
SENTIMENT_MAX_AGE_DAYS = 7


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def build_sentiment_aux(
    rows: Iterable[dict],
    as_of,
    max_age_days: int = SENTIMENT_MAX_AGE_DAYS,
) -> dict[str, dict]:
    """Map live ``ticker_sentiment_daily`` rows to per-ticker sentiment dicts.

    rows: iterable of dict-like rows with at least ``ticker``, ``date``,
        ``alpaca_news_count_24h``, ``alpaca_news_mean_score`` and the three
        ``alpaca_news_finbert_{pos,neu,neg}`` columns.
    as_of: the trading date (date / datetime / 'YYYY-MM-DD').

    Returns ``{ticker: {news_count_24h, news_mean_score, news_finbert_pos,
    news_finbert_neu, news_finbert_neg}}`` for each ticker whose latest
    news-bearing row (``alpaca_news_count_24h > 0``) is dated on or before
    ``as_of`` and within ``max_age_days``. Point-in-time: future rows are ignored;
    a same-day zero-news row never shadows an older news-bearing row.
    """
    as_of_d = _as_date(as_of)
    if as_of_d is None:
        return {}

    latest: dict[str, tuple[date, dict]] = {}
    for row in rows:
        count = row.get('alpaca_news_count_24h') or 0
        if count <= 0:
            continue  # zero-news rows must not shadow older news (forward-fill parity)
        d = _as_date(row.get('date'))
        if d is None or d > as_of_d:
            continue  # no look-ahead
        if (as_of_d - d).days > max_age_days:
            continue  # staleness cap
        ticker = row.get('ticker')
        if ticker is None:
            continue
        prev = latest.get(ticker)
        if prev is None or d > prev[0]:
            mean = row.get('alpaca_news_mean_score')
            latest[ticker] = (d, {
                'news_count_24h': int(count),
                'news_mean_score': float(mean) if mean is not None else None,
                'news_finbert_pos': row.get('alpaca_news_finbert_pos') or 0,
                'news_finbert_neu': row.get('alpaca_news_finbert_neu') or 0,
                'news_finbert_neg': row.get('alpaca_news_finbert_neg') or 0,
            })
    return {ticker: payload for ticker, (_d, payload) in latest.items()}
