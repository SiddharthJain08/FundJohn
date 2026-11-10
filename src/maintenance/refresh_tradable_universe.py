#!/usr/bin/env python3
"""refresh_tradable_universe.py

Pulls Alpaca's master `us_equity` asset list (plus, since crypto engine
Phase 3, the active `crypto` pairs) and upserts it into
`alpaca_tradable_universe` so the trade pipeline can filter out
delisted / halted / non-tradable tickers BEFORE submission.

Crypto rows are stored under the INTERNAL symbol ('BTC/USD' -> 'BTC-USD',
via ingestion.crypto_flow.universe.normalize_broker_symbol) with
asset_class='crypto'; pairs outside the internal crypto universe (non-USD
quotes, unknown bases) are skipped and counted. Consumers read the table
through execution.tradable_universe.load_tradable_symbols(classes=...) —
default us_equity only.

Designed to run on a daily timer (13:30 UTC, ~30 min before the 14:00
UTC pipeline cycle) and also as a BotJohn maintenance step.

Behaviour:
  - Fetches every active us_equity asset (~13.6k symbols at time of
    writing) via `alpaca asset list --asset-class us_equity --status active`.
  - Upserts each row, advancing last_seen_at and refreshing the
    tradable/shortable/marginable flags.
  - Any symbol previously stored but absent from today's pull is marked
    status='inactive', tradable=false (NEVER deleted — master-data rule).
    Scoped to the asset classes PRESENT in this run: a failed / empty
    crypto pull never deactivates crypto rows, and vice versa.
  - Writes a row to `alpaca_tradable_universe_refresh_log` with diff counts.
  - Prints a one-line summary + non-zero exit code on failure so a
    systemd timer / pipeline ExecStartPre will surface the failure.

Usage:
  python3 -m maintenance.refresh_tradable_universe [--dry-run] [--verbose]

Exit codes:
  0  refresh succeeded
  1  Alpaca CLI failed / DB write failed
  2  CLI returned 0 assets (suspicious; aborts without writing)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

ALPACA_BIN = os.environ.get('ALPACA_BIN', '/root/go/bin/alpaca')
SUSPICIOUS_FLOOR = int(os.environ.get('UNIVERSE_MIN_ROWS', '5000'))


def _log(msg: str) -> None:
    print(f'[refresh_tradable_universe] {msg}', flush=True)


def fetch_alpaca_assets(timeout: int = 60,
                        asset_class: str = 'us_equity') -> tuple[list[dict], int]:
    """Return (assets, elapsed_ms). Raises subprocess.CalledProcessError
    on CLI failure, ValueError if stdout isn't valid JSON list."""
    started = time.monotonic()
    proc = subprocess.run(
        [ALPACA_BIN, 'asset', 'list',
         '--asset-class', asset_class, '--status', 'active'],
        capture_output=True, text=True, timeout=timeout,
        env=os.environ.copy(),
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, proc.args,
            output=proc.stdout, stderr=proc.stderr,
        )
    data = json.loads(proc.stdout)
    if not isinstance(data, list):
        # CLI sometimes returns an error dict — surface it.
        if isinstance(data, dict) and 'error' in data:
            raise RuntimeError(f"alpaca CLI error: {data['error']}")
        raise ValueError(f'unexpected payload type: {type(data).__name__}')
    return data, elapsed_ms


def fetch_all_assets(timeout: int = 60, *,
                     crypto_universe=None) -> tuple[list[dict], int, dict]:
    """Equities + crypto pairs. Returns (assets, elapsed_ms, counts).

    The us_equity fetch raises exactly like fetch_alpaca_assets. The crypto
    fetch is best-effort: on failure the run carries equities only
    (counts['crypto_error'] set) — the class-scoped deactivation in
    upsert_universe then leaves the stored crypto rows untouched.
    Crypto symbols are rewritten to the INTERNAL form ('BTC-USD') and
    'class' forced to 'crypto'; ``crypto_universe`` (list of crypto_flow
    Assets) is injectable for tests."""
    equities, eq_ms = fetch_alpaca_assets(timeout=timeout, asset_class='us_equity')
    counts = {'us_equity': len(equities), 'crypto_fetched': 0, 'crypto': 0,
              'crypto_skipped_non_usd': 0, 'crypto_unmapped_usd': 0,
              'crypto_unmapped_symbols': [], 'crypto_error': None}
    crypto_rows: list[dict] = []
    cr_ms = 0
    try:
        from ingestion.crypto_flow.universe import normalize_broker_symbol
        raw, cr_ms = fetch_alpaca_assets(timeout=timeout, asset_class='crypto')
        counts['crypto_fetched'] = len(raw)
        for a in raw:
            sym = a.get('symbol') or ''
            internal = normalize_broker_symbol(sym, crypto_universe)
            if not internal:
                if _quote_currency(sym) != 'USD':
                    counts['crypto_skipped_non_usd'] += 1       # BTC/USDT, ETH/BTC …
                else:
                    # a /USD pair the internal crypto universe does not map —
                    # append it via scripts/refresh_crypto_flow_universe.py
                    counts['crypto_unmapped_usd'] += 1
                    counts['crypto_unmapped_symbols'].append(sym)
                continue
            crypto_rows.append({**a, 'symbol': internal, 'class': 'crypto'})
    except Exception as e:  # noqa: BLE001 — crypto is additive; equities must still refresh
        counts['crypto_error'] = f'{type(e).__name__}: {e}'
        crypto_rows = []
    counts['crypto'] = len(crypto_rows)
    return equities + crypto_rows, eq_ms + cr_ms, counts


