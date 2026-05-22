#!/usr/bin/env python3
"""SP-2 Phase B v1.5 gap fix: seed ``alpaca_tradable_universe.first_seen_at``
from FMP ``profile.ipoDate``.

Problem this fixes
------------------
The Phase B metadata backfill filters out rows where
``first_seen_at > snapshot_date`` (a ticker isn't yet "listed" on that date).
SP-1 started tracking the Alpaca asset feed on 2026-05-14, so the entire
backfill universe currently has ``first_seen_at = 2026-05-14`` — which means
the metadata builder sees an empty universe for every month in 2021-2025 and
quarantines the chunk as "empty".

Fix
---
Pull the true IPO date from FMP's ``/stable/profile`` endpoint (formerly
``/api/v3/profile``, which returns 403 "Legacy Endpoint" on FMP Starter
since 2025-08-31) and ``LEAST``-merge it into ``first_seen_at``. After this
seed runs, the metadata backfill should produce real snapshots for every
month back to each ticker's true IPO.

Operator usage::

    export $(grep -E "^POSTGRES_URI=|^FMP_API_KEY=" .env | head -2)
    python3 scripts/seed_first_seen_at_from_fmp_ipo.py --top-n 404

Idempotency
-----------
Writes a marker file ``data/.fmp_ipo_seeded_v1.txt`` on success. Re-runs
without ``--force`` exit with REFUSED. This mirrors the frozen-artifact
convention in ``scripts/build_backfill_universe.py`` — a future v2 of this
seed (e.g. expanding past the 404-ticker universe) ships as a sibling
marker file ``.fmp_ipo_seeded_v2.txt``.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import requests

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_FILE = ROOT / 'data' / '.backfill_universe_v1.txt'
MARKER = ROOT / 'data' / '.fmp_ipo_seeded_v1.txt'

FMP_PROFILE_URL = 'https://financialmodelingprep.com/stable/profile'
HTTP_TIMEOUT_S = 10.0
INTER_CALL_SLEEP_S = 0.2  # FMP Starter rate-limit safety
RETRY_BACKOFFS_S = (1.0, 5.0, 30.0)  # exponential backoff on 403/429


def _load_universe(args) -> list[str]:
    if args.tickers:
        return [t.strip().upper() for t in args.tickers.split(',') if t.strip()]
    if not UNIVERSE_FILE.exists():
        raise FileNotFoundError(
            f'backfill universe file missing at {UNIVERSE_FILE}; '
            'run scripts/build_backfill_universe.py first'
        )
    tickers = [
        line.strip()
        for line in UNIVERSE_FILE.read_text().splitlines()
        if line.strip()
    ]
    if args.top_n is not None:
        tickers = tickers[: args.top_n]
    return tickers


def _fetch_ipo_date(symbol: str, api_key: str) -> tuple[str | None, str]:
    """Return (ipoDate_str_or_None, outcome_tag).

    outcome_tag ∈ {'ok', 'missing_profile', 'no_ipo_date', 'parse_error',
                   'http_error'}.
    """
    last_exc: Exception | None = None
    for attempt, backoff in enumerate((0.0,) + RETRY_BACKOFFS_S):
        if backoff:
            time.sleep(backoff)
        try:
            r = requests.get(
                FMP_PROFILE_URL,
                params={'symbol': symbol, 'apikey': api_key},
                timeout=HTTP_TIMEOUT_S,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            continue  # retry path
        if r.status_code in (403, 429):
            last_exc = RuntimeError(
                f'FMP {r.status_code} on {symbol} '
                f'(attempt {attempt + 1}/{len(RETRY_BACKOFFS_S) + 1})'
            )
            continue
        if r.status_code != 200:
            return None, 'http_error'
        try:
            data = r.json()
        except ValueError:
            return None, 'parse_error'
        if not isinstance(data, list) or not data:
            return None, 'missing_profile'
        ipo = data[0].get('ipoDate')
        if not ipo:
            return None, 'no_ipo_date'
        return ipo, 'ok'
    # exhausted retries
    sys.stderr.write(
        f'  [warn] {symbol}: exhausted FMP retries ({last_exc}); skipping\n'
    )
    return None, 'http_error'


def _parse_ipo_date(ipo_str: str) -> datetime | None:
    """Parse FMP ipoDate ('YYYY-MM-DD') into a UTC-aware datetime.

    Returns None on parse failure (caller counts as parse_error).
    """
    try:
        dt = datetime.strptime(ipo_str, '%Y-%m-%d')
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc)


def main(args) -> int:
    pg_uri = os.environ.get('POSTGRES_URI')
    api_key = os.environ.get('FMP_API_KEY')
    if not pg_uri:
        sys.stderr.write(
            'POSTGRES_URI env var not set. '
            "Run: export $(grep -E '^POSTGRES_URI=' .env | head -1)\n"
        )
        return 2
    if not api_key:
        sys.stderr.write(
            'FMP_API_KEY env var not set. '
            "Run: export $(grep -E '^FMP_API_KEY=' .env | head -1)\n"
        )
        return 2

    if MARKER.exists() and not args.force:
        try:
            display = MARKER.relative_to(ROOT)
        except ValueError:
            display = MARKER
        sys.stderr.write(
            f'REFUSED: {display} already exists; pass --force to re-run '
            '(or ship a v2 sibling file per the frozen-artifact convention)\n'
        )
        return 2

    tickers = _load_universe(args)
    if not tickers:
        sys.stderr.write('No tickers to process; exiting.\n')
        return 1

    print(
        f'Seeding first_seen_at from FMP ipoDate for {len(tickers)} tickers '
        f'(dry_run={args.dry_run})'
    )

    stats = {
        'updated': 0,
        'unchanged': 0,
        'missing_profile': 0,
        'no_ipo_date': 0,
        'parse_error': 0,
        'http_error': 0,
    }
    years_backfilled: list[float] = []

    conn = psycopg2.connect(pg_uri)
    try:
        for i, symbol in enumerate(tickers, 1):
            if i > 1:
                time.sleep(INTER_CALL_SLEEP_S)
            if i % 50 == 0:
                print(f'  [{i}/{len(tickers)}] '
                      f'updated={stats["updated"]} unchanged={stats["unchanged"]} '
                      f'missing={stats["missing_profile"]} '
                      f'no_ipo={stats["no_ipo_date"]} '
                      f'err={stats["parse_error"] + stats["http_error"]}')

            ipo_str, outcome = _fetch_ipo_date(symbol, api_key)
            if outcome != 'ok':
                stats[outcome] += 1
                continue

            ipo_dt = _parse_ipo_date(ipo_str)
            if ipo_dt is None:
                stats['parse_error'] += 1
                continue

            # WHERE first_seen_at > %s lets us read rowcount as a clean
            # "updated vs unchanged" signal — no need to SELECT-then-compare.
            if args.dry_run:
                # Mirror the live path's logic: SELECT current to classify.
                with conn.cursor() as cur:
                    cur.execute(
                        'SELECT first_seen_at FROM alpaca_tradable_universe '
                        'WHERE symbol = %s',
                        (symbol,),
                    )
                    row = cur.fetchone()
                if row is None:
                    stats['missing_profile'] += 1  # not in alpaca universe
                    continue
                current = row[0]
                if current > ipo_dt:
                    stats['updated'] += 1
                    delta_years = (current - ipo_dt).days / 365.25
                    years_backfilled.append(delta_years)
                    print(f'  [dry-run] {symbol}: '
                          f'{current.date()} → {ipo_dt.date()} '
                          f'({delta_years:.1f}y backfilled)')
                else:
                    stats['unchanged'] += 1
                continue

            with conn.cursor() as cur:
                # SELECT current first to track "years backfilled" stat,
                # then conditionally UPDATE only when ipoDate is earlier.
                cur.execute(
                    'SELECT first_seen_at FROM alpaca_tradable_universe '
                    'WHERE symbol = %s',
                    (symbol,),
                )
                row = cur.fetchone()
                if row is None:
                    # Ticker is in our backfill universe file but not in
                    # alpaca_tradable_universe. Shouldn't happen normally
                    # (build_backfill_universe.py joined against it), but
                    # the table can drift if refresh ran in between.
                    stats['missing_profile'] += 1
                    continue
                current = row[0]
                if current > ipo_dt:
                    cur.execute(
                        'UPDATE alpaca_tradable_universe '
                        'SET first_seen_at = %s, updated_at = NOW() '
                        'WHERE symbol = %s',
                        (ipo_dt, symbol),
                    )
                    delta_years = (current - ipo_dt).days / 365.25
                    years_backfilled.append(delta_years)
                    stats['updated'] += 1
                else:
                    stats['unchanged'] += 1
        if not args.dry_run:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    median_years = statistics.median(years_backfilled) if years_backfilled else 0.0

    print(
        f'\nSeeded first_seen_at for {len(tickers)} tickers from FMP profile.ipoDate:\n'
        f'  updated:         {stats["updated"]} '
        f'({median_years:.1f}y median backfilled)\n'
        f'  unchanged:       {stats["unchanged"]} (FMP ipoDate >= current first_seen_at)\n'
        f'  missing_profile: {stats["missing_profile"]} (FMP returned empty)\n'
        f'  no_ipo_date:     {stats["no_ipo_date"]} (profile present but no ipoDate field)\n'
        f'  parse_error:     {stats["parse_error"]}\n'
        f'  http_error:      {stats["http_error"]} (after exhausted retries)'
    )

    if not args.dry_run:
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(
            f'fmp_ipo_seed_v1\n'
            f'run_at_utc={datetime.now(timezone.utc).isoformat()}\n'
            f'tickers_processed={len(tickers)}\n'
            f'updated={stats["updated"]}\n'
            f'unchanged={stats["unchanged"]}\n'
            f'missing_profile={stats["missing_profile"]}\n'
            f'no_ipo_date={stats["no_ipo_date"]}\n'
            f'parse_error={stats["parse_error"]}\n'
            f'http_error={stats["http_error"]}\n'
            f'median_years_backfilled={median_years:.2f}\n'
        )
        print(f'Wrote marker: {MARKER}')

    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        '--top-n', type=int, default=None,
        help='Limit to first N tickers from the universe file (default: all).',
    )
    ap.add_argument(
        '--tickers', type=str, default=None,
        help='Comma-separated tickers (overrides universe file).',
    )
    ap.add_argument(
        '--dry-run', action='store_true', default=False,
        help='Print what would be updated; no DB writes; no marker file.',
    )
    ap.add_argument(
        '--force', action='store_true', default=False,
        help='Re-run even if marker file exists (frozen-artifact convention '
             'says you should ship a v2 sibling file instead).',
    )
    args = ap.parse_args()
    sys.exit(main(args))
