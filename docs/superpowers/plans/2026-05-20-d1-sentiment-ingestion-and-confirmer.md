# D1 — Sentiment Ingestion + TradeJohn Confirmer Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a daily `(ticker, date)` sentiment substrate from Reddit + StockTwits + FinBERT-over-news, then wire it into the TradeJohn confirmer's per-ticker prompt context as a veto-only input. Confirmer output semantics stay `keep|cancel` — no multiplier, no boost.

**Architecture:** New orchestrator step `sentiment` between `collect` and `signals`. Sources: Reddit subreddit feeds (no auth) → parse $TICKER mentions; StockTwits per-ticker stream (sparse, only for tickers with ≥3 Reddit mentions); local FinBERT service for `market_news` rows. All output keyed `(ticker, date)`; persisted to Postgres `ticker_sentiment_daily` (fast lookup) + `data/master/sentiment.parquet` (append-only).

**Tech Stack:** Python 3.11, psycopg2, pandas + pyarrow, urllib, pytest. Gated by `OPENCLAW_SENTIMENT_INGEST=1` (step runs) and `OPENCLAW_CONFIRMER_SENTIMENT=1` (confirmer reads).

---

## File structure

| Path | Responsibility |
|---|---|
| `src/database/migrations/106_ticker_sentiment_daily.sql` (new) | Table schema |
| `src/ingestion/resolve_sentiment_universe.py` (new) | Runtime universe assembly (SP500 + held + manifest + watchlist) |
| `src/ingestion/reddit_client.py` (new) | Public unauth Reddit subreddit fetch + ticker mention parser |
| `src/ingestion/stocktwits_client.py` (new) | Public unauth StockTwits per-ticker stream + sentiment-tag aggregator |
| `src/ingestion/social_sentiment_aggregator.py` (new) | Merges Reddit + StockTwits into per-ticker daily rollup |
| `src/ingestion/news_finbert_scorer.py` (new) | Reads today's `market_news`, scores via FinBERT, aggregates per ticker |
| `src/ingestion/sentiment_storage.py` (new) | Postgres upsert + parquet append helpers |
| `scripts/run_sentiment_step.py` (new) | Orchestrator entry point — runs all 4 stages |
| `src/execution/pipeline_orchestrator.py` (modify) | Add `sentiment` step (gated) |
| `src/execution/trade_handoff_builder.py` (modify) | Inject `sentiment` block per proposal |
| `src/agent/prompts/subagents/tradejohn-confirmer.md` (modify) | Append "Sentiment & News Inputs" section |
| `src/execution/tradejohn_confirmer.py` (modify) | Inject sentiment fields into prompt INPUT (gated) |

Tests live under `tests/` following the existing `test_*.py` convention.

---

## Task 1: Migration 106 — `ticker_sentiment_daily` table

**Files:**
- Create: `src/database/migrations/106_ticker_sentiment_daily.sql`

- [ ] **Step 1: Write the migration**

```sql
-- Migration 106: ticker_sentiment_daily — per (ticker, date) social + news sentiment rollup.
-- Fed by scripts/run_sentiment_step.py daily. Consumed by trade_handoff_builder.py
-- to enrich tradejohn_confirmer proposals.

CREATE TABLE IF NOT EXISTS ticker_sentiment_daily (
  ticker                  TEXT NOT NULL,
  date                    DATE NOT NULL,
  -- social (Reddit + StockTwits aggregate)
  social_posts_24h        INT     NOT NULL DEFAULT 0,
  social_bull_ratio       NUMERIC,
  social_bear_ratio       NUMERIC,
  social_unique_authors   INT     NOT NULL DEFAULT 0,
  social_top_themes       JSONB,
  -- news (Tavily-fed, FinBERT-scored)
  news_count_24h          INT     NOT NULL DEFAULT 0,
  news_finbert_pos        INT     NOT NULL DEFAULT 0,
  news_finbert_neu        INT     NOT NULL DEFAULT 0,
  news_finbert_neg        INT     NOT NULL DEFAULT 0,
  news_mean_score         NUMERIC,  -- signed: +1 = fully positive, -1 = fully negative
  news_top_headlines      JSONB,    -- top 3 by |polarity|
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_sentiment_date ON ticker_sentiment_daily(date);
```

- [ ] **Step 2: Apply the migration**

Run:

```bash
psql "$POSTGRES_URI" -f src/database/migrations/106_ticker_sentiment_daily.sql
```

Expected: `CREATE TABLE` + `CREATE INDEX` messages.

- [ ] **Step 3: Verify table exists with the right shape**

Run:

```bash
psql "$POSTGRES_URI" -c "\d ticker_sentiment_daily" | head -25
```

Expected output includes all 12 columns + the PK + the `idx_sentiment_date` index.

- [ ] **Step 4: Commit**

```bash
git add src/database/migrations/106_ticker_sentiment_daily.sql
git commit -m "feat(sentiment): migration 106 — ticker_sentiment_daily table"
```

---

## Task 2: Universe resolver

**Files:**
- Create: `src/ingestion/resolve_sentiment_universe.py`
- Test: `tests/test_resolve_sentiment_universe.py`

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_resolve_sentiment_universe.py"""
from __future__ import annotations
from unittest.mock import patch, MagicMock
import pytest


def _mock_cursor(rows_by_query):
    """Build a cursor whose fetchall returns the next list in sequence per execute()."""
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__  = MagicMock(return_value=False)
    calls = {'i': 0}
    def fetchall_side(*a, **kw):
        idx = calls['i']; calls['i'] += 1
        return rows_by_query[idx] if idx < len(rows_by_query) else []
    def execute_side(sql, params=None):
        return None
    cur.execute = MagicMock(side_effect=execute_side)
    cur.fetchall = MagicMock(side_effect=fetchall_side)
    return cur


def test_universe_union_dedupes_and_sorts():
    from src.ingestion.resolve_sentiment_universe import current_universe
    # 4 queries: sp500, held, manifest, watchlist
    rows = [
        [('AAPL',), ('MSFT',), ('NVDA',)],          # sp500
        [('NVDA',), ('TSLA',)],                      # currently held
        [('TSLA',), ('GOOGL',)],                     # manifest universe
        [('AAPL',), ('AMD',)],                       # watchlists
    ]
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__  = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=_mock_cursor(rows))
    with patch('src.ingestion.resolve_sentiment_universe.psycopg2.connect',
               return_value=conn):
        result = current_universe(postgres_uri='postgres://fake')
    assert result == ['AAPL', 'AMD', 'GOOGL', 'MSFT', 'NVDA', 'TSLA']


def test_universe_handles_empty_subqueries():
    from src.ingestion.resolve_sentiment_universe import current_universe
    rows = [[('AAPL',)], [], [], []]
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__  = MagicMock(return_value=False)
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
```

- [ ] **Step 2: Run the test, see it fail**

Run: `pytest tests/test_resolve_sentiment_universe.py -x`
Expected: FAIL — `ModuleNotFoundError: src.ingestion.resolve_sentiment_universe`.

- [ ] **Step 3: Write the implementation**

```python
"""src/ingestion/resolve_sentiment_universe.py — runtime universe assembly
for the sentiment ingestion step.