def _quote_currency(sym: str) -> str:
    """'BTC/USDT' -> 'USDT'; 'BTC/USD' / 'BTCUSD' -> 'USD'; else ''."""
    sym = (sym or '').upper()
    if '/' in sym:
        return sym.rsplit('/', 1)[1]
    return 'USD' if sym.endswith('USD') else ''


def crypto_notes(counts: dict) -> str:
    """refresh_log.notes payload: crypto fetched/upserted/skip split + error,
    so a daily-failing crypto pull is visible to the system_checks digest."""
    parts = [f"crypto fetched={counts.get('crypto_fetched', 0)}",
             f"upserted={counts.get('crypto', 0)}",
             f"skipped_non_usd={counts.get('crypto_skipped_non_usd', 0)}",
             f"unmapped_usd={counts.get('crypto_unmapped_usd', 0)}"]
    if counts.get('crypto_unmapped_symbols'):
        parts.append('unmapped=' + ','.join(counts['crypto_unmapped_symbols'][:20]))
    if counts.get('crypto_error'):
        parts.append(f"crypto_error={counts['crypto_error']}"[:500])
    return ' '.join(parts)


def _asset_class(value) -> str:
    """NULL / missing class ≡ us_equity (every pre-crypto row)."""
    return value or 'us_equity'


