"""tests/test_social_sentiment_aggregator.py"""
from __future__ import annotations
from unittest.mock import patch


REDDIT_POSTS = [
    {'id': 'p1', 'subreddit': 'wallstreetbets', 'author': 'u1',
     'ticker_mentions': ['AAPL', 'NVDA'], 'title': 'AAPL calls', 'body': ''},
    {'id': 'p2', 'subreddit': 'stocks', 'author': 'u2',
     'ticker_mentions': ['AAPL'], 'title': 'AAPL puts', 'body': ''},
    {'id': 'p3', 'subreddit': 'wallstreetbets', 'author': 'u3',
     'ticker_mentions': ['AAPL', 'TSLA'], 'title': '', 'body': 'AAPL TSLA'},
    {'id': 'p4', 'subreddit': 'investing', 'author': 'u1',
     'ticker_mentions': ['MSFT'], 'title': 'MSFT', 'body': ''},
]

STOCKTWITS_BY_TICKER = {
    'AAPL': {'ticker': 'AAPL', 'bull_count': 10, 'bear_count': 4, 'neutral_count': 6,
             'total_posts': 20, 'authors': ['s1', 's2']},
    'NVDA': {'ticker': 'NVDA', 'bull_count': 2, 'bear_count': 1, 'neutral_count': 0,
             'total_posts': 3, 'authors': ['s3']},
    'TSLA': {'ticker': 'TSLA', 'bull_count': 5, 'bear_count': 5, 'neutral_count': 0,
             'total_posts': 10, 'authors': ['s4']},
    # MSFT under 3 Reddit mentions → not queried; treated as zeros
}


def test_aggregator_picks_sparse_stocktwits_tickers():
    from src.ingestion.social_sentiment_aggregator import select_sparse_tickers
    selected = select_sparse_tickers(REDDIT_POSTS, min_mentions=3)
    # AAPL appears in 3 posts → selected; NVDA, MSFT, TSLA each 1-2 → not.
    assert selected == ['AAPL']


def test_aggregator_merges_reddit_and_stocktwits():
    from src.ingestion.social_sentiment_aggregator import aggregate_for_ticker
    row = aggregate_for_ticker('AAPL', REDDIT_POSTS, STOCKTWITS_BY_TICKER['AAPL'])
    # Reddit: 3 mentions across 3 posts, 3 unique authors (u1, u2, u3)
    # StockTwits: 20 posts, 10 bull / 4 bear / 6 neutral, 2 authors
    assert row['ticker']                   == 'AAPL'
    assert row['social_posts_24h']         == 23   # 3 reddit + 20 stocktwits
    assert row['social_unique_authors']    == 5    # 3 reddit + 2 stocktwits
    assert abs(row['social_bull_ratio'] - 10/23) < 1e-6
    assert abs(row['social_bear_ratio'] -  4/23) < 1e-6


def test_aggregator_no_stocktwits_uses_reddit_only():
    from src.ingestion.social_sentiment_aggregator import aggregate_for_ticker
    row = aggregate_for_ticker('NVDA', REDDIT_POSTS, None)
    # NVDA: 1 reddit post (u1), no stocktwits
    assert row['ticker']                == 'NVDA'
    assert row['social_posts_24h']      == 1
    assert row['social_unique_authors'] == 1
    # No bull/bear counts → ratios are None
    assert row['social_bull_ratio'] is None
    assert row['social_bear_ratio'] is None


def test_full_aggregate_returns_one_row_per_mentioned_ticker():
    from src.ingestion.social_sentiment_aggregator import aggregate_all
    rows = aggregate_all(REDDIT_POSTS, STOCKTWITS_BY_TICKER, min_mentions_for_st=3)
    tickers = {r['ticker'] for r in rows}
    assert tickers == {'AAPL', 'NVDA', 'TSLA', 'MSFT'}