The universe expands automatically as new SP500 members, new strategies,
or new held tickers appear. No hardcoded ticker lists.
"""
from __future__ import annotations
import os
from typing import List

import psycopg2


_SP500_QUERY = """
    SELECT DISTINCT ticker FROM market_universe
     WHERE active = TRUE
"""

_HELD_QUERY = """
    SELECT DISTINCT ticker FROM execution_positions
     WHERE status = 'open'
"""

_MANIFEST_QUERY = """
    SELECT DISTINCT ticker
      FROM strategy_universe_membership
     WHERE state IN ('live', 'paper', 'monitoring', 'candidate')
"""

_WATCHLIST_QUERY = """
    SELECT DISTINCT ticker FROM watchlists
"""


def current_universe(postgres_uri: str | None = None) -> List[str]:
    """Return the sorted, deduplicated union of tickers from 4 sources.

    Raises on DB error — sentiment without a universe is meaningless.
    """
    uri = postgres_uri or os.environ['POSTGRES_URI']
    seen: set[str] = set()
    with psycopg2.connect(uri) as conn:
        with conn.cursor() as cur:
            for sql in (_SP500_QUERY, _HELD_QUERY, _MANIFEST_QUERY, _WATCHLIST_QUERY):
                cur.execute(sql)
                for (ticker,) in cur.fetchall():
                    if ticker:
                        seen.add(ticker.upper())
    return sorted(seen)


if __name__ == '__main__':
    import json
    print(json.dumps(current_universe(), indent=2))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_resolve_sentiment_universe.py -x -v`
Expected: PASS — 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/resolve_sentiment_universe.py tests/test_resolve_sentiment_universe.py
git commit -m "feat(sentiment): runtime universe resolver"
```

---

## Task 3: Reddit client

**Files:**
- Create: `src/ingestion/reddit_client.py`
- Test: `tests/test_reddit_client.py`

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_reddit_client.py"""
from __future__ import annotations
from unittest.mock import patch, MagicMock
import json
import pytest


SAMPLE_REDDIT_RESPONSE = {
    "data": {
        "children": [
            {"data": {"id": "p1", "title": "$AAPL is going to moon 🚀",
                      "selftext": "puts on TSLA", "author": "u1",
                      "created_utc": 1716000000, "score": 42}},
            {"data": {"id": "p2", "title": "Why $MSFT is undervalued",
                      "selftext": "Just bought more $MSFT", "author": "u2",
                      "created_utc": 1716000500, "score": 15}},
            {"data": {"id": "p3", "title": "calls on AAPL",
                      "selftext": "", "author": "u1",
                      "created_utc": 1716001000, "score": 7}},
        ]
    }
}


def test_fetch_subreddit_parses_ticker_mentions():
    from src.ingestion.reddit_client import fetch_subreddit_posts
    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.read = MagicMock(return_value=json.dumps(SAMPLE_REDDIT_RESPONSE).encode())
    fake_resp.__enter__ = MagicMock(return_value=fake_resp)
    fake_resp.__exit__  = MagicMock(return_value=False)
    with patch('src.ingestion.reddit_client.urllib.request.urlopen', return_value=fake_resp):
        posts = fetch_subreddit_posts('wallstreetbets', after_utc=1716000000)
    assert len(posts) == 3
    assert posts[0]['ticker_mentions'] == ['AAPL', 'TSLA']
    assert posts[1]['ticker_mentions'] == ['MSFT']
    assert posts[2]['ticker_mentions'] == ['AAPL']


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
```

- [ ] **Step 2: Run test, see it fail**

Run: `pytest tests/test_reddit_client.py -x`
Expected: FAIL — `ModuleNotFoundError: src.ingestion.reddit_client`.

- [ ] **Step 3: Write the implementation**

```python
"""src/ingestion/reddit_client.py — public unauth Reddit subreddit fetch
and ticker-mention parser.

Uses the public JSON endpoint reddit.com/r/<sub>/new.json?limit=100. No
auth needed; conservative 0.4s throttle between requests. Standard polite
User-Agent (the public API rejects requests without one).
"""
from __future__ import annotations
import json
import logging
import re
import time
import urllib.request
import urllib.error
from typing import List, Dict, Iterable

logger = logging.getLogger(__name__)

USER_AGENT      = 'FundJohn-Sentiment/1.0 (+https://github.com/)'
REQUEST_TIMEOUT = 10.0
THROTTLE_SEC    = 0.4

# Words that look like tickers but are common English/forum noise.
# Conservative — better to miss a few than tag $I or $PM as tickers.
COMMON_WORDS = {
    'A', 'I', 'AM', 'PM', 'OR', 'AND', 'BUT', 'THE', 'YOU', 'CEO', 'CFO',
    'IPO', 'YOLO', 'FOMO', 'IMO', 'IMHO', 'TIL', 'WSB', 'ETF', 'TLDR',
    'CEO', 'USD', 'GDP', 'CPI', 'FED', 'EOY', 'YOY', 'WSJ', 'NYT',
    'OK', 'NO', 'SO', 'BE', 'HOT', 'NOW', 'JUST', 'NEW', 'BIG', 'TOP',
    'BUY', 'SELL', 'PUT', 'CALL', 'LONG', 'SHORT', 'BULL', 'BEAR',
    'HOLD', 'DD', 'YTD', 'EPS', 'P', 'E',
}

_TICKER_RE = re.compile(r'\$?\b([A-Z]{1,5})\b')


def extract_tickers(text: str) -> List[str]:
    """Return sorted unique ticker symbols found in `text`."""
    if not text:
        return []
    found = set()
    for m in _TICKER_RE.finditer(text):
        sym = m.group(1)
        if sym in COMMON_WORDS:
            continue
        found.add(sym)
    return sorted(found)


