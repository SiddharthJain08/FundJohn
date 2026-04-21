"""
openalex_discovery.py — Unified ingestion via the OpenAlex academic-metadata API.

Phase 2b of the Opus Corpus Curator rollout. The old nber_discovery.py and
ssrn_discovery.py modules are deprecated: both NBER and SSRN no longer publish
machine-readable feeds. OpenAlex (https://openalex.org) indexes both venues
plus most journals with stable JSON over HTTPS and a polite rate-limit policy.

This module queries OpenAlex for recently-published finance works across
multiple source venues and finance concepts, deduplicated by OpenAlex ID, and
writes them into `research_corpus` with `source` set to the OpenAlex host venue
(e.g. 'openalex:ssrn', 'openalex:nber').

Citation: https://api.openalex.org — free, 100k req/day with an email address.

Usage:
    python3 src/ingestion/openalex_discovery.py \
        [--days 90] [--limit-per-venue 200] [--min-citations 0]
"""

import os
import sys
import json
import argparse
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _http_retry import fetch_with_retry  # local import to avoid src.ingestion package __init__

OPENALEX_BASE = 'https://api.openalex.org/works'

# Venue source IDs (OpenAlex Sources).
# `concept_filter=False` bypasses the finance-concept filter for venues where
# OpenAlex's concept tagging is too sparse (e.g. SSRN working papers often miss
# concept tags despite being squarely in finance). The venue ID itself is a
# strong-enough finance prior.
#
# Top-3 finance journals added in Phase 4a: lower volume than SSRN, much higher
# signal — every paper is already peer-reviewed.
VENUES = {
    'ssrn': {'id': 'S4210172589', 'concept_filter': False},  # SSRN Electronic Journal — 1.58M works
    'nber': {'id': 'S2809516038', 'concept_filter': True},   # NBER — 36k works
    'jf':   {'id': 'S5353659',    'concept_filter': False},  # Journal of Finance — 16k works
    'rfs':  {'id': 'S170137484',  'concept_filter': False},  # Review of Financial Studies — 3.2k works
    'jfe':  {'id': 'S149240962',  'concept_filter': False},  # Journal of Financial Economics — 4.8k works
    'jfqa': {'id': 'S193228710',  'concept_filter': False},  # Journal of Financial and Quantitative Analysis — 4k works
    'qf':   {'id': 'S182689569',  'concept_filter': False},  # Quantitative Finance — 3.2k works
}

# OpenAlex concept IDs relevant to trading-strategy research.
# Using the broader Finance concept catches most SSRN/NBER papers (OpenAlex's
# concept tagging is often sparse for working papers). The curator downstream
# filters ruthlessly so false positives here are cheap.
FINANCE_CONCEPTS = [
    'C10138342',   # Finance (broad, level-1 parent)
    'C64943373',   # Alpha (finance)
    'C91602232',   # Volatility (finance)
    'C93373587',   # Mathematical finance
]

# Phase 4b: alpha-author watchlist. Any paper by one of these authors in the
# configured window is ingested regardless of venue or concept filter — known
# alpha producers often publish in non-standard venues or on topics OpenAlex
# tags poorly. This is a recall lever.
AUTHOR_WATCHLIST = {
    'fama':       'A5091820687',   # Eugene F. Fama — Chicago
    'french':     'A5041631912',   # Kenneth R. French — Dartmouth
    'jegadeesh':  'A5074911203',   # Narasimhan Jegadeesh — Emory
    'asness':     'A5038478408',   # Clifford S. Asness — AQR
    'cremers':    'A5087021719',   # Martijn Cremers — Notre Dame
    'pedersen':   'A5022954104',   # Lasse Heje Pedersen — CBS/AQR
    'moskowitz':  'A5053979330',   # Tobias J. Moskowitz — Chicago/AQR
    'koijen':     'A5083877502',   # Ralph S. J. Koijen — Chicago
    'lettau':     'A5033095024',   # Martin Lettau — Berkeley
    'hirshleifer':'A5067662277',   # David Hirshleifer — USC
    'hou':        'A5066035235',   # Kewei Hou — OSU
}


def _email_for_ua() -> str:
    return (os.environ.get('OPENALEX_EMAIL')
            or os.environ.get('SEC_USER_AGENT', '').split('(')[-1].rstrip(')')
            or 'openclaw-research@localhost')


def _request(url: str) -> dict:
    req = Request(url, headers={
        'User-Agent': f'openclaw-research/1.0 ({_email_for_ua()})',
        'Accept':     'application/json',
    })
    body = fetch_with_retry(req, label='openalex')
    if body is None:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        print(f'[openalex] JSON decode error: {e}', file=sys.stderr)
        return {}


