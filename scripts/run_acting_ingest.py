#!/usr/bin/env python3
"""Tier-1 acting-set ingest — the 14:30 ET pre-compute fetch.

Three-tier ingestion (operator directive 2026-07-29):
  tier 1  14:30 ET  — everything the ACTING strategies consume, fetched fresh
  tier 2  16:15 ET  — the EOD collect: real closes + every non-acting gap
  tier 3  on regime change — only the NEW acting set's delta (plan_delta)

This is tier 1. It resolves what the acting set consumes (acting_ingest_plan)
and writes DAY-SCOPED OVERLAYS under data/derived/intraday/<date>/ — never a
master. The masters stay append-only, written by the 16:15 collect.

Runs as its OWN cron ahead of the 15:00 compute rather than as a pipeline
step: a job that overruns here leaves a partial overlay and the engine falls
back per-ticker to the EOD panel, whereas an in-chain step that overruns
delays execution itself and can trip the 15:55 "no sized handoff" abort — a
silent no-trade day. Hence --budget, a HARD global wall clock.

Coverage is REPORTED, not enforced: a thin overlay still beats yesterday's
data, but "we asked and got nothing" must never look like "we didn't ask"
(feedback_silent_failure_pattern). manifest.json records attempted /
succeeded / skipped per category, and system_checks reads it.

Adapters exist for: options_eod. Categories without one are recorded in the
manifest with adapter="none" so the remaining gaps stay visible.

Run: python3 scripts/run_acting_ingest.py [--date YYYY-MM-DD] [--budget 900]
     [--categories options_eod] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date as _date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('acting_ingest')

# Categories this tier can actually fetch today. Everything else in the plan is
# still reported so the gap is visible rather than assumed-covered.
ADAPTERS = {'options_eod'}


def _ingest_options(tickers, as_of, budget_s, dry_run=False) -> dict:
    """Fetch today's chains and write BOTH overlays from ONE fetch: the raw
    contract rows the live engine splices into its panel, and the aggregate
    rows in options_aggregates shape for inspection and backtest parity."""
    import pandas as pd
    from ingestion.intraday_options import (fetch_raw_frame, build_overlay,
                                            write_overlay, overlay_path)

    ts = pd.Timestamp(as_of)
    if dry_run:
        return {'adapter': 'intraday_options', 'dry_run': True,
                'requested': len(tickers)}

    raw, stats = fetch_raw_frame(tickers, ts, budget_s=budget_s)
    result = {'adapter': 'intraday_options', **stats}
    if raw.empty:
        result['error'] = 'no rows fetched'
        return result

    write_overlay(raw, ts, category='options_raw')
    result['raw_path'] = str(overlay_path(ts, 'options_raw'))
    # The aggregate is a convenience artifact; its failure must not discard the
    # raw overlay, which is what the engine actually reads.
    try:
        agg = build_overlay(sorted(raw['ticker'].unique()), ts, raw=raw)
        write_overlay(agg, ts, category='options')
        result['aggregate_rows'] = len(agg)
    except Exception as exc:  # noqa: BLE001
        logger.warning('aggregate overlay failed (raw overlay kept): %s', exc)
        result['aggregate_error'] = str(exc)[:200]
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None)
    ap.add_argument('--budget', type=float, default=900.0,
                    help='hard wall-clock seconds for the whole ingest')
    ap.add_argument('--categories', default=None,
                    help='comma-separated subset (default: every category the '
                         'plan names that has an adapter)')
    ap.add_argument('--dry-run', action='store_true',
                    help='resolve and report the plan without fetching')
    args = ap.parse_args()
    run_date = _date.fromisoformat(args.date) if args.date else _date.today()

    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        logger.error('POSTGRES_URI not set; aborting')
        return 2

    import psycopg2
    from execution.acting_ingest_plan import resolve_ingest_plan

    t0 = time.monotonic()
    conn = psycopg2.connect(uri)
    try:
        cur = conn.cursor()
        cur.execute('SELECT state FROM market_regime ORDER BY updated_at DESC LIMIT 1')
        row = cur.fetchone()
        if not row:
            logger.error('no regime row — cannot scope the acting set')
            return 1
        regime = row[0]
        plan = resolve_ingest_plan(cur, regime, run_date)
    finally:
        conn.close()

    logger.info('acting set: %d strategies in %s; categories %s; marketwide %s',
                len(plan['acting']), regime,
                {c: len(t) for c, t in plan['categories'].items()},
                plan['marketwide'] or '[]')

    wanted = ({c.strip() for c in args.categories.split(',')} if args.categories
              else set(plan['categories']))
    results: dict = {}
    for cat in sorted(plan['categories']):
        consumers = plan['consumers'].get(cat, [])
        if cat not in wanted:
            results[cat] = {'adapter': 'skipped', 'consumers': consumers}
            continue
        if cat not in ADAPTERS:
            # prices are refreshed in-memory by the close-proxy injection
            # inside the signals step, so they are covered without an adapter.
            served = ('close_proxy_snapshot (signals step)'
                      if cat == 'prices' else None)
            results[cat] = {'adapter': served or 'none', 'consumers': consumers,
                            'tickers': len(plan['categories'][cat]),
                            'stale_from': None if served else 'last EOD collect'}
            if not served:
                logger.warning('category %s has NO intraday adapter — its %d '
                               'consumers (%s) act on the last EOD collect',
                               cat, len(consumers), ','.join(consumers[:4]))
            continue
        remaining = args.budget - (time.monotonic() - t0)
        if remaining <= 30:
            results[cat] = {'adapter': 'none', 'error': 'budget exhausted',
                            'consumers': consumers}
            logger.error('budget exhausted before %s', cat)
            continue
        logger.info('ingesting %s: %d tickers, %.0fs of budget left',
                    cat, len(plan['categories'][cat]), remaining)
        try:
            results[cat] = {'consumers': consumers,
                            **_ingest_options(plan['categories'][cat], run_date,
                                              remaining, args.dry_run)}
        except Exception as exc:  # noqa: BLE001
            logger.error('%s ingest FAILED: %s', cat, exc)
            results[cat] = {'adapter': 'intraday_options', 'consumers': consumers,
                            'error': str(exc)[:300]}

    manifest = {
        'run_date': run_date.isoformat(),
        'regime_state': regime,
        'acting': plan['acting'],
        'marketwide': plan['marketwide'],
        'categories': results,
        'elapsed_s': round(time.monotonic() - t0, 1),
        'budget_s': args.budget,
        # plan_delta (tier 3) needs to know what tier 1 already covered.
        'fresh': {'categories': {c: plan['categories'][c] for c in plan['categories']
                                 if results.get(c, {}).get('rows')},
                  'marketwide': []},
    }
    out = ROOT / 'data' / 'derived' / 'intraday' / run_date.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / 'manifest.json.tmp'
    tmp.write_text(json.dumps(manifest, indent=2, default=str))
    os.replace(tmp, out / 'manifest.json')
    logger.info('tier-1 ingest done in %.1fs -> %s',
                manifest['elapsed_s'], out / 'manifest.json')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
