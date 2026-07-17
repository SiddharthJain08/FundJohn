"""tests/test_reddit_client.py"""
from __future__ import annotations
from unittest.mock import patch, MagicMock


# Three real-shape Atom entries: the first mentions $AAPL in title +
# TSLA in the HTML content; the second mentions $MSFT twice; the third
# title-only mention of AAPL with empty content.
# Timestamps: 2024-05-18T00:00:00Z = 1715990400, +500s, +1000s.
SAMPLE_REDDIT_ATOM = '''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>t3_p1</id>
    <title>$AAPL is going to moon</title>
    <updated>2024-05-18T00:00:00+00:00</updated>
    <author><name>/u/u1</name></author>
    <content type="html">&lt;p&gt;puts on TSLA&lt;/p&gt;</content>
  </entry>
  <entry>
    <id>t3_p2</id>
    <title>Why $MSFT is undervalued</title>
    <updated>2024-05-18T00:08:20+00:00</updated>
    <author><name>/u/u2</name></author>
    <content type="html">&lt;p&gt;Just bought more $MSFT&lt;/p&gt;</content>
  </entry>
  <entry>
    <id>t3_p3</id>
    <title>calls on AAPL</title>
    <updated>2024-05-18T00:16:40+00:00</updated>
    <author><name>/u/u1</name></author>
    <content type="html"></content>
  </entry>
</feed>'''.encode()


def _fake_resp(payload: bytes, status: int = 200):
    r = MagicMock()
    r.status = status
    r.read = MagicMock(return_value=payload)
    r.__enter__ = MagicMock(return_value=r)
    r.__exit__  = MagicMock(return_value=False)
    return r


def test_fetch_subreddit_parses_ticker_mentions():
    from src.ingestion.reddit_client import fetch_subreddit_posts
    with patch('src.ingestion.reddit_client.urllib.request.urlopen',
               return_value=_fake_resp(SAMPLE_REDDIT_ATOM)):
        posts = fetch_subreddit_posts('wallstreetbets', after_utc=1715990400)
    assert len(posts) == 3
    assert posts[0]['ticker_mentions'] == ['AAPL', 'TSLA']
    assert posts[1]['ticker_mentions'] == ['MSFT']
    assert posts[2]['ticker_mentions'] == ['AAPL']
    # Verify the JSON-path-compatible shape is preserved
    assert posts[0]['id'] == 'p1'              # t3_ prefix stripped
    assert posts[0]['author'] == 'u1'          # /u/ prefix stripped
    assert posts[0]['subreddit'] == 'wallstreetbets'
    assert posts[0]['score'] == 0              # RSS doesn't carry score
    assert posts[0]['created_utc'] == 1715990400


def test_extract_tickers_filters_common_words():
    from src.ingestion.reddit_client import extract_tickers
    # The COMMON_WORDS denylist should filter "I", "AM", "PM", "ETF"
    text = "I think $AAPL is the next big play. PM me about ETF picks."
    assert extract_tickers(text) == ['AAPL']


def test_extract_tickers_handles_both_dollar_and_uppercase():
    from src.ingestion.reddit_client import extract_tickers
    text = "Buying NVDA and $AMD today, $MSFT later."
    assert sorted(extract_tickers(text)) == ['AMD', 'MSFT', 'NVDA']


def test_fetch_handles_http_error_returns_empty():
    from src.ingestion.reddit_client import fetch_subreddit_posts
    from urllib.error import HTTPError
    with patch('src.ingestion.reddit_client.urllib.request.urlopen',
               side_effect=HTTPError('u', 503, 'down', {}, None)):
        posts = fetch_subreddit_posts('wallstreetbets', after_utc=0)
    assert posts == []


def test_after_utc_filters_old_posts():
    """Posts older than after_utc should be excluded."""
    from src.ingestion.reddit_client import fetch_subreddit_posts
    with patch('src.ingestion.reddit_client.urllib.request.urlopen',
               return_value=_fake_resp(SAMPLE_REDDIT_ATOM)):
        # after_utc set between post 2 (00:08:20 = 1715990900) and post 3 (00:16:40 = 1715991400)
        posts = fetch_subreddit_posts('wallstreetbets', after_utc=1715991000)
    # Only post 3 should survive
    assert len(posts) == 1
    assert posts[0]['id'] == 'p3'
