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

Adapters: options_eod (chains), insider (Form 4 stream), financials (today's
reporters). Categories without one are recorded in the manifest with
adapter="none" so the remaining gaps stay visible. The manifest also records
`master_ticker_coverage` per category — an adapter closes a FRESHNESS gap and
cannot close a COVERAGE gap the master never had.

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


def _ingest_insider(tickers, as_of, budget_s, dry_run=False) -> dict:
    """New Form 4s since the master's newest filing, scoped to the universe.

    Cheapest of the three (a few paged calls over the global filing stream) and
    the one with the most genuinely intraday content — Form 4s post throughout
    the trading day."""
    import pandas as pd
    from ingestion.intraday_insider import build_overlay
    from ingestion.intraday_options import write_overlay, overlay_path

    ts = pd.Timestamp(as_of)
    if dry_run:
        return {'adapter': 'intraday_insider', 'dry_run': True,
                'requested': len(tickers)}
    df, stats = build_overlay(tickers, ts, budget_s=budget_s)
    result = {'adapter': 'intraday_insider', **stats}
    # An empty result is a QUIET DAY, not a failure: no new filings in the
    # window is the common case. Only write when there is something to serve.
    if not df.empty:
        write_overlay(df, ts, category='insider')
        result['path'] = str(overlay_path(ts, 'insider'))
    return result


def _ingest_financials(tickers, as_of, budget_s, dry_run=False) -> dict:
    """Newly-published quarters for in-universe tickers that just reported."""
    import pandas as pd
    from ingestion.intraday_financials import build_overlay
    from ingestion.intraday_options import write_overlay, overlay_path

    ts = pd.Timestamp(as_of)
    if dry_run:
        return {'adapter': 'intraday_financials', 'dry_run': True,
                'requested': len(tickers)}
    df, stats = build_overlay(tickers, ts, budget_s=budget_s)
    result = {'adapter': 'intraday_financials', **stats}
    if not df.empty:
        write_overlay(df, ts, category='financials')
        result['path'] = str(overlay_path(ts, 'financials'))
    return result


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


# Categories this tier can fetch, in RUN ORDER. Cheapest first, so a budget
# overrun costs the least-complete adapter rather than the one still queued —
# and note the order is explicit, not `sorted(plan['categories'])`, which would
# have put the ~270s financials sweep ahead of the ~294s options fetch.
# Everything else in the plan is still reported so the gap stays visible.
ADAPTERS = {
    'insider':     _ingest_insider,      # ~1s   — paged global filing stream
    'options_eod': _ingest_options,      # ~294s — 5,173 chains
    'financials':  _ingest_financials,   # ~270s — ~283 reporters x 4 endpoints
}
ADAPTER_ORDER = ['insider', 'options_eod', 'financials']


_MASTER_FOR = {'options_eod': 'options_eod', 'financials': 'financials',
               'insider': 'insider'}


def _master_coverage(category: str, wanted) -> dict | None:
    """{tickers_in_master, wanted, frac} for the category's master parquet.

    The adapter closes a FRESHNESS gap; it cannot close a COVERAGE gap the
    master never had. Surfacing both keeps them distinguishable."""
    name = _MASTER_FOR.get(category)
    if not name or not wanted:
        return None
    path = ROOT / 'data' / 'master' / f'{name}.parquet'
    if not path.exists():
        return {'in_master': 0, 'wanted': len(wanted), 'frac': 0.0}
    try:
        import pyarrow.parquet as pq
        have = set(pq.read_table(path, columns=['ticker']).column('ticker').to_pylist())
    except Exception as exc:  # noqa: BLE001
        logger.warning('coverage probe failed for %s: %s', category, exc)
        return None
    n = len(have & set(wanted))
    return {'in_master': n, 'wanted': len(wanted),
            'frac': round(n / len(wanted), 4)}


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
    # Adapter-backed categories in explicit cost order, then the rest.
    ordered = ([c for c in ADAPTER_ORDER if c in plan['categories']] +
               [c for c in sorted(plan['categories']) if c not in ADAPTER_ORDER])
    for cat in ordered:
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
                            **ADAPTERS[cat](plan['categories'][cat], run_date,
                                            remaining, args.dry_run)}
        except Exception as exc:  # noqa: BLE001
            logger.error('%s ingest FAILED: %s', cat, exc)
            results[cat] = {'adapter': ADAPTERS[cat].__name__, 'consumers': consumers,
                            'error': str(exc)[:300]}

    # Freshness is not coverage. An adapter can run perfectly and still leave a
    # category thin because the MASTER only ever covered part of the universe
    # (financials: 817 of 5,173 tickers as of 2026-07-30). Recording both stops
    # "adapter live" from reading as "category covered".
    for cat, res in results.items():
        if res.get('adapter') in (None, 'none', 'skipped') or res.get('dry_run'):
            continue
        res['master_ticker_coverage'] = _master_coverage(cat, plan['categories'].get(cat, []))

    manifest = {
        'run_date': run_date.isoformat(),
        'regime_state': regime,
        'acting': plan['acting'],
        'marketwide': plan['marketwide'],
        'categories': results,
        'elapsed_s': round(time.monotonic() - t0, 1),
        'budget_s': args.budget,
        # plan_delta (tier 3) needs to know what tier 1 already covered. Keyed
        # on the fetch SUCCEEDING, not on rows returned: zero new rows is the
        # normal state for an event stream on a quiet day, and treating that as
        # "not covered" would make tier 3 re-fetch it on every regime change.
        'fresh': {'categories': {c: plan['categories'][c]
                                 for c, r in results.items()
                                 if r.get('adapter', '').startswith('intraday_')
                                 and not r.get('error') and not r.get('dry_run')},
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