def fetch_subreddit_posts(subreddit: str, after_utc: int,
                          limit: int = 100) -> List[Dict]:
    """Fetch new posts from /r/<subreddit>/new.json. Returns posts with
    `ticker_mentions` populated. On error, returns empty list (caller decides
    how to handle missing data)."""
    url = f'https://www.reddit.com/r/{subreddit}/new.json?limit={limit}'
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            if r.status != 200:
                logger.warning('reddit %s: status %s', subreddit, r.status)
                return []
            body = json.loads(r.read())
    except urllib.error.URLError as e:
        logger.warning('reddit %s: %s', subreddit, e)
        return []
    except Exception as e:
        logger.warning('reddit %s: unexpected %s', subreddit, e)
        return []
    finally:
        time.sleep(THROTTLE_SEC)

    out: List[Dict] = []
    for child in body.get('data', {}).get('children', []):
        d = child.get('data') or {}
        if d.get('created_utc', 0) < after_utc:
            continue
        title = d.get('title') or ''
        body_text = d.get('selftext') or ''
        out.append({
            'id':              d.get('id'),
            'subreddit':       subreddit,
            'title':           title,
            'body':            body_text,
            'author':          d.get('author'),
            'created_utc':     d.get('created_utc'),
            'score':           d.get('score', 0),
            'ticker_mentions': extract_tickers(f'{title} {body_text}'),
        })
    return out


def fetch_multiple_subreddits(subreddits: Iterable[str], after_utc: int) -> List[Dict]:
    """Fetch posts from multiple subreddits, throttled between requests."""
    posts: List[Dict] = []
    for sub in subreddits:
        posts.extend(fetch_subreddit_posts(sub, after_utc))
    return posts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_reddit_client.py -x -v`
Expected: PASS — 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/reddit_client.py tests/test_reddit_client.py
git commit -m "feat(sentiment): Reddit client with ticker-mention parser"
```

---

## Task 4: StockTwits client

**Files:**
- Create: `src/ingestion/stocktwits_client.py`
- Test: `tests/test_stocktwits_client.py`

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_stocktwits_client.py"""
from __future__ import annotations
from unittest.mock import patch, MagicMock
import json


SAMPLE_RESP = {
    "messages": [
        {"id": 1, "body": "AAPL to the moon!", "user": {"username": "u1"},
         "entities": {"sentiment": {"basic": "Bullish"}}},
        {"id": 2, "body": "Selling my AAPL puts", "user": {"username": "u2"},
         "entities": {"sentiment": {"basic": "Bearish"}}},
        {"id": 3, "body": "Just watching", "user": {"username": "u3"},
         "entities": None},  # no sentiment tag
        {"id": 4, "body": "Bullish AAPL", "user": {"username": "u4"},
         "entities": {"sentiment": {"basic": "Bullish"}}},
    ]
}


def test_fetch_ticker_stream_aggregates_sentiment():
    from src.ingestion.stocktwits_client import fetch_ticker_stream
    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.read = MagicMock(return_value=json.dumps(SAMPLE_RESP).encode())
    fake_resp.__enter__ = MagicMock(return_value=fake_resp)
    fake_resp.__exit__  = MagicMock(return_value=False)
    with patch('src.ingestion.stocktwits_client.urllib.request.urlopen', return_value=fake_resp):
        agg = fetch_ticker_stream('AAPL')
    assert agg['ticker']    == 'AAPL'
    assert agg['bull_count'] == 2
    assert agg['bear_count'] == 1
    assert agg['neutral_count'] == 1
    assert agg['total_posts'] == 4
    assert set(agg['authors']) == {'u1', 'u2', 'u3', 'u4'}


def test_fetch_handles_404_returns_empty():
    from src.ingestion.stocktwits_client import fetch_ticker_stream
    from urllib.error import HTTPError
    with patch('src.ingestion.stocktwits_client.urllib.request.urlopen',
               side_effect=HTTPError('u', 404, 'not found', {}, None)):
        agg = fetch_ticker_stream('FAKE')
    assert agg['total_posts'] == 0
    assert agg['bull_count']  == 0


def test_fetch_empty_messages_returns_zeros():
    from src.ingestion.stocktwits_client import fetch_ticker_stream
    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.read = MagicMock(return_value=json.dumps({"messages": []}).encode())
    fake_resp.__enter__ = MagicMock(return_value=fake_resp)
    fake_resp.__exit__  = MagicMock(return_value=False)
    with patch('src.ingestion.stocktwits_client.urllib.request.urlopen', return_value=fake_resp):
        agg = fetch_ticker_stream('AAPL')
    assert agg['total_posts'] == 0
```

- [ ] **Step 2: Run test, see it fail**

Run: `pytest tests/test_stocktwits_client.py -x`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
"""src/ingestion/stocktwits_client.py — public unauth StockTwits per-ticker
sentiment-tag aggregator.

Endpoint: api.stocktwits.com/api/2/streams/symbol/<TICKER>.json
Returns up to 30 most-recent messages with optional Bullish/Bearish tags.
"""
from __future__ import annotations
import json
import logging
import time
import urllib.request
import urllib.error
from typing import Dict, List

logger = logging.getLogger(__name__)

BASE_URL        = 'https://api.stocktwits.com/api/2/streams/symbol'
REQUEST_TIMEOUT = 10.0
THROTTLE_SEC    = 0.4


def fetch_ticker_stream(ticker: str) -> Dict:
    """Fetch + aggregate sentiment tags for a single ticker.

    Returns a dict with bull/bear/neutral counts and unique authors. On any
    HTTP/parsing error, returns the same dict with zeros (treated as
    'no signal' downstream)."""
    empty = {
        'ticker': ticker.upper(),
        'bull_count': 0,
        'bear_count': 0,
        'neutral_count': 0,
        'total_posts': 0,
        'authors': [],
    }
    url = f'{BASE_URL}/{ticker.upper()}.json'
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as r:
            if r.status != 200:
                logger.warning('stocktwits %s: status %s', ticker, r.status)
                return empty
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        logger.warning('stocktwits %s: HTTP %s', ticker, e.code)
        return empty
    except Exception as e:
        logger.warning('stocktwits %s: %s', ticker, e)
        return empty
    finally:
        time.sleep(THROTTLE_SEC)

    bull, bear, neutral = 0, 0, 0
    authors = set()
    messages = body.get('messages') or []
    for m in messages:
        u = (m.get('user') or {}).get('username')
        if u:
            authors.add(u)
        ent = m.get('entities') or {}
        sent = (ent.get('sentiment') if isinstance(ent, dict) else None) or {}
        basic = sent.get('basic') if isinstance(sent, dict) else None
        if basic == 'Bullish':
            bull += 1
        elif basic == 'Bearish':
            bear += 1
        else:
            neutral += 1
    return {
        'ticker':         ticker.upper(),
        'bull_count':     bull,
        'bear_count':     bear,
        'neutral_count':  neutral,
        'total_posts':    len(messages),
        'authors':        sorted(authors),
    }


def fetch_many_tickers(tickers: List[str]) -> List[Dict]:
    """Throttled batch fetch. Returns one dict per ticker (with zeros if fetch failed)."""
    return [fetch_ticker_stream(t) for t in tickers]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_stocktwits_client.py -x -v`
Expected: PASS — 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/stocktwits_client.py tests/test_stocktwits_client.py
git commit -m "feat(sentiment): StockTwits client with sentiment-tag aggregation"
```

---

## Task 5: Social sentiment aggregator

**Files:**
- Create: `src/ingestion/social_sentiment_aggregator.py`
- Test: `tests/test_social_sentiment_aggregator.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test, see it fail**

Run: `pytest tests/test_social_sentiment_aggregator.py -x`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
"""src/ingestion/social_sentiment_aggregator.py — merges Reddit subreddit
posts + StockTwits per-ticker streams into one (ticker, date) row.

