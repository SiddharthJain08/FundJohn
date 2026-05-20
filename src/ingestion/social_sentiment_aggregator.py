"""src/ingestion/social_sentiment_aggregator.py — merges Reddit subreddit
posts + StockTwits per-ticker streams into one (ticker, date) row.

StockTwits is sparse: only queried for tickers with >=3 Reddit mentions
today (to keep the universe scan cheap). Tickers without StockTwits data
get Reddit-only aggregation (bull/bear ratios are None).
"""
from __future__ import annotations
from collections import Counter, defaultdict
from typing import Dict, List, Optional


def select_sparse_tickers(reddit_posts: List[Dict], min_mentions: int = 3) -> List[str]:
    counts: Counter = Counter()
    for p in reddit_posts:
        for t in p.get('ticker_mentions', []):
            counts[t] += 1
    return sorted([t for t, c in counts.items() if c >= min_mentions])


def aggregate_for_ticker(ticker: str, reddit_posts: List[Dict],
                          stocktwits: Optional[Dict]) -> Dict:
    """Build one (ticker, date) row from Reddit posts + optional StockTwits."""
    ticker = ticker.upper()
    # Reddit side
    r_posts = 0
    r_authors: set[str] = set()
    themes: Counter = Counter()
    for p in reddit_posts:
        if ticker in p.get('ticker_mentions', []):
            r_posts += 1
            if p.get('author'):
                r_authors.add(p['author'])
            themes[p.get('subreddit', 'unknown')] += 1

    # StockTwits side
    st_posts = stocktwits['total_posts'] if stocktwits else 0
    st_authors = set(stocktwits['authors']) if stocktwits else set()
    bull   = stocktwits['bull_count']    if stocktwits else 0
    bear   = stocktwits['bear_count']    if stocktwits else 0
    total_posts = r_posts + st_posts
    unique_authors = len(r_authors | st_authors)

    bull_ratio: Optional[float] = None
    bear_ratio: Optional[float] = None
    if st_posts > 0 and total_posts > 0:
        bull_ratio = bull / total_posts
        bear_ratio = bear / total_posts

    return {
        'ticker':                ticker,
        'social_posts_24h':      total_posts,
        'social_bull_ratio':     bull_ratio,
        'social_bear_ratio':     bear_ratio,
        'social_unique_authors': unique_authors,
        'social_top_themes':     dict(themes.most_common(5)),
    }


def aggregate_all(reddit_posts: List[Dict],
                  stocktwits_by_ticker: Dict[str, Dict],
                  min_mentions_for_st: int = 3) -> List[Dict]:
    """Return one row per ticker that appeared in any Reddit post."""
    mentioned: set[str] = set()
    for p in reddit_posts:
        mentioned.update(p.get('ticker_mentions', []))
    rows = []
    for ticker in sorted(mentioned):
        st = stocktwits_by_ticker.get(ticker)
        rows.append(aggregate_for_ticker(ticker, reddit_posts, st))
    return rows
