"""SP-1: Replace Massive S3 flatfiles for daily options EOD archive.

Each Mon-Fri 16:30 ET, iterate every active ticker in alpaca_tradable_universe,
paginate the full Alpaca options chain, flatten into rows, and append to
options_eod.parquet (deduped on (date, contract_symbol)). Per-ticker Redis
checkpoint makes the job idempotent.

Run via systemd timer openclaw-options-archive.timer (Task 8).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as _date
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

ALPACA_BIN = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')
PARQUET_PATH = Path(os.environ.get(
    'OPTIONS_EOD_PARQUET',
    '/root/openclaw/data/master/options_eod.parquet',
))
SOFT_BUDGET_S = int(os.environ.get('OPTIONS_ARCHIVE_BUDGET_S', '1800'))
CONCURRENCY = int(os.environ.get('OPTIONS_ARCHIVE_CONCURRENCY', '8'))
REDIS_TTL_S = 24 * 3600

_PARQUET_LOCK = threading.Lock()


def _redis():
    import redis
    return redis.Redis(
        host=os.environ.get('REDIS_HOST', '127.0.0.1'),
        port=int(os.environ.get('REDIS_PORT', '6379')),
        decode_responses=True,
    )


def _redis_checkpoint_done(ticker: str, date: str) -> bool:
    return bool(_redis().get(f'options_archive:done:{date}:{ticker}'))


def _redis_checkpoint_set(ticker: str, date: str) -> None:
    _redis().set(f'options_archive:done:{date}:{ticker}', '1', ex=REDIS_TTL_S)


def _fetch_chain_page(ticker: str, page_token: str | None = None) -> dict:
    args = [ALPACA_BIN, 'data', 'option', 'chain',
            '--underlying-symbol', ticker, '--limit', '100']
    if page_token:
        args.extend(['--page-token', page_token])
    res = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise RuntimeError(f'alpaca chain rc={res.returncode}: {res.stderr.strip()}')
    return json.loads(res.stdout)


def _decode_occ(symbol: str) -> dict:
    """OCC contract symbol: AAPL260618C00185000 → {root, expiry, type, strike}.

    Handles standard OCC format. Returns None fields for non-conforming symbols
    (e.g., adjusted symbols like GME1260618C..., which we do not currently
    process in downstream consumers — flagged for operator review).
    """
    for i, ch in enumerate(symbol):
        if ch.isdigit():
            root_end = i
            break
    else:
        return {'root': symbol, 'expiry': None, 'type': None, 'strike': None}
    root = symbol[:root_end]
    rest = symbol[root_end:]
    # OCC standard: YYMMDD + C|P + 8-digit-strike-thousandths = exactly 15 chars after root
    if len(rest) != 15 or rest[6] not in ('C', 'P'):
        log.warning('sp1.occ_non_standard symbol=%s — adjusted/non-OCC; returning None fields', symbol)
        return {'root': root, 'expiry': None, 'type': None, 'strike': None}
    yy, mm, dd = rest[0:2], rest[2:4], rest[4:6]
    # Sanity-check the date components
    try:
        from datetime import date as _date_check
        _date_check(2000 + int(yy), int(mm), int(dd))
    except (ValueError, TypeError):
        log.warning('sp1.occ_invalid_date symbol=%s yy=%s mm=%s dd=%s', symbol, yy, mm, dd)
        return {'root': root, 'expiry': None, 'type': None, 'strike': None}
    ctype = 'call' if rest[6] == 'C' else 'put'
    try:
        strike = int(rest[7:]) / 1000.0
    except ValueError:
        return {'root': root, 'expiry': f'20{yy}-{mm}-{dd}', 'type': ctype, 'strike': None}
    return {
        'root': root,
        'expiry': f'20{yy}-{mm}-{dd}',
        'type': ctype,
        'strike': strike,
    }


def _flatten_snapshot(contract_symbol: str, snap: dict, *, date: str, underlying: str) -> dict:
    """Flatten one Alpaca chain snapshot into a master-parquet-compatible row.

    Writes both new (`type`, `underlying`, `iv_implied`) and legacy (`option_type`,
    `ticker`, `implied_volatility`) field names so engine.py's hardcoded column
    references continue to resolve. The Alpaca chain endpoint does not return
    open_interest — that field stays NULL for new rows (master parquet was
    already 99.9% NULL on this column from the Polygon era; no real regression).
    """
    occ = _decode_occ(contract_symbol)
    bar = snap.get('dailyBar') or {}
    quote = snap.get('latestQuote') or {}
    greeks = snap.get('greeks') or {}
    iv = snap.get('impliedVolatility')
    return {
        'date': date,
        'underlying': underlying,
        'ticker': underlying,  # legacy alias for engine.py compat
        'contract_symbol': contract_symbol,
        'strike': occ['strike'],
        'expiry': occ['expiry'],
        'type': occ['type'],
        'option_type': occ['type'],  # legacy alias for engine.py compat
        'open': bar.get('o'),
        'high': bar.get('h'),
        'low': bar.get('l'),
        'close': bar.get('c'),
        'volume': bar.get('v'),
        'vwap': bar.get('vw'),
        'transactions': bar.get('n'),
        'bid': quote.get('bp'),
        'ask': quote.get('ap'),
        'delta': greeks.get('delta'),
        'gamma': greeks.get('gamma'),
        'theta': greeks.get('theta'),
        'vega': greeks.get('vega'),
        'rho': greeks.get('rho'),
        'iv_implied': iv,
        'implied_volatility': iv,  # legacy alias for engine.py compat
        'data_source': 'alpaca_aat_plus',
    }


def _append_parquet(rows: list[dict], *, parquet_path: Path = PARQUET_PATH) -> None:
    """Append-dedupe on (date, contract_symbol). Last write wins on duplicates.

    Thread-safe: serializes read-modify-write via module-level lock so concurrent
    archive_ticker_chain calls from the ThreadPoolExecutor don't clobber each other.
    """
    if not rows:
        return
    with _PARQUET_LOCK:
        new_df = pd.DataFrame(rows)
        if parquet_path.exists():
            old_df = pd.read_parquet(parquet_path)
            merged = pd.concat([old_df, new_df], ignore_index=True)
        else:
            merged = new_df
        merged = merged.drop_duplicates(
            subset=['date', 'contract_symbol'], keep='last',
        )
        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        merged.to_parquet(parquet_path, index=False)


def archive_ticker_chain(ticker: str, *, date: str) -> int:
    """Archive one ticker's full chain. Returns row count written."""
    if _redis_checkpoint_done(ticker, date):
        return 0
    rows: list[dict] = []
    page_token = None
    while True:
        page = _fetch_chain_page(ticker, page_token=page_token)
        snapshots = page.get('snapshots') or {}
        for sym, snap in snapshots.items():
            rows.append(_flatten_snapshot(sym, snap, date=date, underlying=ticker))
        page_token = page.get('next_page_token')
        if not page_token:
            break
    _append_parquet(rows)
    _redis_checkpoint_set(ticker, date)
    return len(rows)


