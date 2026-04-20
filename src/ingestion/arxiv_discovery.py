"""
arxiv_discovery.py — Broad arXiv q-fin harvest into research_corpus.

Phase 1 of the Opus Corpus Curator rollout. Fetches widely across all q-fin
categories and stores everything in research_corpus for the curator to judge.
Idempotent on source_url.

During Phase 1 the legacy research_candidates queue is ALSO populated (via the
old keyword heuristic) so the existing downstream pipeline keeps running while
the curator is being built. Phase 2 flips the default to --no-legacy.

Usage:
    python3 src/ingestion/arxiv_discovery.py [--days N] [--max-per-cat N] [--no-legacy]
"""

import os
import sys
import json
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen
from urllib.parse import urlencode
from urllib.error import URLError

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

ARXIV_API  = 'http://export.arxiv.org/api/query'
# Expanded set vs. pre-curator version (was ST/PM/TR only).
CATEGORIES          = ['q-fin.ST', 'q-fin.PM', 'q-fin.TR', 'q-fin.CP', 'q-fin.GN', 'q-fin.RM']
MAX_RESULTS_DEFAULT = 200  # per category

# ── Legacy keyword heuristic (kept only to keep research_candidates populated
# during Phase 1; removed in Phase 2 once the Opus curator is live). ──────────
SCORED_KEYWORDS = {
    'regime': 3, 'hmm': 3, 'hidden markov': 3, 'kelly': 3,
    'factor': 2, 'volatility': 2, 'momentum': 2, 'mean reversion': 2,
    'anomaly': 2, 'alpha': 2,
    'backtest': 1, 'sharpe': 1, 'drawdown': 1, 'options': 1,
    'implied vol': 1, 'liquidity': 1, 'cross-sectional': 1,
}
SKIP_KEYWORDS   = ['stochastic differential', 'lévy process', 'martingale', 'utility maximization']
LEGACY_MIN_SCORE = 4
LEGACY_TOP_N     = 10


def _arxiv_search(category: str, days: int, max_results: int) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y%m%d%H%M%S')
    query = f'cat:{category} AND submittedDate:[{since} TO 99991231235959]'
    params = urlencode({
        'search_query': query,
        'start':        0,
        'max_results':  max_results,
        'sortBy':       'submittedDate',
        'sortOrder':    'descending',
    })
    url = f'{ARXIV_API}?{params}'

    try:
        with urlopen(url, timeout=30) as resp:
            xml_data = resp.read()
    except URLError as e:
        print(f'[arxiv] fetch error for {category}: {e}', file=sys.stderr)
        return []

    ns = {'atom': 'http://www.w3.org/2005/Atom'}
    root = ET.fromstring(xml_data)
    papers = []
    for entry in root.findall('atom:entry', ns):
        paper_id  = (entry.findtext('atom:id', '', ns) or '').strip()
        title     = (entry.findtext('atom:title', '', ns) or '').strip().replace('\n', ' ')
        abstract  = (entry.findtext('atom:summary', '', ns) or '').strip().replace('\n', ' ')
        published = (entry.findtext('atom:published', '', ns) or '').strip()
        authors   = [
            (a.findtext('atom:name', '', ns) or '').strip()
            for a in entry.findall('atom:author', ns)
        ]
        pub_date = None
        if published:
            try:
                pub_date = datetime.fromisoformat(published.replace('Z', '+00:00')).date().isoformat()
            except ValueError:
                pub_date = None
        papers.append({
            'source_url':     paper_id,
            'title':          title,
            'abstract':       abstract,
            'authors':        [a for a in authors if a],
            'venue':          f'arxiv:{category}',
            'published_date': pub_date,
            'category':       category,
        })
    return papers


def discover(days: int = 30, max_per_cat: int = MAX_RESULTS_DEFAULT) -> list[dict]:
    """Return deduped papers from every configured q-fin category — no filtering."""
    seen: set[str] = set()
    out: list[dict] = []
    for cat in CATEGORIES:
        for p in _arxiv_search(cat, days, max_per_cat):
            if p['source_url'] in seen:
                continue
            seen.add(p['source_url'])
            out.append(p)
    return out


