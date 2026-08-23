"""SP-1: Replace Massive S3 flatfiles for daily options EOD archive.

Each Mon-Fri 16:30 ET, iterate the universe_config S&P 500 tickers,
paginate the full Alpaca options chain, flatten into rows, and append to
options_eod.parquet (deduped on (date, contract_symbol)). Per-ticker Redis
checkpoint makes the job idempotent.

Run via systemd timer openclaw-options-archive.timer (Task 8).

Known gap — open_interest is NULL for new rows:
    Alpaca's options chain snapshot + bars endpoints do not expose open
    interest (verified 2026-05-22 deploy probe). FMP Starter options
    endpoints all return "Legacy not available". The master parquet was
    already 99.95% NULL on `open_interest` from the Polygon era (28 of
    59,797 rows populated), so no strategy was relying on it in
    production. engine.py:354 degrades gracefully when OI is absent
    (iv_rank falls back to 50.0 default; no crash). If a future strategy
    needs OI, source it via the bounded yfinance allowlist
    (cboe_vol_indices.py) rather than re-introducing Polygon.
"""
from __future__ import annotations

import json
import logging
import os
import re
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


# SP-2 Phase A: union-universe consumer (Phase A scope = helper exists;
# Phase C wires the production call site).
def _select_archive_universe(as_of, resolver, meta_lookup):
    """Restrict the LIVE union universe to symbols that are options-eligible.

    Caller is expected to invoke this from the daily options-archive loop
    in place of "iterate alpaca_tradable_universe WHERE active". For Phase A
    the existing call site is unchanged — this helper just exists so the
    daily-cycle graph + Task 13.1's resolver CLI can wire it in.
    """
    union = resolver.union_universe(as_of, states=("live",))
    return [s for s in union if meta_lookup(s, as_of).options_eligible]


def _resolver_archive_universe(date_str: str, resolver=None) -> list[str] | None:
    """SP-7 Phase C (C3): gated archive universe = options-eligible ∩ live
    adopted-union, via the Phase-A helper _select_archive_universe.
    Returns None on gate-off or ANY failure → caller falls back to
    _load_universe() (fail-open; the archive must keep accruing).

    Symbol-form note: the archive hits the Alpaca API directly and metadata
    is already stored in Alpaca dot-form (e.g. BRK.B).  We do NOT apply a
    dash-bridge here — deliberate.  Contrast with sentiment/collector, which
    feed dash-form parquet/universe_config consumers and need the bridge.
    """
    if os.environ.get('OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE') != '1':
        return None
    try:
        from datetime import date as _d
        as_of = _d.fromisoformat(date_str)
        if resolver is None:
            from src.execution.live_universe import build_resolver
            resolver = build_resolver()
        # Reuse the resolver's (memoized) adapter for the eligibility lookup.
        meta_rows = resolver._db.fetch_metadata_as_of(as_of)
        meta_map = {r.symbol: r.metadata for r in meta_rows}

        class _NoMeta:           # absent from metadata → not options-eligible
            options_eligible = False

        def meta_lookup(sym, _as_of):
            return meta_map.get(sym, _NoMeta)

        out = _select_archive_universe(as_of, resolver, meta_lookup)
        if not out:
            # options_eligible is all-FALSE until the chain-probe producer ships
            # (Phase D backlog) — make the dead-gate state visible, then fall back.
            log.warning('archive resolver-universe: gate ON but 0 options-eligible '
                        'names in metadata — falling back to universe_config')
            return None
        return out
    except Exception as e:  # noqa: BLE001 — fail-open to universe_config
        log.warning('archive resolver-universe failed (fail-open): %s', e)
        return None


def _redis_checkpoint_done(ticker: str, date: str) -> bool:
    return bool(_redis().get(f'options_archive:done:{date}:{ticker}'))


def _redis_checkpoint_set(ticker: str, date: str) -> None:
    _redis().set(f'options_archive:done:{date}:{ticker}', '1', ex=REDIS_TTL_S)


