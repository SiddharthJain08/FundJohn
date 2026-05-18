"""
backfill_gate_decisions.py — Reconstruct historical paper_gate_decisions rows
from implementation_queue + research_candidates so the Phase 1 !hit-rate funnel
has baseline data before the Opus curator ships.

Idempotent: safe to run repeatedly. Uses
(candidate_id, gate_name, outcome) as a dedup key via NOT EXISTS.

Usage:
    python3 src/ingestion/backfill_gate_decisions.py
"""

import os
import sys
import json
import uuid
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from src.research.paper_fingerprint import compute_fingerprint  # noqa: E402


def _classify_backtest_reason(br: dict) -> str:
    if not br:
        return 'backtest_missing'
    if br.get('error'):
        return 'backtest_error'
    if (br.get('trade_count') or 0) < 20:
        return 'trade_count_below_floor'
    if (br.get('sharpe') or 0) < 0.5:
        return 'sharpe_below_floor'
    if (br.get('max_dd') or 0) > 0.4:
        return 'dd_above_ceiling'
    return 'windows_insufficient'


def _insert_if_absent(cur, candidate_id, gate_name, outcome, reason_code,
                      reason_detail, metadata, strategy_id=None):
    # Resolve paper_id via source_url match into research_corpus.
    cur.execute(
        """SELECT p.paper_id
             FROM research_candidates rc
             LEFT JOIN research_corpus p ON p.source_url = rc.source_url
            WHERE rc.candidate_id = %s
            LIMIT 1""",
        (candidate_id,)
    )
    row = cur.fetchone()
    paper_id = row[0] if row else None

    cur.execute(
        """INSERT INTO paper_gate_decisions
             (paper_id, candidate_id, strategy_id, gate_name, outcome,
              reason_code, reason_detail, metadata)
           SELECT %s, %s, %s, %s, %s, %s, %s, %s::jsonb
           WHERE NOT EXISTS (
             SELECT 1 FROM paper_gate_decisions
             WHERE candidate_id = %s AND gate_name = %s AND outcome = %s
           )""",
        (
            paper_id, candidate_id, strategy_id, gate_name, outcome,
            reason_code, reason_detail, json.dumps(metadata) if metadata else None,
            candidate_id, gate_name, outcome,
        )
    )
    return cur.rowcount