StockTwits is sparse: only queried for tickers with ≥3 Reddit mentions
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_social_sentiment_aggregator.py -x -v`
Expected: PASS — 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/social_sentiment_aggregator.py tests/test_social_sentiment_aggregator.py
git commit -m "feat(sentiment): social sentiment aggregator (Reddit + StockTwits)"
```

---

## Task 6: FinBERT news scorer

**Files:**
- Create: `src/ingestion/news_finbert_scorer.py`
- Test: `tests/test_news_finbert_scorer.py`

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_news_finbert_scorer.py"""
from __future__ import annotations
from unittest.mock import patch, MagicMock


NEWS_ROWS = [
    {'ticker': 'AAPL', 'headline': 'Apple beats earnings', 'summary': '',
     'published_at': '2026-05-20T08:00:00Z'},
    {'ticker': 'AAPL', 'headline': 'Apple recalls iPhone batteries', 'summary': '',
     'published_at': '2026-05-20T09:00:00Z'},
    {'ticker': 'AAPL', 'headline': 'Apple to launch new product', 'summary': '',
     'published_at': '2026-05-20T10:00:00Z'},
    {'ticker': 'TSLA', 'headline': 'Tesla recalls 200k vehicles', 'summary': '',
     'published_at': '2026-05-20T07:00:00Z'},
]

# FinBERT mock: returns positive for "beats" + "launch", negative for "recall",
# neutral otherwise.
def mock_finbert_score(text):
    text_l = text.lower()
    if 'recall' in text_l:
        return {'label': 'Negative', 'score': 0.92}
    if 'beats' in text_l or 'launch' in text_l:
        return {'label': 'Positive', 'score': 0.85}
    return {'label': 'Neutral', 'score': 0.60}


def test_scorer_aggregates_per_ticker():
    from src.ingestion.news_finbert_scorer import score_news_rows
    with patch('src.ingestion.news_finbert_scorer.FinbertClient') as MC:
        MC.return_value.score = MagicMock(side_effect=mock_finbert_score)
        result = score_news_rows(NEWS_ROWS)
    aapl = next(r for r in result if r['ticker'] == 'AAPL')
    assert aapl['news_count_24h']     == 3
    assert aapl['news_finbert_pos']   == 2  # "beats" + "launch"
    assert aapl['news_finbert_neg']   == 1  # "recall"
    assert aapl['news_finbert_neu']   == 0
    # signed mean: (2 * +0.85 + 1 * -0.92 + 0 * 0) / 3 ≈ +0.26
    assert abs(aapl['news_mean_score'] - ((2*0.85 - 0.92) / 3)) < 1e-6
    # top headlines: highest |score| first → recall (|0.92|), then beats/launch (|0.85|)
    assert aapl['news_top_headlines'][0].startswith('Apple recalls')


def test_scorer_handles_finbert_error_returns_zeros():
    from src.ingestion.news_finbert_scorer import score_news_rows
    with patch('src.ingestion.news_finbert_scorer.FinbertClient') as MC:
        MC.return_value.score = MagicMock(side_effect=RuntimeError('service down'))
        result = score_news_rows(NEWS_ROWS)
    # On error, every ticker gets zeros + None mean
    for r in result:
        assert r['news_count_24h']    == 0
        assert r['news_finbert_pos']  == 0
        assert r['news_mean_score'] is None


def test_scorer_returns_empty_list_when_no_news():
    from src.ingestion.news_finbert_scorer import score_news_rows
    with patch('src.ingestion.news_finbert_scorer.FinbertClient') as MC:
        MC.return_value.score = MagicMock(return_value={'label': 'Neutral', 'score': 0.5})
        assert score_news_rows([]) == []
```

- [ ] **Step 2: Run test, see it fail**

Run: `pytest tests/test_news_finbert_scorer.py -x`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
"""src/ingestion/news_finbert_scorer.py — scores `market_news` rows via the
local FinBERT-Tone service and aggregates per (ticker, date).

Output one dict per ticker with counts in each polarity bucket plus a
signed mean polarity score.
"""
from __future__ import annotations
import logging
from collections import defaultdict
from typing import Dict, List, Optional

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
        try:
            scored = client.score(text)
        except Exception as e:
            logger.warning('finbert score failed for %s: %s', ticker, e)
            per_ticker[ticker]['error'] = True
            continue
        label = scored.get('label', 'Neutral')
        score = float(scored.get('score', 0.0))
        bucket = per_ticker[ticker]
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_news_finbert_scorer.py -x -v`
Expected: PASS — 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/news_finbert_scorer.py tests/test_news_finbert_scorer.py
git commit -m "feat(sentiment): news FinBERT scorer + per-ticker rollup"
```

---

## Task 7: Storage layer (Postgres upsert + parquet append)

**Files:**
- Create: `src/ingestion/sentiment_storage.py`
- Test: `tests/test_sentiment_storage.py`

- [ ] **Step 1: Write the failing test**

```python
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
    # 2 rows → 2 cursor.execute() calls
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
```

- [ ] **Step 2: Run test, see it fail**

Run: `pytest tests/test_sentiment_storage.py -x`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
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
    # Decode top_themes / top_headlines back from JSON to native python objs
    # (parquet preserves both JSON-strings and lists/dicts; for consistency
    # we store them as Python objects in the parquet column).
    df_new['social_top_themes']    = df_new['social_top_themes'].apply(json.loads)
    df_new['news_top_headlines']   = df_new['news_top_headlines'].apply(json.loads)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sentiment_storage.py -x -v`
Expected: PASS — 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/sentiment_storage.py tests/test_sentiment_storage.py
git commit -m "feat(sentiment): storage layer — Postgres upsert + parquet append"
```

---

## Task 8: Orchestration script — `scripts/run_sentiment_step.py`

**Files:**
- Create: `scripts/run_sentiment_step.py`
- Test: `tests/test_run_sentiment_step.py`

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_run_sentiment_step.py"""
from __future__ import annotations
from unittest.mock import patch, MagicMock


