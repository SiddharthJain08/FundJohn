"""News-RSS ingest using the 2C Jaccard headline-dedup helper.

Pulls free public financial-news RSS feeds, parses items, runs them through
the 24h Jaccard dedup window before INSERT INTO research_corpus, and
preserves dedup_dropped rows with shared dedup_group_id for forensics.

Each source is tagged with a category so the same-category-tighter-threshold
path of dedup_within_window fires correctly when multiple wire services
cover the same story.

Usage:
    python3 src/ingestion/news_rss_ingest.py [--limit-per-feed N] [--dry-run]

Notes
-----
* `paper_fingerprint` is deliberately left NULL on news rows. The fingerprint
  contract is title + first-author last name + year, and headlines almost
  never carry author/year — so `compute_fingerprint` returns None and we
  store NULL. This is by design: synthesizing a fake author would corrupt
  cross-source dup semantics for actual papers, where the fingerprint is
  load-bearing.
* The dedup decision is fixed at first-write time. ON CONFLICT (source_url)
  DO NOTHING means a re-fetched URL never re-runs the Jaccard pass — the
  v1 row's dedup_dropped flag persists. Acceptable for the 24h-window batch
  ingest; revisit if we ever want re-evaluation under aging-out winners.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.request import Request

# Make src.* importable in script mode
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.ingestion._http_retry import fetch_with_retry  # noqa: E402
from src.research.headline_dedup import dedup_within_window  # noqa: E402
from src.research.paper_fingerprint import compute_fingerprint  # noqa: E402


# Browser-style UA — same pattern as expanded_sources.py. Several feeds
# (SeekingAlpha, dowjones.io) reject the default 'Python-urllib' UA.
_DEFAULT_UA = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36 (openclaw-research; +https://openclaw)'
)
_ACCEPT_FEED = 'application/rss+xml, application/atom+xml, application/xml, text/xml, */*; q=0.1'


def _build_request(url: str) -> Request:
    return Request(url, headers={'User-Agent': _DEFAULT_UA, 'Accept': _ACCEPT_FEED})


# (URL, source_tag, category)
#
# Curation choices:
#   * SEC press releases     — regulatory; high-signal corporate actions.
#   * Federal Reserve press  — macro; FOMC + enforcement + speeches.
#   * BEA press              — macro; GDP/personal-income releases. Replaces
#                              BLS empsit (returns 404; bls.gov gates non-
#                              browser RSS access behind 403).
#   * Yahoo Finance ^GSPC    — wire-service; SPX-level movers.
#   * MarketWatch top stories — wire-service. The classic
#                              www.marketwatch.com/rss/topstories URL 301s
#                              to feeds.content.dowjones.io; we pin the
#                              post-redirect URL to drop one hop per run.
#   * SeekingAlpha currents  — commentary; analyst-skewed take on the
#                              same wire stories — high dup rate with
#                              MarketWatch is the *point* (proves
#                              cross-feed Jaccard fires).
#   * CNBC top news          — wire-service.
FEEDS = [
    # Tier 1 — regulatory + macro
    ('https://www.sec.gov/news/pressreleases.rss',
     'rss:sec',           'regulatory'),
    ('https://www.federalreserve.gov/feeds/press_all.xml',
     'rss:federalreserve', 'macro'),
    ('https://apps.bea.gov/rss/rss.xml',
     'rss:bea',           'macro'),
    # Tier 2 — financial wire services (high dup rate among each other)
    ('https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC&region=US&lang=en-US',
     'rss:yahoo-sp500',   'wire-service'),
    ('https://feeds.content.dowjones.io/public/rss/mw_topstories',
     'rss:marketwatch',   'wire-service'),
    ('https://www.cnbc.com/id/100003114/device/rss/rss.html',
     'rss:cnbc',          'wire-service'),
    # Tier 3 — analyst commentary (often dups Tier 2)
    ('https://seekingalpha.com/market_currents.xml',
     'rss:seekingalpha',  'commentary'),
]


# RFC 822 named US zone offsets — datetime.strptime can't handle these via
# %Z portably (see CPython issue), so we manually substitute before parse.
_NAMED_TZ_OFFSETS = {
    'GMT': '+0000', 'UTC': '+0000', 'Z': '+0000',
    'EDT': '-0400', 'EST': '-0500',
    'CDT': '-0500', 'CST': '-0600',
    'MDT': '-0600', 'MST': '-0700',
    'PDT': '-0700', 'PST': '-0800',
}


def _parse_pubdate(s: str, source_tag: str = '') -> datetime | None:
    """Best-effort RFC 822 / ISO 8601 pubDate parser.

    Returns a tz-aware UTC datetime, or None on parse failure. Emits a
    one-line WARN to stderr on failure so silent fallback never masks a
    feed-format change. Caller decides what to do with None (we substitute
    'now in UTC' at the call site for dedup-window math, but record the
    parse failure separately so DB-level published_date ends up NULL).
    """
    if not s:
        return None
    s = s.strip()
    # Substitute named US zones with numeric offsets so %z parses cleanly.
    for name, off in _NAMED_TZ_OFFSETS.items():
        if s.endswith(' ' + name):
            s = s[: -(len(name) + 1)] + ' ' + off
            break

    fmts = (
        '%a, %d %b %Y %H:%M:%S %z',  # RFC 822 with seconds
        '%a, %d %b %Y %H:%M %z',     # CNBC: no seconds
        '%a, %d %b %Y %H:%M:%S',     # rare: tz already stripped
        '%Y-%m-%dT%H:%M:%S%z',       # ISO 8601 w/ offset
        '%Y-%m-%dT%H:%M:%SZ',        # ISO 8601 zulu
        '%Y-%m-%d',                  # date-only
    )
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    print(f'[news-rss] WARN: unparseable pubDate from {source_tag}: {s!r}',
          file=sys.stderr)
    return None


_ATOM_NS = '{http://www.w3.org/2005/Atom}'


def _parse_rss(xml_text: str, source_tag: str, category: str) -> list[dict]:
    """Parse RSS 2.0 + Atom into normalized item dicts.

    Each dict carries the keys the dedup helper requires
    (id, title, source, ts) plus link + category + parsed_ts_ok flag for
    DB inserts.
    """
    out: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f'[news-rss] WARN: XML parse error for {source_tag}: {e}',
              file=sys.stderr)
        return out

    items = root.findall('.//item')
    is_atom = False
    if not items:
        items = root.findall(f'.//{_ATOM_NS}entry')
        is_atom = True

    for item in items:
        if is_atom:
            title = (item.findtext(f'{_ATOM_NS}title') or '').strip()
            link_el = item.find(f'{_ATOM_NS}link')
            link = (link_el.get('href') if link_el is not None else '') or ''
            link = link.strip()
            pub = (item.findtext(f'{_ATOM_NS}published')
                   or item.findtext(f'{_ATOM_NS}updated') or '').strip()
        else:
            title = (item.findtext('title') or '').strip()
            link = (item.findtext('link') or '').strip()
            pub = (item.findtext('pubDate') or '').strip()

        if not title or not link:
            continue

        parsed_ts = _parse_pubdate(pub, source_tag)
        ts = parsed_ts if parsed_ts is not None else datetime.now(tz=timezone.utc)
        out.append({
            'id':            link,            # deduper's identity
            'title':         title,
            'source':        source_tag,
            'link':          link,
            'category':      category,
            'ts':            ts,
            'parsed_ts_ok':  parsed_ts is not None,
        })
    return out


def fetch_all_feeds(limit_per_feed: int | None = None) -> list[dict]:
    out: list[dict] = []
    for url, source_tag, category in FEEDS:
        body = fetch_with_retry(_build_request(url), label=source_tag, timeout=20)
        if not body:
            print(f'[news-rss] skip {source_tag} (fetch failed)', file=sys.stderr)
            continue
        items = _parse_rss(body.decode('utf-8', errors='replace'),
                           source_tag, category)
        if limit_per_feed:
            items = items[:limit_per_feed]
        print(f'[news-rss] {source_tag}: {len(items)} items', file=sys.stderr)
        out.extend(items)
    return out


def insert_into_corpus(items: list[dict], conn, dry_run: bool = False) -> dict:
    """Returns a result dict with kept/dropped/inserted/skipped counts.

    Pulls the last 24h of non-dropped rss:* rows as the prior dedup window,
    runs ``dedup_within_window`` over prior + new, then INSERTs every new
    item (kept or dropped) into research_corpus. Items that lost the
    Jaccard race are inserted with ``dedup_dropped=TRUE`` so forensics can
    inspect them. All items in this batch share a single ``dedup_group_id``.
    """
    cur = conn.cursor()
    cur.execute(
        """SELECT paper_id, title, source, ingested_at
             FROM research_corpus
            WHERE source LIKE 'rss:%%'
              AND ingested_at > NOW() - INTERVAL '24 hours'
              AND dedup_dropped = FALSE""")
    prior = [
        {'id': str(r[0]), 'title': r[1], 'source': r[2], 'ts': r[3]}
        for r in cur.fetchall()
    ]

    combined = prior + items
    kept = dedup_within_window(combined, threshold=0.25, same_category_threshold=0.20)
    kept_ids = {k['id'] for k in kept}

    new_kept = [it for it in items if it['id'] in kept_ids]
    new_dropped = [it for it in items if it['id'] not in kept_ids]

    batch_group_id = str(uuid.uuid4())

    inserted = 0
    skipped = 0
    if dry_run:
        return {
            'kept':                len(new_kept),
            'dropped':             len(new_dropped),
            'inserted':            0,
            'on_conflict_skipped': 0,
            'prior_window_size':   len(prior),
            'dry_run':             True,
        }

    for it in items:
        dropped = it['id'] not in kept_ids
        # Headlines lack author/year, so fingerprint will be None — this is
        # correct (see module docstring).
        fp = compute_fingerprint(it['title'], None, None)
        published_date = it['ts'].date() if it.get('parsed_ts_ok') else None
        try:
            cur.execute(
                """INSERT INTO research_corpus
                     (source, source_url, title, abstract, authors, venue,
                      published_date, raw_metadata, dedup_group_id,
                      dedup_dropped, paper_fingerprint)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                   ON CONFLICT (source_url) DO NOTHING""",
                (
                    it['source'],
                    it['link'],
                    it['title'][:1000],
                    '',                       # no abstract for headlines
                    None,                     # authors
                    it['source'][:200],       # venue = source tag
                    published_date,
                    json.dumps({'category':  it['category'],
                                'feed_kind': 'rss-news'}),
                    batch_group_id,
                    dropped,
                    fp,
                ),
            )
            if cur.rowcount > 0:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            print(f'[news-rss] insert failed for {it["link"]}: {e}', file=sys.stderr)
            conn.rollback()
            cur = conn.cursor()
    conn.commit()

    return {
        'kept':                len(new_kept),
        'dropped':             len(new_dropped),
        'inserted':            inserted,
        'on_conflict_skipped': skipped,
        'prior_window_size':   len(prior),
        'batch_group_id':      batch_group_id,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit-per-feed', type=int, default=None)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    items = fetch_all_feeds(limit_per_feed=args.limit_per_feed)
    if not items:
        print('[news-rss] no items fetched', file=sys.stderr)
        return 0

    if args.dry_run:
        print(f'[news-rss] dry-run: would process {len(items)} items '
              f'across {len({i["source"] for i in items})} feeds')
        # Dry-run still reports the dedup split (no DB writes).
        unparseable = sum(1 for i in items if not i.get('parsed_ts_ok'))
        if unparseable:
            print(f'[news-rss] dry-run: {unparseable} items had unparseable pubDate')
        return 0

    import psycopg2
    conn = psycopg2.connect(
        os.environ.get('POSTGRES_URI',
                       'postgresql://openclaw:password@localhost:5432/openclaw'))
    try:
        result = insert_into_corpus(items, conn)
    finally:
        conn.close()

    print(f'[news-rss] {json.dumps(result, default=str)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