def _fetch_chain_page(ticker: str, page_token: str | None = None) -> dict:
    args = [ALPACA_BIN, 'data', 'option', 'chain',
            '--underlying-symbol', ticker, '--limit', '100']
    if page_token:
        args.extend(['--page-token', page_token])
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except Exception as e:
        _record_call('alpaca', 'options_chain', success=False, error=f'subprocess: {e}')
        raise
    if res.returncode != 0:
        err = res.stderr.strip()
        _record_call('alpaca', 'options_chain', success=False,
                     error=f'rc={res.returncode}: {err[:160]}')
        raise RuntimeError(f'alpaca chain rc={res.returncode}: {err}')
    _record_call('alpaca', 'options_chain', success=True)
    return json.loads(res.stdout)


# Lazy import so test contexts that don't have psycopg2 still work.
def _record_call(provider, endpoint, *, success, error=None):
    try:
        from src.maintenance.provider_health import record
        record(provider, endpoint, success=success, error=error)
    except Exception:
        pass


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


def _master_readable(parquet_path: Path | None = None) -> bool:
    """Cheap footer/metadata validation before a run touches anything.

    A corrupt master (the 2026-06-29 mid-write SIGTERM truncation) previously
    surfaced as ~5k per-ticker read failures over ~12 CPU-minutes every run;
    abort up-front with one actionable error instead.
    """
    parquet_path = parquet_path or PARQUET_PATH
    if not parquet_path.exists():
        return True
    try:
        import pyarrow.parquet as pq
        pq.ParquetFile(parquet_path)
        return True
    except Exception as e:  # noqa: BLE001 — any unreadable state must abort
        log.error('master parquet unreadable (%s) — aborting run; repair/restore '
                  '%s before the next archive', e, parquet_path)
        return False


def _merge_write(new_df: pd.DataFrame, *, parquet_path: Path = PARQUET_PATH) -> None:
    """Merge-dedupe new_df into the master ATOMICALLY (new row wins on
    (date, contract_symbol) conflict).

    Delegates to parquet_store.append_dedup: DuckDB streams the existing
    master from disk (memory-bounded, spills) and writes tmp+os.replace.
    Only the incoming batch lives in pandas — the previous implementation
    loaded the whole 6M-row master (pd.read_parquet + concat) and was
    OOM-killed daily at 3.6-6.2G peaks on the 8GB no-swap box.
    """
    try:
        from src.data.parquet_store import append_dedup
    except ModuleNotFoundError:
        # The systemd unit execs this file directly (no PYTHONPATH); the
        # repo root is three levels up from src/pipeline/backfillers/.
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
        from src.data.parquet_store import append_dedup
    with _PARQUET_LOCK:
        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        append_dedup(parquet_path, new_df,
                     key_cols=['date', 'contract_symbol'], mode='replace')


def _append_parquet(rows: list[dict], *, parquet_path: Path = PARQUET_PATH) -> None:
    """Append-dedupe on (date, contract_symbol). Last write wins on duplicates.

    Thread-safe: serializes read-modify-write via module-level lock so
    concurrent callers don't clobber each other. External callers
    (backfill_universe_5y promotion) keep this immediate-write semantic;
    main() spools instead — one merge per run, not one per ticker.
    """
    if not rows:
        return
    _merge_write(pd.DataFrame(rows), parquet_path=parquet_path)


def archive_ticker_chain(ticker: str, *, date: str, sink=None,
                         defer_checkpoint: bool = False) -> int:
    """Archive one ticker's full chain. Returns row count written.

    sink: rows consumer; defaults to the immediate atomic merge-write.
    main() passes a spool so the whole run does ONE master rewrite.
    defer_checkpoint: caller owns the Redis checkpoint — set it only after
    the spooled rows actually land on disk, else a crash between fetch and
    flush would mark the day done while its rows evaporate with the process.
    """
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
    (sink or _append_parquet)(rows)
    if not defer_checkpoint:
        _redis_checkpoint_set(ticker, date)
    return len(rows)


def _coverage_rows(counts: dict, date: str) -> list[tuple]:
    """(ticker, date, rows) for every ticker that actually landed rows.
    Mirrors store.updateCoverage: a zero-row fetch never advances coverage."""
    return [(t, date, int(n)) for t, n in counts.items() if n and int(n) > 0]