def test_run_sentiment_step_happy_path():
    """Stub all four upstream modules; verify orchestration produces upsert+parquet calls."""
    import importlib
    mod = importlib.import_module('scripts.run_sentiment_step')

    fake_universe   = ['AAPL', 'TSLA', 'MSFT']
    fake_reddit     = [{'ticker_mentions': ['AAPL', 'AAPL', 'AAPL', 'MSFT'],
                        'subreddit': 'wsb', 'author': 'u1', 'title': '', 'body': ''}] * 4
    fake_stocktwits = {'AAPL': {'ticker': 'AAPL', 'bull_count': 10, 'bear_count': 4,
                                 'neutral_count': 6, 'total_posts': 20,
                                 'authors': ['s1', 's2']}}
    fake_news       = [{'ticker': 'AAPL', 'headline': 'Apple beats earnings'}]
    fake_news_scored = [{'ticker': 'AAPL', 'news_count_24h': 1, 'news_finbert_pos': 1,
                          'news_finbert_neu': 0, 'news_finbert_neg': 0,
                          'news_mean_score': 0.85, 'news_top_headlines': ['Apple beats earnings']}]

    with patch.object(mod, 'current_universe', return_value=fake_universe), \
         patch.object(mod, 'fetch_multiple_subreddits', return_value=fake_reddit), \
         patch.object(mod, 'select_sparse_tickers', return_value=['AAPL']), \
         patch.object(mod, 'fetch_many_tickers', return_value=[fake_stocktwits['AAPL']]), \
         patch.object(mod, '_load_todays_news', return_value=fake_news), \
         patch.object(mod, 'score_news_rows', return_value=fake_news_scored), \
         patch.object(mod, 'upsert_postgres', return_value=3) as up_pg, \
         patch.object(mod, 'append_parquet',  return_value=3) as up_pq:
        rc = mod.main(['--date', '2026-05-20'])
    assert rc == 0
    assert up_pg.call_count == 1
    assert up_pq.call_count == 1
    # Check both got 3 rows (AAPL, MSFT from reddit, AAPL has stocktwits & news)
    rows_arg = up_pg.call_args[0][0]
    assert {r['ticker'] for r in rows_arg} == {'AAPL', 'MSFT'}


def test_run_sentiment_step_universe_failure_aborts():
    import importlib
    mod = importlib.import_module('scripts.run_sentiment_step')
    with patch.object(mod, 'current_universe', side_effect=ConnectionError('db down')):
        rc = mod.main(['--date', '2026-05-20'])
    assert rc == 2
```

- [ ] **Step 2: Run test, see it fail**

Run: `pytest tests/test_run_sentiment_step.py -x`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
"""scripts/run_sentiment_step.py — orchestrator entry point for the
daily `sentiment` pipeline step.

Stages:
  1. Resolve the runtime universe
  2. Fetch Reddit posts from r/wsb + r/stocks + r/investing
  3. Aggregate Reddit ticker mentions; identify sparse-StockTwits set (≥3 mentions)
  4. Fetch StockTwits streams for the sparse set
  5. Aggregate Reddit + StockTwits per ticker
  6. Load today's market_news rows for the universe
  7. Score news headlines via FinBERT, aggregate per ticker
  8. Merge social + news per ticker
  9. Upsert to ticker_sentiment_daily + append to sentiment.parquet

Exit codes:
  0 — success
  1 — partial (some sources failed but data was persisted)
  2 — abort (universe lookup failed; nothing to persist)
"""
from __future__ import annotations
import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ingestion.resolve_sentiment_universe import current_universe
from src.ingestion.reddit_client import fetch_multiple_subreddits
from src.ingestion.stocktwits_client import fetch_many_tickers
from src.ingestion.social_sentiment_aggregator import (
    select_sparse_tickers, aggregate_all
)
from src.ingestion.news_finbert_scorer import score_news_rows
from src.ingestion.sentiment_storage import upsert_postgres, append_parquet

logger = logging.getLogger(__name__)

SUBREDDITS = ('wallstreetbets', 'stocks', 'investing')


def _load_todays_news(postgres_uri: str, run_date: str) -> List[Dict]:
    """Pull today's market_news rows for downstream FinBERT scoring."""
    import psycopg2, psycopg2.extras
    with psycopg2.connect(postgres_uri) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT ticker, headline, summary, published_at
                  FROM market_news
                 WHERE published_at::date = %s::date
                   AND ticker IS NOT NULL
            """, (run_date,))
            return [dict(r) for r in cur.fetchall()]


def _merge_social_and_news(social_rows: List[Dict], news_rows: List[Dict]) -> List[Dict]:
    """Outer-join social and news rows on ticker."""
    by_ticker: Dict[str, Dict] = {}
    for r in social_rows:
        by_ticker[r['ticker']] = dict(r)
    for r in news_rows:
        existing = by_ticker.setdefault(r['ticker'], {'ticker': r['ticker'],
                                                       'social_posts_24h': 0,
                                                       'social_unique_authors': 0,
                                                       'social_top_themes': {}})
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
    args = ap.parse_args(argv)
    run_date = args.date
    pg_uri   = os.environ['POSTGRES_URI']

    # Stage 1: universe
    try:
        universe = current_universe(pg_uri)
    except Exception as e:
        logger.error('universe lookup failed: %s', e)
        return 2
    logger.info('sentiment: universe %d tickers', len(universe))
    universe_set = set(universe)

    # Stage 2 + 3: Reddit
    twenty_four_h_ago_utc = int((datetime.now(timezone.utc) - timedelta(hours=24)).timestamp())
    reddit_posts = fetch_multiple_subreddits(SUBREDDITS, twenty_four_h_ago_utc)
    # Filter to universe tickers only
    for p in reddit_posts:
        p['ticker_mentions'] = [t for t in p.get('ticker_mentions', []) if t in universe_set]
    logger.info('sentiment: %d reddit posts after universe filter', len(reddit_posts))

    # Stage 4: StockTwits (sparse)
    sparse_tickers = select_sparse_tickers(reddit_posts, min_mentions=3)
    st_results     = fetch_many_tickers(sparse_tickers)
    stocktwits_by  = {r['ticker']: r for r in st_results}
    logger.info('sentiment: %d StockTwits tickers queried', len(sparse_tickers))

    # Stage 5: aggregate social
    social_rows = aggregate_all(reddit_posts, stocktwits_by, min_mentions_for_st=3)
    logger.info('sentiment: %d social rows aggregated', len(social_rows))

    # Stage 6 + 7: news → FinBERT
    news_rows_raw = _load_todays_news(pg_uri, run_date)
    # Filter to universe
    news_rows_raw = [r for r in news_rows_raw if (r.get('ticker') or '').upper() in universe_set]
    news_rows     = score_news_rows(news_rows_raw)
    logger.info('sentiment: %d news rows scored', len(news_rows))

    # Stage 8: merge
    merged = _merge_social_and_news(social_rows, news_rows)
    logger.info('sentiment: %d total ticker rows ready for persist', len(merged))

    # Stage 9: persist
    upsert_postgres(merged, run_date, pg_uri)
    append_parquet(merged, run_date)

    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='[sentiment %(asctime)s] %(message)s')
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_run_sentiment_step.py -x -v`
Expected: PASS — 2 tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_sentiment_step.py tests/test_run_sentiment_step.py
git commit -m "feat(sentiment): orchestration script for the sentiment pipeline step"
```

---

## Task 9: Pipeline orchestrator — add the `sentiment` step

**Files:**
- Modify: `src/execution/pipeline_orchestrator.py`

- [ ] **Step 1: Write a failing test**

```python
"""tests/test_pipeline_orchestrator_sentiment_step.py"""
from __future__ import annotations
import importlib, os
from unittest.mock import patch


