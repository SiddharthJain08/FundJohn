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