def _reconstruct_abstract(inverted_idx: dict | None) -> str:
    """OpenAlex stores abstracts as word → [positions]. Reassemble to text."""
    if not inverted_idx:
        return ''
    by_position: dict[int, str] = {}
    for word, positions in inverted_idx.items():
        for p in positions:
            by_position[p] = word
    if not by_position:
        return ''
    max_pos = max(by_position.keys())
    return ' '.join(by_position.get(i, '') for i in range(max_pos + 1)).strip()


def _authors(work: dict) -> list[str]:
    return [
        (a.get('author') or {}).get('display_name', '').strip()
        for a in work.get('authorships', [])[:10]
    ] or []


def _best_url(work: dict) -> str | None:
    # Prefer canonical source URL, then DOI, then OpenAlex ID.
    loc = work.get('primary_location') or {}
    if (loc.get('landing_page_url') or '').startswith('http'):
        return loc['landing_page_url']
    if work.get('doi'):
        return work['doi'] if work['doi'].startswith('http') else f"https://doi.org/{work['doi']}"
    return work.get('id')


def _iter_openalex(filter_parts: list[str], max_results: int,
                   sort: str = 'cited_by_count:desc'):
    """Cursor-paginated OpenAlex works iterator. Yields work dicts."""
    cursor = '*'
    fetched = 0
    while fetched < max_results and cursor:
        params = {
            'filter':   ','.join(filter_parts),
            'sort':     sort,
            'per-page': min(200, max_results - fetched),
            'cursor':   cursor,
        }
        url = f'{OPENALEX_BASE}?{urlencode(params, safe=":,|>")}'
        body = _request(url)
        results = body.get('results') or []
        if not results:
            return
        for w in results:
            yield w
            fetched += 1
            if fetched >= max_results:
                return
        cursor = (body.get('meta') or {}).get('next_cursor')
        time.sleep(0.1)


def _as_paper(w: dict, source_tag: str, venue_name: str | None = None) -> dict | None:
    """Normalise an OpenAlex work dict into a research_corpus row."""
    src_url = _best_url(w)
    if not src_url:
        return None
    return {
        'source':         source_tag,
        'source_url':     src_url,
        'title':          (w.get('title') or '').strip(),
        'abstract':       _reconstruct_abstract(w.get('abstract_inverted_index')),
        'authors':        _authors(w),
        'venue':          venue_name,
        'published_date': w.get('publication_date'),
        'raw_metadata':   {
            'openalex_id':    w.get('id'),
            'doi':            w.get('doi'),
            'cited_by_count': w.get('cited_by_count'),
            'venue_name':     venue_name,
        },
    }