def test_sentiment_step_present_when_gate_on():
    os.environ['OPENCLAW_SENTIMENT_INGEST'] = '1'
    import importlib
    if 'src.execution.pipeline_orchestrator' in __import__('sys').modules:
        importlib.reload(__import__('sys').modules['src.execution.pipeline_orchestrator'])
    mod = importlib.import_module('src.execution.pipeline_orchestrator')
    keys = [k for k, _ in mod.STEPS]
    assert 'sentiment' in keys
    # Order: between collect and signals
    assert keys.index('sentiment') == keys.index('collect') + 1
    assert keys.index('sentiment') == keys.index('signals') - 1


def test_sentiment_step_absent_when_gate_off():
    os.environ.pop('OPENCLAW_SENTIMENT_INGEST', None)
    import importlib
    if 'src.execution.pipeline_orchestrator' in __import__('sys').modules:
        importlib.reload(__import__('sys').modules['src.execution.pipeline_orchestrator'])
    mod = importlib.import_module('src.execution.pipeline_orchestrator')
    keys = [k for k, _ in mod.STEPS]
    assert 'sentiment' not in keys
```

- [ ] **Step 2: Run test, see it fail**

Run: `pytest tests/test_pipeline_orchestrator_sentiment_step.py -x`
Expected: FAIL — `sentiment` not in STEPS keys.

- [ ] **Step 3: Modify pipeline_orchestrator.py**

Open `src/execution/pipeline_orchestrator.py`. Find the `STEPS = [...]` list (currently 10 entries — collect/signals/ic_gate/handoff/trade/alpaca/reconcile/report/pyportfolioopt_shadow/health). Replace the literal list with a function that inserts the sentiment step when gated:

```python
def _build_steps() -> list[tuple[str, str]]:
    """Build the canonical 10-step list. Optional `sentiment` step inserted
    between collect and signals when OPENCLAW_SENTIMENT_INGEST=1.
    """
    base = [
        ('collect',              'run_collector_once'),
        ('signals',              'engine'),
        ('ic_gate',              'ic_gate_runner'),
        ('handoff',              'trade_handoff_builder'),
        ('trade',                'regime_blended_sizer_live'),
        ('alpaca',               'alpaca_executor'),
        ('reconcile',            'alpaca_reconcile'),
        ('report',               'send_report'),
        ('pyportfolioopt_shadow','pyportfolioopt_shadow'),
        ('health',               'daily_health_digest'),
    ]
    if os.environ.get('OPENCLAW_SENTIMENT_INGEST') == '1':
        # Insert sentiment immediately after `collect`
        insert_at = next(i for i, (k, _) in enumerate(base) if k == 'collect') + 1
        base.insert(insert_at, ('sentiment', 'run_sentiment_step'))
    return base


STEPS = _build_steps()
```

Also update `STEP_FAILURE_CHANNEL` to include the new step (data-alerts):

```python
STEP_FAILURE_CHANNEL = {
    'collect':     'data-alerts',
    'sentiment':   'data-alerts',   # <-- NEW
    'signals':     'data-alerts',
    'ic_gate':     'trade-reports',
    'handoff':     'trade-reports',
    'trade':       'trade-reports',
    'alpaca':      'trade-reports',
    'reconcile':   'trade-reports',
    'report':      'trade-reports',
    'health':      'pipeline-feed',
}
```

And add `STEP_AGENTS['sentiment']` entry:

```python
STEP_AGENTS = {
    # ... existing entries ...
    'sentiment':   ('databot', f'Scraping social + scoring news: {run_date}', None),
    # ... existing entries unchanged ...
}
```

The script path resolver — `_resolve_script` — needs to know `run_sentiment_step` lives in `scripts/`, not `src/execution/`. Check how other `scripts/` entries are resolved (e.g. `run_collector_once`). Most likely there's a `py_pipe = ROOT / 'scripts' / f'{script}.py'` branch; verify by reading lines 490-525 (the resolver block). If the resolver already checks both `scripts/` and `src/execution/`, no change needed. If not, extend it.

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_pipeline_orchestrator_sentiment_step.py -x -v
pytest tests/test_dry_run_dataflow.py -x -v  # regression — orchestrator step layout
```

Expected: both pass.

- [ ] **Step 5: Commit**

```bash
git add src/execution/pipeline_orchestrator.py tests/test_pipeline_orchestrator_sentiment_step.py
git commit -m "feat(sentiment): wire sentiment step into pipeline orchestrator (gated)"
```

---

## Task 10: Handoff enrichment — inject sentiment block per proposal

**Files:**
- Modify: `src/execution/trade_handoff_builder.py`
- Test: `tests/test_trade_handoff_builder_sentiment.py`

- [ ] **Step 1: Write a failing test**