def _write_coverage(rows: list[tuple]) -> int:
    """Upsert data_coverage(options) for the archived tickers — the same SQL
    store.updateCoverage uses, so doctor's staleness probe and the data-tier
    filter see the archive's work. D3 (2026-08-23): the in-cycle collector
    phase used to be the only coverage writer for options while the archive
    was the only writer that ever landed on disk."""
    if not rows:
        return 0
    import psycopg2
    sql = """
        INSERT INTO data_coverage (ticker, data_type, date_from, date_to, rows_stored, last_updated)
        VALUES (%s, 'options', %s::date, %s::date, %s, NOW())
        ON CONFLICT (ticker, data_type) DO UPDATE SET
          date_from    = LEAST(EXCLUDED.date_from, data_coverage.date_from),
          date_to      = GREATEST(EXCLUDED.date_to, data_coverage.date_to),
          rows_stored  = data_coverage.rows_stored + EXCLUDED.rows_stored,
          last_updated = NOW()
    """
    with psycopg2.connect(os.environ['POSTGRES_URI']) as conn, conn.cursor() as cur:
        cur.executemany(sql, [(t, d, d, n) for t, d, n in rows])
        conn.commit()
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
    if not _master_readable(PARQUET_PATH):
        return 1
    universe = _resolver_archive_universe(date) or _load_universe()
    # Yahoo-style non-equity symbols (BTC-USD, EURUSD=X, GC=F, ^VIX, BRK-B
    # preferred/class shares) can never have Alpaca option chains — the
    # snapshot endpoint 400s "invalid underlying symbol" on every one, every
    # day, which made the unit exit 1 daily on 54 permanent no-ops and page
    # the failure notifier (2026-07-21: 5019/5073 archived fine, rc=1 anyway).
    # Valid Alpaca underlyings are plain [A-Z0-9.] — filter the rest up front
    # so a nonzero exit means a REAL failure again.
    skipped_shape = [t for t in universe if not re.fullmatch(r'[A-Z][A-Z0-9.]*', t)]
    if skipped_shape:
        universe = [t for t in universe if re.fullmatch(r'[A-Z][A-Z0-9.]*', t)]
        log.info('options-archive: skipping %d never-optionable symbols '
                 '(crypto/FX/futures/index/Yahoo-class shapes)', len(skipped_shape))
    log.info('options-archive start date=%s tickers=%d', date, len(universe))

    deadline = time.time() + SOFT_BUDGET_S
    written_total = 0
    completed = 0
    failed: list[str] = []
    done_ticks: list[str] = []

    # Spool per-ticker rows in memory; ONE merge + atomic write at the end.
    # The old per-ticker sink re-read + rewrote the whole master once per
    # ticker — O(N²) I/O that blew the unit timeout at the SP-7 wide universe
    # (5k names) and got SIGTERM'd mid-write (2026-06-29 corruption).
    spooled: list[pd.DataFrame] = []
    spool_lock = threading.Lock()
    counts: dict[str, int] = {}

    def _spool(rows: list[dict], **_kw) -> None:
        if not rows:
            return
        with spool_lock:
            spooled.append(pd.DataFrame(rows))

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {ex.submit(archive_ticker_chain, t, date=date,
                             sink=_spool, defer_checkpoint=True): t
                   for t in universe}
        for fut in as_completed(futures):
            t = futures[fut]
            if time.time() > deadline:
                log.warning('soft-budget exceeded; %d tickers completed', completed)
                break
            try:
                n = fut.result()
                written_total += n
                completed += 1
                done_ticks.append(t)
                counts[t] = n
            except Exception as e:
                log.warning('archive failed for %s: %s', t, e)
                failed.append(t)

    try:
        if spooled:
            _merge_write(pd.concat(spooled, ignore_index=True))
        for t in done_ticks:
            _redis_checkpoint_set(t, date)
    except Exception as e:  # noqa: BLE001 — flush failure must not checkpoint
        log.error('final merge-write failed — nothing checkpointed, the day '
                  're-fetches on the next run: %s', e)
        return 1

    # Coverage is observability, not data: never let a Postgres hiccup fail
    # a run whose rows are already on disk and checkpointed.
    try:
        n_cov = _write_coverage(_coverage_rows(counts, date))
        log.info('options-archive data_coverage upserted for %d ticker(s)', n_cov)
    except Exception as e:  # noqa: BLE001
        log.warning('options-archive data_coverage write failed (non-fatal): %s', e)

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
