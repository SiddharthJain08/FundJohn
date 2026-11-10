#!/usr/bin/env python3
"""
register_s_beta_btc.py — mirror S_beta_btc (crypto benchmark sleeve, crypto
engine Phase 3 Task 3) into strategy_registry.

Shape follows the 08-29 runbook's S_beta_spy registration
(docs/superpowers/plans/2026-08-29-benchmark-relative-sizing.md, Task 14):

    INSERT INTO strategy_registry (id, name, description, tier,
        implementation_path, parameters, universe, signal_frequency, status)
    VALUES (..., %s::jsonb, ARRAY['BTC-USD'], 'daily', 'pending_approval')
    ON CONFLICT (id) DO UPDATE SET parameters = strategy_registry.parameters || EXCLUDED.parameters, ...

parameters = {'benchmark_sleeve': True, 'instrument_class': 'crypto',
'benchmark_ticker': 'BTC-USD'} — exactly the keys
execution.benchmark_sleeve.sleeve_benchmark() reads (strategy_weights' S_m
call site + the activation assigner's equity/crypto sleeve split).

Idempotent: re-running merges the parameters (jsonb ||, existing other keys
kept) and re-asserts universe / implementation_path, but NEVER touches
`status` — a re-run after promotion must not demote the sleeve back to
pending_approval.

Usage:
    python3 scripts/register_s_beta_btc.py --dry-run   # print SQL + params; never connects
    python3 scripts/register_s_beta_btc.py             # apply (controller merge-phase step)

--dry-run imports neither psycopg2 nor dotenv and never reads .env.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))

STRATEGY_ID = 'S_beta_btc'
IMPLEMENTATION_PATH = 'src/strategies/implementations/S_beta_btc.py'
PARAMETERS = {'benchmark_sleeve': True, 'instrument_class': 'crypto', 'benchmark_ticker': 'BTC-USD'}
UNIVERSE = ['BTC-USD']

SQL = """
INSERT INTO strategy_registry (id, name, description, tier, implementation_path,
                               parameters, universe, signal_frequency, status)
VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::text[], %s, 'pending_approval')
ON CONFLICT (id) DO UPDATE SET
    parameters          = strategy_registry.parameters || EXCLUDED.parameters,
    universe            = EXCLUDED.universe,
    implementation_path = EXCLUDED.implementation_path
""".strip()


def build_statement() -> tuple[str, tuple]:
    """(sql, params) for the upsert. Pure: no DB, no env."""
    from strategies.implementations.S_beta_btc import BetaBtc, STRATEGY_ID as _SID
    assert _SID == STRATEGY_ID and BetaBtc.benchmark_sleeve is True
    assert BetaBtc.instrument_class == PARAMETERS['instrument_class']
    assert BetaBtc.BENCHMARK == PARAMETERS['benchmark_ticker']
    params = (STRATEGY_ID, BetaBtc.name, BetaBtc.description, int(BetaBtc.tier),
              IMPLEMENTATION_PATH, json.dumps(PARAMETERS, sort_keys=True), list(UNIVERSE),
              BetaBtc.signal_frequency)
    return SQL, params


def _apply(sql: str, params: tuple) -> int:
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    conn = psycopg2.connect(os.environ['POSTGRES_URI'])
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            cur.execute("SELECT id, status, parameters, universe FROM strategy_registry WHERE id = %s",
                        (STRATEGY_ID,))
            row = cur.fetchone()
        print(f'[register_s_beta_btc] upserted: {row}')
        if not row or not (row[2] or {}).get('benchmark_sleeve'):
            print('[register_s_beta_btc] ERROR: row missing or benchmark_sleeve not set', file=sys.stderr)
            return 1
        return 0
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--dry-run', action='store_true', help='print SQL + params; never connect')
    args = ap.parse_args(argv)
    sql, params = build_statement()
    if args.dry_run:
        print('[register_s_beta_btc] DRY RUN — no connection made')
        print(sql)
        print('params:', json.dumps(list(params), indent=2))
        return 0
    return _apply(sql, params)


if __name__ == '__main__':
    sys.exit(main())