def insert_into_corpus(papers: list[dict], conn) -> int:
    if not papers:
        return 0
    cur = conn.cursor()
    inserted = 0
    for p in papers:
        raw = json.dumps({'category': p.get('category')})
        cur.execute(
            """INSERT INTO research_corpus
                 (source, source_url, title, abstract, authors, venue, published_date, raw_metadata)
               VALUES ('arxiv', %s, %s, %s, %s, %s, %s, %s::jsonb)
               ON CONFLICT (source_url) DO NOTHING""",
            (
                p['source_url'], p['title'], p['abstract'],
                p['authors'] or None, p['venue'], p['published_date'], raw,
            )
        )
        if cur.rowcount:
            inserted += 1
    conn.commit()
    return inserted


# ── Legacy helpers (deprecated; removed in Phase 2) ───────────────────────────

def _legacy_score(p: dict) -> int:
    text = (p.get('title', '') + ' ' + p.get('abstract', '')).lower()
    for kw in SKIP_KEYWORDS:
        if kw in text:
            return -1
    return sum(w for kw, w in SCORED_KEYWORDS.items() if kw in text)


def legacy_insert_candidates(papers: list[dict], conn) -> tuple[int, int]:
    """Old keyword-scored top-N populator for research_candidates. Returns (inserted, found)."""
    scored = []
    for p in papers:
        s = _legacy_score(p)
        if s >= LEGACY_MIN_SCORE:
            q = dict(p)
            q['score'] = s
            scored.append(q)
    scored.sort(key=lambda p: -p['score'])
    scored = scored[:LEGACY_TOP_N]

    inserted = 0
    cur = conn.cursor()
    for p in scored:
        spec = json.dumps({
            'strategy_id':             None,
            'hypothesis_one_liner':    p['title'],
            'signal_logic':            (p['abstract'] or '')[:500],
            'data_requirements':       ['prices'],
            'rejection_reason_if_any': None,
            'source':                  'arxiv',
            'arxiv_score':             p['score'],
        })
        cur.execute('SELECT 1 FROM research_candidates WHERE source_url = %s', (p['source_url'],))
        if cur.fetchone():
            continue
        cur.execute(
            """INSERT INTO research_candidates
                 (source_url, submitted_by, priority, status, hunter_result_json)
               VALUES (%s, 'arxiv-discovery', %s, 'pending', %s::jsonb)""",
            (p['source_url'], min(p['score'], 5), spec)
        )
        inserted += 1
    conn.commit()
    return inserted, len(scored)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=30, help='Days back to search')
    parser.add_argument('--max-per-cat', type=int, default=MAX_RESULTS_DEFAULT,
                        help='Max results per arXiv category')
    parser.add_argument('--no-legacy', action='store_true',
                        help='Skip legacy research_candidates population (set in Phase 2)')
    args = parser.parse_args()

    print(f'[arxiv] Broad-fetch last {args.days}d across {len(CATEGORIES)} categories '
          f'(up to {args.max_per_cat}/cat)...')
    papers = discover(args.days, args.max_per_cat)
    print(f'[arxiv] Fetched {len(papers)} unique papers.')

    pg_uri = os.environ.get('POSTGRES_URI')
    if not pg_uri:
        print('[arxiv] No POSTGRES_URI — dry-run only. First 10:', file=sys.stderr)
        for p in papers[:10]:
            print(f"  {p['title'][:90]} — {p['source_url']}")
        # Preserve old string format for orchestrator back-compat even in dry-run.
        print(f'[arxiv] Inserted 0 of 0 top papers into research_candidates.')
        sys.exit(0)

    import psycopg2
    conn = psycopg2.connect(pg_uri)
    try:
        corpus_n = insert_into_corpus(papers, conn)
        print(f'[arxiv] Inserted {corpus_n} new papers into research_corpus.')

        if args.no_legacy:
            # Phase 2: print the legacy-format line with zero so orchestrator still parses.
            print(f'[arxiv] Inserted 0 of 0 top papers into research_candidates.')
        else:
            cand_inserted, cand_found = legacy_insert_candidates(papers, conn)
            # MUST match old regex: "Inserted N of M" in research-orchestrator.js line 111.
            print(f'[arxiv] Inserted {cand_inserted} of {cand_found} top papers into research_candidates.')
    finally:
        conn.close()
