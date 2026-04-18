"""
arxiv_discovery.py — Weekly arXiv q-fin paper harvest.

Queries arXiv API for papers tagged q-fin.ST or q-fin.PM published in the
last 7 days. Scores abstracts with a keyword heuristic (zero LLM tokens).
Inserts top 3 scoring papers into research_candidates.

Usage:
    python3 src/ingestion/arxiv_discovery.py [--days N]
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

ARXIV_API   = 'http://export.arxiv.org/api/query'
CATEGORIES  = ['q-fin.ST', 'q-fin.PM', 'q-fin.TR']
MAX_RESULTS = 100  # papers to fetch before scoring
TOP_N       = 10   # papers to insert into research_candidates

# Keyword heuristic weights
SCORED_KEYWORDS = {
    'regime':         3,
    'hmm':            3,
    'hidden markov':  3,
    'kelly':          3,
    'factor':         2,
    'volatility':     2,
    'momentum':       2,
    'mean reversion': 2,
    'anomaly':        2,
    'alpha':          2,
    'backtest':       1,
    'sharpe':         1,
    'drawdown':       1,
    'options':        1,
    'implied vol':    1,
    'liquidity':      1,
    'cross-sectional': 1,
}

# Skip papers that are pure theory with no implementation signal
SKIP_KEYWORDS = ['stochastic differential', 'lévy process', 'martingale', 'utility maximization']


def _arxiv_search(category: str, days: int) -> list[dict]:
    """Fetch recent papers from arXiv for a category. Returns list of {id, title, abstract, url}."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y%m%d%H%M%S')
    query = f'cat:{category} AND submittedDate:[{since} TO 99991231235959]'
    params = urlencode({
        'search_query': query,
        'start':        0,
        'max_results':  MAX_RESULTS,
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
        paper_id = (entry.findtext('atom:id', '', ns) or '').strip()
        title    = (entry.findtext('atom:title', '', ns) or '').strip().replace('\n', ' ')
        abstract = (entry.findtext('atom:summary', '', ns) or '').strip().replace('\n', ' ')
        papers.append({'id': paper_id, 'title': title, 'abstract': abstract, 'url': paper_id})
    return papers


def _score(paper: dict) -> int:
    """Score a paper abstract by keyword heuristic."""
    text = (paper.get('title', '') + ' ' + paper.get('abstract', '')).lower()

    # Hard skip
    for kw in SKIP_KEYWORDS:
        if kw in text:
            return -1

    score = 0
    for kw, weight in SCORED_KEYWORDS.items():
        if kw in text:
            score += weight
    return score


def discover(days: int = 7) -> list[dict]:
    """Return top-N scored papers from arXiv."""
    seen_ids: set[str] = set()
    all_papers = []
    for cat in CATEGORIES:
        papers = _arxiv_search(cat, days)
        for p in papers:
            if p['id'] not in seen_ids:
                seen_ids.add(p['id'])
                p['score'] = _score(p)
                if p['score'] > 0:
                    all_papers.append(p)

    all_papers.sort(key=lambda p: -p['score'])
    return all_papers[:TOP_N]


def insert_candidates(papers: list[dict]) -> int:
    """Insert papers into research_candidates. Returns count inserted."""
    if not papers:
        return 0

    pg_uri = os.environ.get('POSTGRES_URI')
    if not pg_uri:
        print('[arxiv] No POSTGRES_URI — printing instead:')
        for p in papers:
            print(f"  [{p['score']}] {p['title'][:80]} — {p['url']}")
        return 0

    import psycopg2
    inserted = 0
    conn = psycopg2.connect(pg_uri)
    try:
        cur = conn.cursor()
        for p in papers:
            spec = json.dumps({
                'strategy_id':           None,
                'hypothesis_one_liner':  p['title'],
                'signal_logic':          p['abstract'][:500],
                'data_requirements':     ['prices'],
                'rejection_reason_if_any': None,
                'source':                'arxiv',
                'arxiv_score':           p['score'],
            })
            # Skip if URL already in queue
            cur.execute('SELECT 1 FROM research_candidates WHERE source_url = %s', (p['url'],))
            if cur.fetchone():
                continue
            cur.execute(
                """INSERT INTO research_candidates
                     (source_url, submitted_by, priority, status, hunter_result_json)
                   VALUES (%s, 'arxiv-discovery', %s, 'pending', %s::jsonb)""",
                (p['url'], min(p['score'], 5), spec)
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()
    return inserted


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=7, help='Days back to search')
    args = parser.parse_args()

    print(f'[arxiv] Searching last {args.days} days across {CATEGORIES}...')
    papers = discover(args.days)

    if not papers:
        print('[arxiv] No scored papers found.')
        sys.exit(0)

    n = insert_candidates(papers)
    print(f'[arxiv] Inserted {n} of {len(papers)} top papers into research_candidates.')
    for p in papers:
        print(f"  [{p['score']}] {p['title'][:80]}")
