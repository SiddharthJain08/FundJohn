"""src/ingestion/stocktwits_client.py — public unauth StockTwits per-ticker
sentiment-tag aggregator.

Endpoint: api.stocktwits.com/api/2/streams/symbol/<TICKER>.json
Returns up to 30 most-recent messages with optional Bullish/Bearish tags.

Note: the endpoint's WAF rejects Python's default `Python-urllib/3.x`
User-Agent with HTTP 403. We send the same polite UA as reddit_client.
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
USER_AGENT      = 'FundJohn-Sentiment/1.0 (+https://github.com/)'
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
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
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