def _backfill_corpus_from_candidates(cur) -> int:
    """Synthesize research_corpus rows for historical research_candidates so
    backfilled gate decisions resolve to a paper_id and appear in the funnel view.

    Title/abstract are pulled from hunter_result_json where present, otherwise
    falls back to the source_url as the title with empty abstract. Idempotent
    via the existing UNIQUE(source_url) constraint on research_corpus.
    """
    cur.execute(
        """SELECT candidate_id, source_url, hunter_result_json, submitted_at
             FROM research_candidates
            WHERE source_url IS NOT NULL"""
    )
    rows = cur.fetchall()
    inserted = 0
    for _cid, url, raw, submitted_at in rows:
        try:
            spec = raw if isinstance(raw, dict) else (json.loads(raw) if raw else {})
        except Exception:
            spec = {}
        title    = (spec.get('hypothesis_one_liner')
                    or spec.get('title')
                    or url[:200]) if isinstance(spec, dict) else url[:200]
        abstract = (spec.get('signal_logic') or spec.get('abstract') or '') if isinstance(spec, dict) else ''
        source   = (spec.get('source') if isinstance(spec, dict) else None) or 'manual'

        # Phase 2-followup #17 — attempt fingerprint dedup for shape-consistency
        # with the other 3 ingest sites.  Historical research_candidates rarely
        # carry authors/year (the legacy keyword pipeline didn't preserve them),
        # so this site is effectively a no-op for most rows — fp will be None
        # and the INSERT proceeds normally with paper_fingerprint=NULL.
        spec_authors = spec.get('authors') if isinstance(spec, dict) else None
        if isinstance(spec_authors, str):
            spec_authors = [s.strip() for s in spec_authors.split(',') if s.strip()]
        elif not isinstance(spec_authors, list):
            spec_authors = []
        spec_year = None
        spec_pub = spec.get('published_date') if isinstance(spec, dict) else None
        if isinstance(spec_pub, str) and len(spec_pub) >= 4:
            try:
                spec_year = int(spec_pub[:4])
            except ValueError:
                spec_year = None
        elif isinstance(spec, dict) and isinstance(spec.get('year'), int):
            spec_year = spec['year']
        fp = compute_fingerprint(title or '', spec_authors, spec_year)

        dedup_dropped = False
        dedup_group_id = None
        if fp:
            cur.execute(
                "SELECT paper_id, dedup_group_id FROM research_corpus "
                "WHERE paper_fingerprint = %s "
                "ORDER BY ingested_at ASC LIMIT 1",
                (fp,),
            )
            existing = cur.fetchone()
            if existing:
                # Cross-source duplicate — share group_id with the original;
                # if the original had NULL group_id, mint one and back-fill.
                # NOTE: small race under concurrent writers; acceptable for
                # the Saturday batch ingest (no concurrent inserters).
                existing_group = existing[1]
                if existing_group is None:
                    existing_group = str(uuid.uuid4())
                    cur.execute(
                        "UPDATE research_corpus SET dedup_group_id = %s "
                        "WHERE paper_id = %s",
                        (existing_group, existing[0]),
                    )
                dedup_group_id = existing_group
                dedup_dropped = True

        cur.execute(
            """INSERT INTO research_corpus
                 (source, source_url, title, abstract, ingested_at, raw_metadata,
                  paper_fingerprint, dedup_group_id, dedup_dropped)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
               ON CONFLICT (source_url) DO NOTHING""",
            (source, url, title or url[:200], abstract or '',
             submitted_at, json.dumps({'backfill_from': 'research_candidates'}),
             fp, dedup_group_id, dedup_dropped)
        )
        if cur.rowcount:
            inserted += 1
    return inserted