def upsert_universe(conn, assets: Iterable[dict], dry_run: bool = False,
                    notes: str | None = None) -> dict:
    """Upsert today's pull. Returns diff stats dict.

    Stats:
      total_active    — rows in today's pull
      total_tradable  — rows with tradable=true
      newly_listed    — symbols inserted for the first time
      newly_inactive  — symbols previously seen but absent today
      status_changes  — symbols whose tradable/status flipped vs prior
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # Snapshot of prior state (before this run) to compute diffs.
    cur.execute("SELECT symbol, tradable, status, asset_class FROM alpaca_tradable_universe")
    prior = {}
    prior_class = {}
    for r in cur:
        prior[r['symbol']] = (r['tradable'], r['status'])
        prior_class[r['symbol']] = _asset_class(r.get('asset_class'))

    today_symbols: set[str] = set()
    run_classes: set[str] = set()
    rows = []
    for a in assets:
        sym = a.get('symbol')
        if not sym:
            continue
        today_symbols.add(sym)
        run_classes.add(_asset_class(a.get('class')))
        rows.append((
            sym,
            a.get('id'),
            a.get('name'),
            _asset_class(a.get('class')),
            a.get('exchange'),
            'active',
            bool(a.get('tradable', False)),
            bool(a.get('shortable', False)),
            bool(a.get('marginable', False)),
            bool(a.get('fractionable', False)),
            bool(a.get('easy_to_borrow', False)),
        ))

    total_active   = len(today_symbols)
    total_tradable = sum(1 for a in assets if a.get('tradable'))

    # Diff stats
    newly_listed   = sum(1 for s in today_symbols if s not in prior)
    today_state    = {a.get('symbol'): (bool(a.get('tradable', False)), 'active') for a in assets}
    status_changes = sum(
        1 for s in today_symbols
        if s in prior and prior[s] != today_state[s]
    )
    # Symbols previously seen with status='active' or tradable=true but
    # absent from today's pull → newly inactive. Only classes present in
    # this run can deactivate (a crypto-only run never touches equities).
    newly_inactive = sum(
        1 for s, (tr, st) in prior.items()
        if s not in today_symbols and (st == 'active' or tr)
        and prior_class[s] in run_classes
    )

    if dry_run:
        return {
            'total_active':   total_active,
            'total_tradable': total_tradable,
            'newly_listed':   newly_listed,
            'newly_inactive': newly_inactive,
            'status_changes': status_changes,
            'dry_run':        True,
        }

    # Bulk upsert today's pull.
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO alpaca_tradable_universe
          (symbol, alpaca_asset_id, name, asset_class, exchange,
           status, tradable, shortable, marginable, fractionable, easy_to_borrow,
           last_seen_at, updated_at)
        VALUES %s
        ON CONFLICT (symbol) DO UPDATE SET
          alpaca_asset_id = EXCLUDED.alpaca_asset_id,
          name            = EXCLUDED.name,
          asset_class     = EXCLUDED.asset_class,
          exchange        = EXCLUDED.exchange,
          status          = EXCLUDED.status,
          tradable        = EXCLUDED.tradable,
          shortable       = EXCLUDED.shortable,
          marginable      = EXCLUDED.marginable,
          fractionable    = EXCLUDED.fractionable,
          easy_to_borrow  = EXCLUDED.easy_to_borrow,
          last_seen_at    = NOW(),
          updated_at      = NOW()
        """,
        rows,
        template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())",
        page_size=1000,
    )

    # Mark symbols that dropped out of today's pull as inactive — but
    # NEVER delete them (master-data invariant). last_seen_at stays
    # frozen on its prior value so we can audit when each dropped.
    if today_symbols:
        cur.execute(
            """
            UPDATE alpaca_tradable_universe
            SET status     = 'inactive',
                tradable   = FALSE,
                shortable  = FALSE,
                updated_at = NOW()
            WHERE symbol <> ALL(%s::text[])
              AND (status = 'active' OR tradable = TRUE)
              AND COALESCE(asset_class, 'us_equity') = ANY(%s)
            """,
            (list(today_symbols), sorted(run_classes)),
        )

    # Append audit row.
    cur.execute(
        """
        INSERT INTO alpaca_tradable_universe_refresh_log
          (total_active, total_tradable, newly_listed, newly_inactive,
           status_changes, alpaca_api_ms, notes)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        """,
        (total_active, total_tradable, newly_listed, newly_inactive,
         status_changes, None, notes),
    )
    conn.commit()

    return {
        'total_active':   total_active,
        'total_tradable': total_tradable,
        'newly_listed':   newly_listed,
        'newly_inactive': newly_inactive,
        'status_changes': status_changes,
        'dry_run':        False,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='Fetch + diff but skip DB writes')
    ap.add_argument('--verbose', action='store_true',
                    help='Print first 10 newly-inactive and newly-listed symbols')
    args = ap.parse_args()

    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        _log('POSTGRES_URI not set; aborting')
        return 1

    try:
        assets, api_ms, counts = fetch_all_assets()
    except Exception as e:
        _log(f'alpaca asset list failed: {type(e).__name__}: {e}')
        return 1

    if counts['us_equity'] < SUSPICIOUS_FLOOR:
        _log(f'CLI returned {counts["us_equity"]} us_equity rows (< floor {SUSPICIOUS_FLOOR}); '
             f'refusing to write — likely an auth or upstream issue')
        return 2

    _log(f'fetched {counts["us_equity"]} us_equity + {counts["crypto"]} crypto assets '
         f'(crypto fetched={counts["crypto_fetched"]}) in {api_ms}ms')
    if counts['crypto_skipped_non_usd']:
        _log(f'crypto: skipped {counts["crypto_skipped_non_usd"]} non-USD-quote pairs (expected)')
    if counts['crypto_unmapped_usd']:
        _log(f'WARN crypto: {counts["crypto_unmapped_usd"]} /USD pairs absent from '
             f'crypto_flow_universe.json, NOT stored: '
             f'{", ".join(counts["crypto_unmapped_symbols"][:20])} — run '
             f'scripts/refresh_crypto_flow_universe.py')
    if counts['crypto_error']:
        _log(f'WARN crypto asset list failed ({counts["crypto_error"]}) — equities only; '
             f'stored crypto rows left untouched (recorded in refresh_log.notes)')

    conn = psycopg2.connect(uri)
    try:
        stats = upsert_universe(conn, assets, dry_run=args.dry_run,
                                notes=crypto_notes(counts))
    except Exception as e:
        conn.rollback()
        _log(f'upsert failed: {type(e).__name__}: {e}')
        return 1
    finally:
        conn.close()

    flag = ' (DRY-RUN)' if stats['dry_run'] else ''
    _log(f'done{flag} — active={stats["total_active"]} '
         f'tradable={stats["total_tradable"]} '
         f'newly_listed={stats["newly_listed"]} '
         f'newly_inactive={stats["newly_inactive"]} '
         f'status_changes={stats["status_changes"]}')

    if args.verbose and (stats['newly_listed'] or stats['newly_inactive']):
        # Re-fetch the diff for human visibility. Cheap on a 13k-row table.
        conn2 = psycopg2.connect(uri)
        cur = conn2.cursor()
        cur.execute(
            """SELECT symbol FROM alpaca_tradable_universe
               WHERE status = 'inactive' AND updated_at > NOW() - INTERVAL '1 hour'
               ORDER BY symbol LIMIT 10"""
        )
        recent_inactive = [r[0] for r in cur]
        if recent_inactive:
            _log(f'  recent inactive (first 10): {", ".join(recent_inactive)}')
        cur.execute(
            """SELECT symbol FROM alpaca_tradable_universe
               WHERE first_seen_at > NOW() - INTERVAL '1 hour'
               ORDER BY symbol LIMIT 10"""
        )
        recent_listed = [r[0] for r in cur]
        if recent_listed:
            _log(f'  newly listed (first 10): {", ".join(recent_listed)}')
        conn2.close()

    # SP-2 Phase A: Chain ticker_metadata_writer after a successful refresh
    # so the daily snapshot stays fresh. Non-fatal: doctor will detect
    # staleness if this fails.
    import subprocess
    subprocess.run(
        ["python3", "-m", "src.pipeline.run_ticker_metadata_step"],
        check=False,
    )

    return 0


if __name__ == '__main__':
    sys.exit(main())