def _load_universe() -> list[str]:
    """Read the canonical S&P 500 universe from universe_config.

    SP-1 keeps the S&P 500 scope — broader alpaca_tradable_universe expansion
    lands in SP-2. universe_config.ticker is the canonical accessor (matches
    src/pipeline/backfillers/fmp.py:_active_universe).
    """
    import psycopg2
    with psycopg2.connect(os.environ['POSTGRES_URI']) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT ticker FROM universe_config WHERE active = TRUE ORDER BY ticker")
        return [r[0] for r in cur.fetchall()]


def main(date_str: str | None = None) -> int:
    date = date_str or _date.today().isoformat()
    universe = _load_universe()
    log.info('options-archive start date=%s tickers=%d', date, len(universe))

    deadline = time.time() + SOFT_BUDGET_S
    written_total = 0
    completed = 0
    failed: list[str] = []

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {ex.submit(archive_ticker_chain, t, date=date): t for t in universe}
        for fut in as_completed(futures):
            t = futures[fut]
            if time.time() > deadline:
                log.warning('soft-budget exceeded; %d tickers completed', completed)
                break
            try:
                n = fut.result()
                written_total += n
                completed += 1
            except Exception as e:
                log.warning('archive failed for %s: %s', t, e)
                failed.append(t)

    log.info('options-archive done date=%s tickers=%d/%d rows=%d failed=%d',
             date, completed, len(universe), written_total, len(failed))
    return 0 if not failed else 1


if __name__ == '__main__':
    import argparse
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    if args.dry_run:
        os.environ['OPTIONS_EOD_PARQUET'] = '/tmp/options_eod_dryrun.parquet'
    raise SystemExit(main(args.date))