def backfill() -> dict:
    pg_uri = os.environ.get('POSTGRES_URI')
    if not pg_uri:
        print('[backfill] No POSTGRES_URI set.', file=sys.stderr)
        return {}

    import psycopg2
    counts = {'corpus': 0, 'paperhunter': 0, 'researchjohn': 0, 'validate': 0, 'convergence': 0, 'promotion': 0}

    conn = psycopg2.connect(pg_uri)
    try:
        cur = conn.cursor()

        # Step 0: populate research_corpus for historical candidates so paper_id
        # resolves in subsequent gate-decision inserts.
        counts['corpus'] = _backfill_corpus_from_candidates(cur)
        conn.commit()

        # Step 0b: stitch existing orphaned paper_gate_decisions to the paper_ids
        # we just created. Idempotent — only touches rows where paper_id IS NULL.
        cur.execute(
            """UPDATE paper_gate_decisions d
               SET    paper_id = p.paper_id
               FROM   research_candidates rc
               JOIN   research_corpus p ON p.source_url = rc.source_url
               WHERE  d.candidate_id = rc.candidate_id
                 AND  d.paper_id IS NULL"""
        )
        stitched = cur.rowcount
        conn.commit()
        counts['stitched_paper_ids'] = stitched

        # ── PaperHunter: research_candidates.hunter_result_json.rejection_reason_if_any
        cur.execute(
            """SELECT candidate_id, hunter_result_json
                 FROM research_candidates
                WHERE hunter_result_json IS NOT NULL"""
        )
        for cid, raw in cur.fetchall():
            try:
                spec = raw if isinstance(raw, dict) else json.loads(raw)
            except Exception:
                continue
            rej = spec.get('rejection_reason_if_any') if isinstance(spec, dict) else None
            outcome = 'reject' if rej else 'pass'
            counts['paperhunter'] += _insert_if_absent(
                cur, cid, 'paperhunter', outcome,
                reason_code=rej, reason_detail=None,
                metadata={'source': 'backfill'},
                strategy_id=(spec or {}).get('strategy_id'),
            ) or 0

        # ── ResearchJohn: research_candidates.status tells us ready/buildable/blocked
        cur.execute(
            """SELECT candidate_id, status, hunter_result_json
                 FROM research_candidates
                WHERE status IN ('done', 'blocked_buildable', 'blocked_rejected', 'blocked_unclassified')"""
        )
        for cid, status, raw in cur.fetchall():
            try:
                spec = raw if isinstance(raw, dict) else (json.loads(raw) if raw else {})
            except Exception:
                spec = {}
            if status == 'done':
                outcome, reason = 'pass', 'ready'
            elif status == 'blocked_buildable':
                outcome, reason = 'buildable', 'missing_columns'
            elif status == 'blocked_rejected':
                outcome, reason = 'reject', 'blocked'
            else:
                outcome, reason = 'error', 'unclassified'
            counts['researchjohn'] += _insert_if_absent(
                cur, cid, 'researchjohn', outcome,
                reason_code=reason, reason_detail=None,
                metadata={'source': 'backfill', 'candidate_status': status},
                strategy_id=(spec or {}).get('strategy_id'),
            ) or 0

        # ── Validate + Convergence + Promotion: implementation_queue rows
        cur.execute(
            """SELECT candidate_id, strategy_spec, status, error_log, backtest_result
                 FROM implementation_queue"""
        )
        for cid, spec, status, err_log, bt in cur.fetchall():
            strat_id = (spec or {}).get('strategy_id') if isinstance(spec, dict) else None
            bt_obj = bt if isinstance(bt, dict) else (json.loads(bt) if bt else None)

            if status == 'validation_failed':
                counts['validate'] += _insert_if_absent(
                    cur, cid, 'validate', 'reject',
                    reason_code='contract_violation', reason_detail=(err_log or '')[:1000],
                    metadata={'source': 'backfill'}, strategy_id=strat_id,
                ) or 0
            elif status in ('backtest_failed', 'promoted', 'done'):
                # done/promoted both imply validation passed
                counts['validate'] += _insert_if_absent(
                    cur, cid, 'validate', 'pass',
                    reason_code=None, reason_detail=None,
                    metadata={'source': 'backfill'}, strategy_id=strat_id,
                ) or 0

            if status == 'backtest_failed':
                counts['convergence'] += _insert_if_absent(
                    cur, cid, 'convergence', 'reject',
                    reason_code=_classify_backtest_reason(bt_obj),
                    reason_detail=(err_log or '')[:500] or None,
                    metadata={**(bt_obj or {}), 'source': 'backfill'}, strategy_id=strat_id,
                ) or 0
            elif status == 'promoted':
                counts['convergence'] += _insert_if_absent(
                    cur, cid, 'convergence', 'pass',
                    reason_code=None, reason_detail=None,
                    metadata={**(bt_obj or {}), 'source': 'backfill'}, strategy_id=strat_id,
                ) or 0
                counts['promotion'] += _insert_if_absent(
                    cur, cid, 'promotion', 'pass',
                    reason_code='auto_backtest_promoted', reason_detail=None,
                    metadata={**(bt_obj or {}), 'source': 'backfill'}, strategy_id=strat_id,
                ) or 0

        conn.commit()
    finally:
        conn.close()

    return counts


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.parse_args()
    counts = backfill()
    if not counts:
        print('[backfill] Nothing inserted (no DB connection or empty history).')
        sys.exit(0)
    print('[backfill] Inserted paper_gate_decisions rows:')
    for gate, n in counts.items():
        print(f'  {gate:15s} {n:5d}')
    print(f'[backfill] Total: {sum(counts.values())}')
