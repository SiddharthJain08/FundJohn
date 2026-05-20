"""src/ingestion/reddit_client.py — public unauth Reddit subreddit fetch
and ticker-mention parser.

Uses the Atom RSS feed at reddit.com/r/<sub>/new.rss?limit=<N>. Reddit's
public JSON endpoint started returning HTTP 403 to unauth requests in 2026
(across all UAs); the Atom feed still serves up to 50 entries per request
with no auth. Output dict shape is identical to the legacy JSON path so
downstream aggregators don't change. `score` is absent from RSS and reported
as 0 — the aggregator doesn't read it anyway.

0.4s throttle between requests; polite User-Agent.
"""
from __future__ import annotations
import logging
import re
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import List, Dict, Iterable

logger = logging.getLogger(__name__)

USER_AGENT      = 'FundJohn-Sentiment/1.0 (+https://github.com/)'
REQUEST_TIMEOUT = 10.0
THROTTLE_SEC    = 0.4
ATOM_NS         = {'a': 'http://www.w3.org/2005/Atom'}

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
_HTML_TAG_RE = re.compile(r'<[^>]+>')


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


def _strip_html(html: str) -> str:
    """Atom <content> is HTML; reduce to visible text for ticker parsing."""
    if not html:
        return ''
    return _HTML_TAG_RE.sub(' ', html)


def _parse_iso_utc(s: str) -> int:
    """ISO-8601 → unix int (UTC). Returns 0 on parse failure (post will be filtered)."""
    try:
        # Atom emits "2026-05-20T14:43:02+00:00"; fromisoformat handles it on 3.11+.
        return int(datetime.fromisoformat(s).timestamp())
    except (ValueError, TypeError):
        return 0


def _parse_atom(body: bytes, subreddit: str, after_utc: int) -> List[Dict]:
    """Convert an Atom feed body into the standard post-dict list."""
    out: List[Dict] = []
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        logger.warning('reddit %s: malformed atom: %s', subreddit, e)
        return out
    for entry in root.findall('a:entry', ATOM_NS):
        id_el      = entry.find('a:id', ATOM_NS)
        title_el   = entry.find('a:title', ATOM_NS)
        updated_el = entry.find('a:updated', ATOM_NS)
        author_el  = entry.find('a:author/a:name', ATOM_NS)
        content_el = entry.find('a:content', ATOM_NS)

        created_utc = _parse_iso_utc(updated_el.text) if updated_el is not None else 0
        if created_utc < after_utc:
            continue
        # id is like "t3_1tinsky"; strip the kind prefix so it matches the JSON path
        raw_id = id_el.text if id_el is not None else ''
        post_id = raw_id.split('_', 1)[1] if raw_id.startswith('t3_') else raw_id
        title    = (title_el.text or '') if title_el is not None else ''
        author   = (author_el.text or '') if author_el is not None else ''
        # Drop the leading /u/ to match the JSON shape's bare username
        if author.startswith('/u/'):
            author = author[3:]
        body_html = (content_el.text or '') if content_el is not None else ''
        body_text = _strip_html(body_html)
        out.append({
            'id':              post_id,
            'subreddit':       subreddit,
            'title':           title,
            'body':            body_text,
            'author':          author,
            'created_utc':     created_utc,
            'score':           0,  # not present in RSS
            'ticker_mentions': extract_tickers(f'{title} {body_text}'),
        })
    return out


def fetch_subreddit_posts(subreddit: str, after_utc: int,
                          limit: int = 50) -> List[Dict]:
    """Fetch new posts from /r/<subreddit>/new.rss. Returns posts with
    `ticker_mentions` populated. On error, returns empty list (caller decides
    how to handle missing data).

    Note: Reddit's RSS endpoint caps at ~50 entries per request regardless
    of the requested limit. Calling with higher values is harmless.
    """
    url = f'https://www.reddit.com/r/{subreddit}/new.rss?limit={limit}'
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            if r.status != 200:
                logger.warning('reddit %s: status %s', subreddit, r.status)
                return []
            raw = r.read()
    except urllib.error.URLError as e:
        logger.warning('reddit %s: %s', subreddit, e)
        return []
    except Exception as e:
        logger.warning('reddit %s: unexpected %s', subreddit, e)
        return []
    finally:
        time.sleep(THROTTLE_SEC)

    return _parse_atom(raw, subreddit, after_utc)


def fetch_multiple_subreddits(subreddits: Iterable[str], after_utc: int) -> List[Dict]:
    """Fetch posts from multiple subreddits, throttled between requests."""
    posts: List[Dict] = []
    for sub in subreddits:
        posts.extend(fetch_subreddit_posts(sub, after_utc))
    return posts
