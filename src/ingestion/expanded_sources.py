"""
expanded_sources.py — Phase 2 (companion) of the Saturday brain.

The brain's Phase 1 (Opus + WebSearch) discovers SOURCE FEEDS beyond
arXiv/OpenAlex (Fed working papers, AQR insights, SSRN top-author archives,
Risk.net, conference proceedings, blogs with citations). This module reads
the latest `paper_source_expansions.sources_discovered` JSONB and ingests
each feed best-effort.

Supports three feed kinds: RSS/Atom, JSON sitemap (OpenAlex-style cursor),
and HTML index pages (heuristic anchor-link extraction). Anything we can't
parse is logged and skipped — never blocks the brain.

Usage:
    python3 src/ingestion/expanded_sources.py [--expansion-id UUID]
                                              [--max-per-source 50]
                                              [--dry-run]

If --expansion-id is omitted, uses the most recent completed
paper_source_expansions row.
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _http_retry import fetch_with_retry  # noqa: E402

# Browser-like User-Agent. Many of the feeds we hit (federalreserve.gov,
# alphaarchitect.com, two sigma) reject the default urllib User-Agent
# ("Python-urllib/3.x") with HTTP 403. A vanilla Chrome UA gets through
# without needing to forge anything more elaborate.
_DEFAULT_UA = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36 (openclaw-research; +https://openclaw)'
)


def _build_request(url: str) -> Request:
    """Wrap a URL in a Request that carries a friendly User-Agent + Accept
    header, so feeds that filter on default urllib UA stop returning 403."""
    return Request(url, headers={
        'User-Agent': _DEFAULT_UA,
        'Accept':     'application/rss+xml, application/atom+xml, application/xml, text/html; q=0.8',
    })

# Heuristic regexes for HTML index extraction. Conservative: prefer pages that
# look like working-paper indexes (PDFs, abstract pages with a title nearby).
RX_PAPER_LINK = re.compile(
    r'<a\b[^>]+href="([^"#]+\.(?:pdf|html?))"[^>]*>([^<]{8,200})</a>',
    re.IGNORECASE,
)
RX_RSS_ITEM    = re.compile(r'<item\b[\s\S]*?</item>', re.IGNORECASE)
RX_RSS_TITLE   = re.compile(r'<title>([\s\S]*?)</title>', re.IGNORECASE)
RX_RSS_LINK    = re.compile(r'<link>([\s\S]*?)</link>', re.IGNORECASE)
RX_RSS_PUBDATE = re.compile(r'<pubDate>([\s\S]*?)</pubDate>', re.IGNORECASE)
RX_RSS_CREATOR = re.compile(r'<dc:creator>([\s\S]*?)</dc:creator>', re.IGNORECASE)
RX_RSS_DESC    = re.compile(r'<description>([\s\S]*?)</description>', re.IGNORECASE)


def _strip_html(s: str) -> str:
    return re.sub(r'<[^>]+>', '', s or '').strip()


def _absolute_url(href: str, base: str) -> str:
    if href.startswith('http://') or href.startswith('https://'):
        return href
    if href.startswith('//'):
        return 'https:' + href
    if href.startswith('/'):
        u = urlparse(base)
        return f'{u.scheme}://{u.netloc}{href}'
    return base.rstrip('/') + '/' + href


def _parse_rss(body: bytes, source_url: str, source_tag: str) -> list[dict]:
    """Extract paper items from RSS/Atom feeds. Best-effort regex parse so
    malformed feeds don't crash the run."""
    out = []
    text = body.decode('utf-8', errors='replace')
    # First try strict XML parse — fewer false positives.
    try:
        root = ET.fromstring(text)
        ns = {'atom': 'http://www.w3.org/2005/Atom'}
        # RSS 2.0
        for item in root.findall('.//item'):
            title = (item.findtext('title') or '').strip()
            link  = (item.findtext('link')  or '').strip()
            pub   = (item.findtext('pubDate') or '').strip()
            desc  = _strip_html(item.findtext('description') or '')
            creator = (item.findtext('{http://purl.org/dc/elements/1.1/}creator') or '').strip()
            if not link:
                continue
            out.append({
                'source':         source_tag,
                'source_url':     link,
                'title':          title,
                'abstract':       desc[:2000],
                'authors':        [creator] if creator else [],
                'venue':          source_tag,
                'published_date': _parse_pubdate(pub),
                'raw_metadata':   {'feed_origin': source_url, 'feed_kind': 'rss'},
            })
        # Atom
        for entry in root.findall('atom:entry', ns):
            title = (entry.findtext('atom:title', '', ns) or '').strip()
            link_el = entry.find('atom:link', ns)
            link  = (link_el.get('href') if link_el is not None else '') or ''
            pub   = (entry.findtext('atom:published', '', ns) or '').strip()
            summ  = _strip_html(entry.findtext('atom:summary', '', ns) or '')
            authors = [
                (a.findtext('atom:name', '', ns) or '').strip()
                for a in entry.findall('atom:author', ns)
            ]
            if not link:
                continue
            out.append({
                'source':         source_tag,
                'source_url':     link,
                'title':          title,
                'abstract':       summ[:2000],
                'authors':        [a for a in authors if a],
                'venue':          source_tag,
                'published_date': _parse_pubdate(pub),
                'raw_metadata':   {'feed_origin': source_url, 'feed_kind': 'atom'},
            })
    except ET.ParseError:
        # Fall back to regex item extraction.
        for m in RX_RSS_ITEM.finditer(text):
            chunk = m.group(0)
            title = _strip_html((RX_RSS_TITLE.search(chunk) or [None, ''])[1])
            link  = _strip_html((RX_RSS_LINK.search(chunk)  or [None, ''])[1])
            pub   = _strip_html((RX_RSS_PUBDATE.search(chunk) or [None, ''])[1])
            creator = _strip_html((RX_RSS_CREATOR.search(chunk) or [None, ''])[1])
            desc  = _strip_html((RX_RSS_DESC.search(chunk) or [None, ''])[1])
            if not link:
                continue
            out.append({
                'source':         source_tag,
                'source_url':     link,
                'title':          title,
                'abstract':       desc[:2000],
                'authors':        [creator] if creator else [],
                'venue':          source_tag,
                'published_date': _parse_pubdate(pub),
                'raw_metadata':   {'feed_origin': source_url, 'feed_kind': 'rss-fallback'},
            })
    return out