```python
"""tests/test_trade_handoff_builder_sentiment.py"""
from __future__ import annotations
from unittest.mock import patch, MagicMock


def test_load_sentiment_for_tickers_returns_dict():
    from src.execution.trade_handoff_builder import _load_sentiment_for_tickers
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__  = MagicMock(return_value=False)
    cur.execute   = MagicMock()
    cur.fetchall  = MagicMock(return_value=[
        ('AAPL', 23, 0.43, 0.17, 5, {'wallstreetbets': 2},
         3, 2, 0, 1, 0.26, ['Apple recalls', 'Apple beats', 'Apple launches']),
        ('TSLA', 10, 0.5, 0.5, 4, {'wallstreetbets': 1},
         1, 0, 0, 1, -0.91, ['Tesla recalls 200k vehicles']),
    ])
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__  = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    with patch('src.execution.trade_handoff_builder.psycopg2.connect', return_value=conn):
        result = _load_sentiment_for_tickers(['AAPL', 'TSLA'], run_date='2026-05-20',
                                              postgres_uri='pg://fake')
    assert 'AAPL' in result
    assert result['AAPL']['social_posts_24h']    == 23
    assert result['AAPL']['news_mean_score']     == 0.26
    assert result['TSLA']['news_top_headlines'][0] == 'Tesla recalls 200k vehicles'


def test_load_sentiment_returns_empty_when_no_rows():
    from src.execution.trade_handoff_builder import _load_sentiment_for_tickers
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__  = MagicMock(return_value=False)
    cur.execute   = MagicMock()
    cur.fetchall  = MagicMock(return_value=[])
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__  = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    with patch('src.execution.trade_handoff_builder.psycopg2.connect', return_value=conn):
        result = _load_sentiment_for_tickers([], run_date='2026-05-20', postgres_uri='pg://fake')
    assert result == {}
```

- [ ] **Step 2: Run test, see it fail**

Run: `pytest tests/test_trade_handoff_builder_sentiment.py -x`
Expected: FAIL — `_load_sentiment_for_tickers` doesn't exist.

- [ ] **Step 3: Add `_load_sentiment_for_tickers` to `trade_handoff_builder.py`**

Open `src/execution/trade_handoff_builder.py`. Near the other `_load_*` helpers, add:

```python
def _load_sentiment_for_tickers(tickers: list[str], run_date: str,
                                 postgres_uri: str) -> dict[str, dict]:
    """Load ticker_sentiment_daily rows for the given tickers + date.

    Returns map: ticker -> {social_posts_24h, social_bull_ratio, social_bear_ratio,
                            news_finbert_pos, news_finbert_neu, news_finbert_neg,
                            news_mean_score, news_top_headlines}.
    Tickers without a row are absent from the result (caller handles missing).
    """
    if not tickers:
        return {}
    import psycopg2
    with psycopg2.connect(postgres_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ticker, social_posts_24h, social_bull_ratio, social_bear_ratio,
                       social_unique_authors, social_top_themes,
                       news_count_24h, news_finbert_pos, news_finbert_neu, news_finbert_neg,
                       news_mean_score, news_top_headlines
                  FROM ticker_sentiment_daily
                 WHERE ticker = ANY(%s) AND date = %s::date
            """, (list(tickers), run_date))
            rows = cur.fetchall()
    out = {}
    for row in rows:
        (ticker, posts, bull_r, bear_r, authors, themes,
         n_count, n_pos, n_neu, n_neg, n_mean, n_top) = row
        out[ticker] = {
            'social_posts_24h':      posts,
            'social_bull_ratio':     float(bull_r) if bull_r is not None else None,
            'social_bear_ratio':     float(bear_r) if bear_r is not None else None,
            'social_unique_authors': authors,
            'social_top_themes':     themes,
            'news_count_24h':        n_count,
            'news_finbert_pos':      n_pos,
            'news_finbert_neu':      n_neu,
            'news_finbert_neg':      n_neg,
            'news_mean_score':       float(n_mean) if n_mean is not None else None,
            'news_top_headlines':    n_top or [],
        }
    return out
```

- [ ] **Step 4: Wire it into the proposal builder**

Locate the function in `trade_handoff_builder.py` that produces the per-proposal dict (search for `'context'` or `'signals': [...]` block in the structured handoff write — the one that becomes `handoff:{run_date}:structured`).

Find the loop that builds each proposal. After the proposal dict is built, add a sentiment block. Example transform — search for where proposals are assembled, e.g.:

```python
# Inside the loop building structured_handoff['signals']:
proposal = {
    'ticker':     ticker,
    'strategy_id': strategy_id,
    # ... existing fields ...
}
# NEW: append sentiment when ticker has data
sentiment_block = sentiment_by_ticker.get(ticker)
if sentiment_block is not None:
    proposal['sentiment'] = sentiment_block
structured_handoff['signals'].append(proposal)
```

Earlier in the same function, before the loop starts, bulk-load sentiment for all tickers:

```python
all_tickers = sorted({s.get('ticker') for s in signals if s.get('ticker')})
sentiment_by_ticker = _load_sentiment_for_tickers(all_tickers, run_date, postgres_uri)
```

- [ ] **Step 5: Run tests**

Run:

```bash
pytest tests/test_trade_handoff_builder_sentiment.py tests/test_trade_handoff_builder.py -x -v
```

Expected: PASS — including any existing handoff tests (no regression in proposal shape — sentiment is an additive optional key).

- [ ] **Step 6: Commit**

```bash
git add src/execution/trade_handoff_builder.py tests/test_trade_handoff_builder_sentiment.py
git commit -m "feat(sentiment): inject sentiment block per handoff proposal"
```

---

## Task 11: Confirmer prompt addendum + parser injection

**Files:**
- Modify: `src/agent/prompts/subagents/tradejohn-confirmer.md`
- Modify: `src/execution/tradejohn_confirmer.py`
- Test: `tests/test_tradejohn_confirmer_sentiment.py`

- [ ] **Step 1: Write a failing test**

```python
"""tests/test_tradejohn_confirmer_sentiment.py"""
from __future__ import annotations
import os
from unittest.mock import patch


def test_prompt_includes_sentiment_block_when_gate_on(monkeypatch):
    monkeypatch.setenv('OPENCLAW_CONFIRMER_SENTIMENT', '1')
    from src.execution.tradejohn_confirmer import _build_prompt
    proposals = [{
        'ticker': 'AAPL',
        'preliminary_size_usd': 1000.0,
        'direction': 1,
        'sentiment': {
            'social_posts_24h': 23, 'social_bull_ratio': 0.43, 'social_bear_ratio': 0.17,
            'news_finbert_pos': 2, 'news_finbert_neu': 0, 'news_finbert_neg': 1,
            'news_mean_score': 0.26, 'news_top_headlines': ['Apple beats earnings']
        },
    }]
    prompt = _build_prompt(proposals)
    assert 'Sentiment & News Inputs' in prompt
    assert 'social_bull_ratio' in prompt
    assert 'news_mean_score' in prompt
    # And the per-ticker JSON is still present
    assert '"AAPL"' in prompt or "'AAPL'" in prompt


def test_prompt_omits_sentiment_block_when_gate_off(monkeypatch):
    monkeypatch.delenv('OPENCLAW_CONFIRMER_SENTIMENT', raising=False)
    from src.execution.tradejohn_confirmer import _build_prompt
    proposals = [{'ticker': 'AAPL', 'preliminary_size_usd': 1000.0, 'direction': 1}]
    prompt = _build_prompt(proposals)
    assert 'Sentiment & News Inputs' not in prompt


def test_confirmer_strips_sentiment_keys_when_gate_off(monkeypatch):
    """Even if upstream injected sentiment, the gate-off path must drop it before LLM sees."""
    monkeypatch.delenv('OPENCLAW_CONFIRMER_SENTIMENT', raising=False)
    from src.execution.tradejohn_confirmer import _build_prompt
    proposals = [{
        'ticker': 'AAPL', 'preliminary_size_usd': 1000.0, 'direction': 1,
        'sentiment': {'social_posts_24h': 23, 'news_mean_score': 0.26},
    }]
    prompt = _build_prompt(proposals)
    assert 'news_mean_score' not in prompt
    assert 'social_posts_24h' not in prompt
```