def discover(days: int = 90, limit_per_venue: int = 200,
             min_citations: int = 0,
             author_works_per: int = 10) -> list[dict]:
    """Return deduplicated papers from every configured OpenAlex venue PLUS the
    author watchlist. Authors override venue — a Fama paper on a non-watchlist
    journal still gets picked up.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    concepts = '|'.join(FINANCE_CONCEPTS)
    cite_filter = f'cited_by_count:>{max(0, min_citations - 1)}'

    seen: set[str] = set()
    out: list[dict] = []

    # ── Pass 1: venue-filtered fetches ─────────────────────────────────────
    for venue_name, cfg in VENUES.items():
        source_id = cfg['id']
        use_concept_filter = cfg.get('concept_filter', True)

        filter_parts = [
            f'primary_location.source.id:{source_id}',
            f'from_publication_date:{since}',
            cite_filter,
        ]
        if use_concept_filter:
            filter_parts.append(f'concepts.id:{concepts}')

        for w in _iter_openalex(filter_parts, limit_per_venue):
            oid = w.get('id')
            if not oid or oid in seen:
                continue
            seen.add(oid)
            paper = _as_paper(w, f'openalex:{venue_name}', venue_name)
            if paper:
                out.append(paper)

    # ── Pass 2: author watchlist — fetch any paper in the window ───────────
    for short_name, author_id in AUTHOR_WATCHLIST.items():
        filter_parts = [
            f'authorships.author.id:{author_id}',
            f'from_publication_date:{since}',
            # No concept filter: a known alpha author's work on a novel topic
            # is still worth a look; the curator downstream makes the call.
        ]
        for w in _iter_openalex(filter_parts, author_works_per, sort='publication_date:desc'):
            oid = w.get('id')
            if not oid or oid in seen:
                continue
            seen.add(oid)
            paper = _as_paper(w, f'openalex:watchlist:{short_name}', None)
            if paper:
                # Preserve watchlist author identity for reporting.
                paper['raw_metadata']['watchlist_author'] = short_name
                out.append(paper)

    return out


def insert_into_corpus(papers: list[dict]) -> int:
    if not papers:
        return 0
    pg_uri = os.environ.get('POSTGRES_URI')
    if not pg_uri:
        print('[openalex] No POSTGRES_URI — dry-run only. First 5:', file=sys.stderr)
        for p in papers[:5]:
            print(f"  [{p['source']}] {p['title'][:90]} — {p['source_url']}")
        return 0

    import psycopg2
    conn = psycopg2.connect(pg_uri)
    inserted = 0
    try:
        cur = conn.cursor()
        for p in papers:
            raw = json.dumps(p.get('raw_metadata') or {})
            cur.execute(
                """INSERT INTO research_corpus
                     (source, source_url, title, abstract, authors, venue, published_date, raw_metadata)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                   ON CONFLICT (source_url) DO NOTHING""",
                (
                    p['source'], p['source_url'], p['title'], p.get('abstract') or '',
                    p.get('authors') or None, p.get('venue'), p.get('published_date'), raw,
                )
            )
            if cur.rowcount:
                inserted += 1
        conn.commit()
    finally:
        conn.close()
    return inserted


def expand_citation_graph(seed_openalex_ids: list[str],
                          max_per_seed: int = 20,
                          min_year: int = 2010) -> list[dict]:
    """Phase 4c: pull works referenced by seed papers (one-hop out).

    Given OpenAlex IDs for high-bucket curator picks, fetch their
    `referenced_works` list and return the resulting paper metadata. Dedup
    by OpenAlex ID across seeds. Filters to works published >= min_year.
    """
    seen: set[str] = set(seed_openalex_ids)
    out: list[dict] = []

    for seed_id in seed_openalex_ids:
        # Fetch the seed paper to get its referenced_works list.
        sid = seed_id.rsplit('/', 1)[-1] if seed_id.startswith('http') else seed_id
        seed_url = f'{OPENALEX_BASE}/{sid}'
        body = _request(seed_url)
        refs = body.get('referenced_works') or []
        if not refs:
            continue
        refs = refs[:max_per_seed]

        # Batch-fetch referenced works: `filter=ids.openalex:<id1>|<id2>|...`
        # OpenAlex accepts up to 50 IDs per filter; chunk defensively at 25.
        for i in range(0, len(refs), 25):
            chunk = refs[i:i + 25]
            ids_filter = '|'.join(r.rsplit('/', 1)[-1] for r in chunk)
            chunk_url = f'{OPENALEX_BASE}?filter=ids.openalex:{ids_filter}&per-page=50'
            chunk_body = _request(chunk_url)
            for w in chunk_body.get('results') or []:
                oid = w.get('id')
                if not oid or oid in seen:
                    continue
                pub_date = w.get('publication_date') or ''
                try:
                    year = int(pub_date[:4])
                    if year < min_year:
                        continue
                except ValueError:
                    continue
                seen.add(oid)
                paper = _as_paper(w, f'openalex:citation_of:{sid}', None)
                if paper:
                    paper['raw_metadata']['cited_by_seed'] = sid
                    out.append(paper)
            time.sleep(0.1)
        time.sleep(0.1)

    return out


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=90)
    parser.add_argument('--limit-per-venue', type=int, default=200,
                        help='Cap per venue. Total ceiling = len(VENUES) × limit.')
    parser.add_argument('--min-citations', type=int, default=0,
                        help='Minimum OpenAlex cited_by_count (venue pass only).')
    parser.add_argument('--author-works-per', type=int, default=10,
                        help='Per-author cap for the watchlist pass.')
    parser.add_argument('--citation-seeds', type=str, default=None,
                        help='Comma-separated OpenAlex IDs (or file path with one per line) to expand via referenced_works. '
                             'Activates Phase 4c citation-graph mode.')
    parser.add_argument('--citation-max-per-seed', type=int, default=20,
                        help='Cap on refs per seed paper.')
    args = parser.parse_args()

    if args.citation_seeds:
        seeds_arg = args.citation_seeds
        if os.path.exists(seeds_arg):
            with open(seeds_arg) as f:
                seeds = [line.strip() for line in f if line.strip()]
        else:
            seeds = [s.strip() for s in seeds_arg.split(',') if s.strip()]
        print(f'[openalex] Citation-graph expansion from {len(seeds)} seed(s)...')
        papers = expand_citation_graph(seeds, max_per_seed=args.citation_max_per_seed)
        print(f'[openalex] Got {len(papers)} unique cited works.')
    else:
        print(f'[openalex] Fetching last {args.days}d from {len(VENUES)} venues + '
              f'{len(AUTHOR_WATCHLIST)} watchlist authors...')
        papers = discover(args.days, args.limit_per_venue, args.min_citations, args.author_works_per)
        print(f'[openalex] Got {len(papers)} unique papers.')

    n = insert_into_corpus(papers)
    print(f'[openalex] Inserted {n} new rows into research_corpus.')