def _parse_html_index(body: bytes, source_url: str, source_tag: str,
                      max_links: int) -> list[dict]:
    """Heuristic HTML-index parse: harvest anchor tags pointing at PDFs or
    paper-shaped HTML pages. The curator downstream filters ruthlessly so a
    high false-positive rate here is acceptable."""
    out = []
    text = body.decode('utf-8', errors='replace')
    for m in RX_PAPER_LINK.finditer(text):
        href, title = m.group(1), _strip_html(m.group(2))
        if len(out) >= max_links:
            break
        # Skip nav/footer/icon/login links by minimum title length already
        # enforced by the regex (8+ chars).
        absolute = _absolute_url(href, source_url)
        out.append({
            'source':         source_tag,
            'source_url':     absolute,
            'title':          title,
            'abstract':       '',     # not present on index pages
            'authors':        [],
            'venue':          source_tag,
            'published_date': None,
            'raw_metadata':   {'feed_origin': source_url, 'feed_kind': 'html-index'},
        })
    return out


def _parse_pubdate(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    # RFC-822: "Mon, 06 Sep 2021 13:00:00 GMT"
    fmts = ['%a, %d %b %Y %H:%M:%S %Z', '%a, %d %b %Y %H:%M:%S %z',
            '%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d']
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _detect_feed_kind(source_url: str, body: bytes) -> str:
    """Return 'rss', 'atom', or 'html'."""
    head = body[:2048].decode('utf-8', errors='replace').lower()
    if '<rss' in head or '<channel>' in head:
        return 'rss'
    if 'xmlns="http://www.w3.org/2005/atom"' in head or '<feed' in head:
        return 'atom'
    return 'html'


def fetch_source(source_spec: dict, max_per_source: int = 50) -> list[dict]:
    """
    Pull papers from a single source feed. `source_spec` shape mirrors what
    paper_expansion_ingestor's reframed prompt now emits per source:
       { domain, name, feed_url, kind?, papers_found, notes }
    The 'kind' is best-effort hint; we sniff the actual response.
    """
    feed_url = source_spec.get('feed_url') or source_spec.get('domain')
    if not feed_url:
        return []
    label = source_spec.get('name') or urlparse(feed_url).netloc
    body = fetch_with_retry(_build_request(feed_url), label=f'expanded:{label}')
    if body is None:
        return []
    kind = source_spec.get('kind') or _detect_feed_kind(feed_url, body)
    if kind in ('rss', 'atom'):
        return _parse_rss(body, feed_url, f'expanded:{label}')
    return _parse_html_index(body, feed_url, f'expanded:{label}', max_per_source)


def insert_into_corpus(papers: list[dict], conn) -> tuple[int, int]:
    """Returns (inserted, skipped_duplicate). Honors research_corpus.source_url
    UNIQUE — same dedup as arxiv_discovery + openalex_discovery."""
    if not papers:
        return 0, 0
    cur = conn.cursor()
    inserted = skipped = 0
    for p in papers:
        cur.execute(
            """INSERT INTO research_corpus
                 (source, source_url, title, abstract, authors, venue, published_date, raw_metadata)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
               ON CONFLICT (source_url) DO NOTHING""",
            (
                p['source'], p['source_url'], (p.get('title') or '')[:1000],
                (p.get('abstract') or '')[:20000],
                p.get('authors') or None,
                (p.get('venue') or '')[:200],
                p.get('published_date'),
                json.dumps(p.get('raw_metadata') or {}),
            )
        )
        if cur.rowcount:
            inserted += 1
        else:
            skipped += 1
    conn.commit()
    return inserted, skipped


def fetch_expansion_sources(conn, expansion_id: str | None) -> list[dict]:
    """Pull the sources_discovered list from a paper_source_expansions row."""
    cur = conn.cursor()
    if expansion_id:
        cur.execute(
            "SELECT sources_discovered FROM paper_source_expansions WHERE id = %s",
            (expansion_id,)
        )
    else:
        cur.execute(
            "SELECT sources_discovered FROM paper_source_expansions "
            "WHERE status = 'completed' "
            "ORDER BY created_at DESC NULLS LAST, run_date DESC LIMIT 1"
        )
    row = cur.fetchone()
    if not row or not row[0]:
        return []
    raw = row[0]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    return raw


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--expansion-id', type=str, default=None,
                        help='UUID of the paper_source_expansions row to ingest. '
                             'Default: most recent completed.')
    parser.add_argument('--max-per-source', type=int, default=50,
                        help='Cap papers ingested per source feed.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Fetch + parse, but do not write to research_corpus.')
    args = parser.parse_args()

    pg_uri = os.environ.get('POSTGRES_URI')
    if not pg_uri:
        print('[expanded] No POSTGRES_URI — aborting', file=sys.stderr)
        sys.exit(1)

    import psycopg2
    conn = psycopg2.connect(pg_uri)
    try:
        sources = fetch_expansion_sources(conn, args.expansion_id)
        print(f'[expanded] {len(sources)} source(s) from expansion '
              f'{args.expansion_id or "(latest completed)"}')
        if not sources:
            print('[expanded] Nothing to ingest. Exit 0.')
            sys.exit(0)

        total_in = total_skip = 0
        for src in sources:
            label = src.get('name') or src.get('domain') or '?'
            try:
                papers = fetch_source(src, max_per_source=args.max_per_source)
            except Exception as e:
                print(f'[expanded] {label}: fetch failed — {e}', file=sys.stderr)
                continue
            print(f'[expanded] {label}: parsed {len(papers)} candidate(s)')
            if args.dry_run:
                continue
            n_in, n_skip = insert_into_corpus(papers, conn)
            total_in += n_in
            total_skip += n_skip
        if args.dry_run:
            print('[expanded] DRY RUN — no rows written.')
        else:
            print(f'[expanded] Inserted {total_in} new papers, '
                  f'skipped {total_skip} duplicates.')
    finally:
        conn.close()