- [ ] **Step 2: Run test, see it fail**

Run: `pytest tests/test_tradejohn_confirmer_sentiment.py -x`
Expected: FAIL — prompt does not yet include "Sentiment & News Inputs" section.

- [ ] **Step 3: Update the prompt template**

Open `src/agent/prompts/subagents/tradejohn-confirmer.md`. Append the new section AFTER the existing "Do NOT cancel for" block, BEFORE the "Bias" section:

```markdown

## Sentiment & News Inputs

When present, each ticker proposal carries a `sentiment` block:
  - `social_posts_24h`, `social_bull_ratio`, `social_bear_ratio`
  - `news_finbert_pos` / `news_finbert_neu` / `news_finbert_neg`
  - `news_mean_score` (signed: +1 fully positive, -1 fully negative)
  - `news_top_headlines` (top 3 by |polarity|)

CANCEL when ANY of the following holds, in addition to the rules above:
  1. `news_top_headlines` contains a hard-veto event (fraud, FDA rejection,
     bankruptcy, regulatory action, restatement, CEO departure for cause,
     catastrophic operational failure)
  2. `news_mean_score` ≤ −0.5 AND signal direction is LONG
  3. `news_mean_score` ≥ +0.5 AND signal direction is SHORT
  4. `social_bear_ratio` ≥ 0.7 AND `social_posts_24h` ≥ 50 AND signal is LONG
  5. `social_bull_ratio` ≥ 0.7 AND `social_posts_24h` ≥ 50 AND signal is SHORT

KEEP otherwise. Default is keep.

DO NOT cancel for: earnings (handled separately), sector moves, macro news,
broad-market sentiment, or low-volume social (posts_24h < 50 = noise).
```

- [ ] **Step 4: Modify `_build_prompt` in tradejohn_confirmer.py to gate sentiment**

Open `src/execution/tradejohn_confirmer.py`. Locate the existing `_build_prompt` function (lines ~33-37). Replace with:

```python
import os  # ensure os is imported at module top

def _build_prompt(proposals: list[dict]) -> str:
    """Compose the per-cycle prompt from the static template + per-ticker proposals.

    When OPENCLAW_CONFIRMER_SENTIMENT=1, the sentiment block (if present on
    each proposal) is injected as a `sentiment` field in the per-ticker
    payload. When the gate is OFF, all `sentiment` keys are stripped so the
    LLM never sees them and the prompt addendum is suppressed.
    """
    template = PROMPT_PATH.read_text() if PROMPT_PATH.exists() else _FALLBACK_TEMPLATE
    gate_on  = os.environ.get('OPENCLAW_CONFIRMER_SENTIMENT') == '1'

    if not gate_on:
        # Strip sentiment keys + suppress the addendum
        cleaned = [{k: v for k, v in p.items() if k != 'sentiment'} for p in proposals]
        # Drop the "## Sentiment & News Inputs" section from the template too
        template_lines = template.split('\n')
        out_lines: list[str] = []
        skip = False
        for line in template_lines:
            if line.strip().startswith('## Sentiment & News Inputs'):
                skip = True
                continue
            if skip and line.strip().startswith('## '):
                skip = False
            if not skip:
                out_lines.append(line)
        template = '\n'.join(out_lines)
        payload = {'proposals': cleaned}
    else:
        payload = {'proposals': proposals}

    return template + '\n\n## INPUT\n```json\n' + json.dumps(payload, indent=2, default=str) + '\n```'
```

- [ ] **Step 5: Run tests**

Run:

```bash
pytest tests/test_tradejohn_confirmer_sentiment.py -x -v
pytest tests/test_tradejohn_confirmer.py -x -v 2>/dev/null || true  # regression on existing tests if any
```

Expected: PASS — 3 new tests pass; any pre-existing confirmer tests still pass (the gate-off path is byte-equivalent to today's behavior).

- [ ] **Step 6: Commit**

```bash
git add src/agent/prompts/subagents/tradejohn-confirmer.md \
        src/execution/tradejohn_confirmer.py \
        tests/test_tradejohn_confirmer_sentiment.py
git commit -m "feat(sentiment): confirmer prompt addendum + gated injection"
```

---

## Done

All 11 tasks complete. D1 ships behind two default-OFF gates:

1. `OPENCLAW_SENTIMENT_INGEST=1` — runs the `sentiment` step daily (populates table + parquet)
2. `OPENCLAW_CONFIRMER_SENTIMENT=1` — confirmer reads sentiment + addendum is exposed

**Recommended operator rollout:**
1. Apply migration 106
2. Set `OPENCLAW_SENTIMENT_INGEST=1`, restart `johnbot.service`
3. Let it run for ≥1 week; review `ticker_sentiment_daily` data quality and any `#data-alerts` warnings
4. Set `OPENCLAW_CONFIRMER_SENTIMENT=1`, restart, observe one daily cycle's veto digest in `#trade-reports`
5. If anomalous veto rate (>5% of proposals), turn off and inspect the prompt + sentiment data before re-enabling

**Smoke test (manual, post-deploy):**

```bash
# 1. Run the sentiment step end-to-end against today's data
OPENCLAW_SENTIMENT_INGEST=1 POSTGRES_URI=$POSTGRES_URI python3 scripts/run_sentiment_step.py

# 2. Verify rows landed
psql "$POSTGRES_URI" -c "SELECT date, COUNT(*) FROM ticker_sentiment_daily WHERE date = CURRENT_DATE GROUP BY date;"

# 3. Verify parquet append
python3 -c "import pandas as pd; df=pd.read_parquet('data/master/sentiment.parquet'); print(df.shape); print(df.tail(3))"
```
