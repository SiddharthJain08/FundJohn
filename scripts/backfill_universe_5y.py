#!/usr/bin/env python3
"""SP-2 Phase B — 5-year universe backfill driver.

Scaffold/harness only (Task 6). Per-target implementations land in:
  - prices   : Task 7  (_run_prices)
  - metadata : Task 8  (_run_metadata)
  - options  : Task 9  (_run_options)

Cross-cutting concerns this module owns:
  - argparse surface (resume/dry-run/tickers/years/source-tag/supersede-quarantine)
  - non-v1 source_tag safety gate (env: OPENCLAW_BACKFILL_ALLOW_OVERWRITE)
  - staging + checkpoint directory ensure-exist (kept under data/, gitignored)
  - Redis client helper (matches src/database/datahub.py pattern: URL + ping)
  - Postgres `backfill_audit` row helpers (start/finish)
  - Discord webhook notifier (best-effort, silent failure)
  - top-level dispatch + PG cleanup

Usage:
  POSTGRES_URI=... python3 scripts/backfill_universe_5y.py \
      --target {prices|metadata|options} [--resume] [--dry-run] \
      [--tickers AAPL,MSFT] [--years 2021,2022] \
      [--source-tag backfill_5y_v1] [--supersede-quarantine]
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
import urllib.request
import urllib.error
from datetime import date as _date
from pathlib import Path
from typing import Iterable, Optional

import psycopg2


# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
# Inject ROOT so `from src.pipeline.backfillers import universe_prices` resolves
# when this script is launched via `python3 scripts/backfill_universe_5y.py`
# (subprocess test + systemd ExecStart contexts both lack PYTHONPATH).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STAGING = ROOT / 'data' / '.staging'
CHECKPOINTS = ROOT / 'data' / '.checkpoints' / 'backfill_5y'
UNIVERSE_FILE = ROOT / 'data' / '.backfill_universe_v1.txt'

# Master parquet path is a module constant so tests can monkeypatch it. We
# follow the existing single-file pattern (alpaca_options.py:_append_parquet,
# run_tier_b.py:305 — read-concat-dedupe-rewrite). The spec (§2.1) called for
# pyarrow.parquet.write_to_dataset partitioned by (year, ticker), but the live
# file is a single 394k-row parquet that every reader treats as a file via
# pd.read_parquet(path). Migrating to a partitioned dataset would change the
# file→directory shape mid-flight and risk silent reader-behavior changes
# across the live pipeline. Spec deviation flagged in DONE report.
MASTER_PRICES = ROOT / 'data' / 'master' / 'prices.parquet'
# Single-file master, NOT a partitioned dataset — matches the master shape
# discovered in Task 7 + the existing alpaca_options._append_parquet writer
# (dedupes on (date, contract_symbol) under a thread lock).
MASTER_OPTIONS = ROOT / 'data' / 'master' / 'options_eod.parquet'


# ── Atomic parquet writer ─────────────────────────────────────────────────────
def _atomic_to_parquet(df, path: Path) -> None:
    """Write *df* to *path* atomically using a sibling tmp file + os.replace.

    A SIGTERM (or any other signal) mid-write leaves either the old file or the
    new file intact — never a partial/truncated parquet.  os.replace() is
    atomic at the filesystem level (POSIX rename(2)) when the source and
    destination are on the same filesystem, which is always true for a sibling
    tmp in the same directory.

    If a stray <path>.tmp-promote exists from a prior killed run it is simply
    overwritten by the new write — no cleanup bookkeeping needed.
    """
    tmp = path.with_suffix('.tmp-promote')
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


# Cache-invalidation hook. The src/data/parquet_store.py untracked file looks
# like the consumer-side cache (see git status). Best-effort import — if not
# wired in yet, we just skip.
def _invalidate_cache(name: str) -> None:
    try:
        from src.data.parquet_store import invalidate as _inv  # type: ignore
        _inv(name)
    except Exception:
        pass

# Source-tag versioning. Anything other than the canonical v1 tag requires an
# explicit env override so the operator can't accidentally overwrite a clean
# v1 promotion with a re-run under the same dirname.
CANONICAL_SOURCE_TAG = 'backfill_5y_v1'


# ── Argparse ──────────────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='backfill_universe_5y',
        description='SP-2 Phase B 5-year universe backfill driver.',
    )
    p.add_argument(
        '--target',
        required=True,
        choices=['prices', 'metadata', 'options'],
        help='Which dataset to backfill.',
    )
    p.add_argument(
        '--resume',
        action='store_true',
        default=False,
        help='Skip chunks already marked done in the Redis checkpoint.',
    )
    p.add_argument(
        '--dry-run',
        action='store_true',
        default=False,
        help='Plan + audit only; do not write parquet or master tables.',
    )
    p.add_argument(
        '--tickers',
        default=None,
        help='Comma-separated ticker override (default: data/.backfill_universe_v1.txt).',
    )
    p.add_argument(
        '--years',
        default=None,
        help='Comma-separated year override (default: last 5 calendar years).',
    )
    p.add_argument(
        '--source-tag',
        default=CANONICAL_SOURCE_TAG,
        help=f'Tag stamped into backfill_audit + parquet metadata (default: {CANONICAL_SOURCE_TAG}).',
    )
    p.add_argument(
        '--supersede-quarantine',
        action='store_true',
        default=False,
        help='If set, validated rows are allowed to overwrite a previously-quarantined chunk.',
    )
    # Options-only date window. Ignored by prices/metadata which use --years.
    p.add_argument(
        '--start-date',
        default=None,
        help='YYYY-MM-DD (--target options only). Defaults to 14 days ago.',
    )
    p.add_argument(
        '--end-date',
        default=None,
        help='YYYY-MM-DD (--target options only). Defaults to yesterday.',
    )
    return p


# ── Safety gate ───────────────────────────────────────────────────────────────
def _check_source_tag_gate(source_tag: str) -> None:
    """Refuse non-v1 source_tag unless operator explicitly opts in via env.

    Exits with rc=2 (config error) so callers / cron can tell this apart from
    a runtime NotImplementedError (rc=1 default) or a missing arg (rc=2 from
    argparse — same code, but the message disambiguates).
    """
    if source_tag == CANONICAL_SOURCE_TAG:
        return
    if os.environ.get('OPENCLAW_BACKFILL_ALLOW_OVERWRITE') == '1':
        return
    sys.stderr.write(
        f'REFUSED: non-v1 source_tag requires OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 '
        f'(got source_tag={source_tag!r}).\n'
    )
    sys.exit(2)


# ── Directory setup ───────────────────────────────────────────────────────────
def _ensure_dirs() -> None:
    """Create staging + checkpoint dirs. data/ stays gitignored."""
    STAGING.mkdir(parents=True, exist_ok=True)
    CHECKPOINTS.mkdir(parents=True, exist_ok=True)


# ── Redis helper ──────────────────────────────────────────────────────────────
def _redis():
    """Return a Python redis client matching src/database/datahub.py pattern.

    There is no src/database/redis.py (only redis.js for the Node side). The
    canonical Python pattern in the codebase is:
        redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)
    followed by .ping(). The env var is REDIS_URL (not REDIS_URI).
    """
    import redis
    url = os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0')
    client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=2)
    client.ping()
    return client


# ── Postgres audit-row helpers ────────────────────────────────────────────────
def _audit_start(pg, target: str, chunk_key: str, source_tag: str) -> int:
    """Insert an in_progress audit row and return its id.

    The (target, chunk_key, source_tag, started_at) UNIQUE constraint means
    parallel runs against the same chunk in the same wallclock instant would
    collide — Redis ops sequencing prevents that in practice. started_at is
    set server-side via NOW() so we don't depend on clock skew.
    """
    with pg.cursor() as cur:
        cur.execute(
            """
            INSERT INTO backfill_audit
                (target, chunk_key, started_at, status, source_tag)
            VALUES (%s, %s, NOW(), 'in_progress', %s)
            RETURNING id
            """,
            (target, chunk_key, source_tag),
        )
        audit_id = cur.fetchone()[0]
    pg.commit()
    return int(audit_id)


def _audit_finish(
    pg,
    audit_id: int,
    status: str,
    rows: int = 0,
    sha: Optional[str] = None,
    err: Optional[str] = None,
) -> None:
    """Mark a previously-started audit row terminal.

    Valid terminal statuses (per migration 115 comments):
        validated, promoted, quarantined, failed
    No validation here — the per-target runners own that contract.
    """
    with pg.cursor() as cur:
        cur.execute(
            """
            UPDATE backfill_audit
               SET status = %s,
                   ended_at = NOW(),
                   rows_written = %s,
                   sha256 = %s,
                   error_text = %s
             WHERE id = %s
            """,
            (status, rows, sha, err, audit_id),
        )
    pg.commit()


# ── Discord notifier ──────────────────────────────────────────────────────────
def _notify_discord(msg: str) -> None:
    """Best-effort POST to DISCORD_BACKFILL_LOG_WEBHOOK. Never raises."""
    url = os.environ.get('DISCORD_BACKFILL_LOG_WEBHOOK')
    if not url:
        return
    try:
        payload = (
            b'{"content":' + __import__('json').dumps(msg)[:1900].encode() + b'}'
        )
        req = urllib.request.Request(
            url,
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        urllib.request.urlopen(req, timeout=5).read()
    except (urllib.error.URLError, OSError, Exception):
        pass  # silent — this is a notifier, not a critical path


# ── Per-target runners ───────────────────────────────────────────────────────
def _load_universe(args: argparse.Namespace) -> list[str]:
    if args.tickers:
        return [t.strip().upper() for t in args.tickers.split(',') if t.strip()]
    if not UNIVERSE_FILE.exists():
        raise SystemExit(f'universe file not found: {UNIVERSE_FILE}')
    return [
        line.strip().upper()
        for line in UNIVERSE_FILE.read_text().splitlines()
        if line.strip() and not line.startswith('#')
    ]


def _load_years(args: argparse.Namespace) -> list[int]:
    if args.years:
        return sorted({int(y.strip()) for y in args.years.split(',') if y.strip()})
    today = _date.today()
    return list(range(2021, today.year + 1))


def _existing_dates_for(master_path: Path, symbol: str, year: int) -> set[str]:
    """Returns the set of `date` strings already present in master for (symbol, year)."""
    if not master_path.exists():
        return set()
    try:
        import pandas as pd
        df = pd.read_parquet(
            master_path,
            columns=['ticker', 'date'],
        )
    except Exception:
        return set()
    if df.empty:
        return set()
    year_start = f'{year}-01-01'
    year_end = f'{year}-12-31'
    mask = (
        (df['ticker'] == symbol)
        & (df['date'] >= year_start)
        & (df['date'] <= year_end)
    )
    return set(df.loc[mask, 'date'].astype(str).tolist())


def _quarantine_chunk(
    pg,
    master_table: str,
    symbol: str,
    year: int,
    source_tag: str,
    reason: str,
) -> None:
    """Insert a single data_quarantine row covering Jan 1 of `year`.

    A whole (symbol, year) chunk's "affected date" can't be a date *range*
    in the current schema (affected_date is a single DATE). We choose
    year-01-01 as the sentinel and stash the year + chunk semantics in
    `reason`. Consumer queries that filter by exact (symbol, date) will
    miss this row — but the explicit purpose of a chunk-level quarantine
    is operator-visible drift detection (doctor + dashboard), not row-level
    filtering, so the (symbol, date) precision isn't required here.

    Note schema remap: data_quarantine uses `symbol` + `affected_date`
    where prices.parquet uses `ticker` + `date`.
    """
    with pg.cursor() as cur:
        cur.execute(
            """
            INSERT INTO data_quarantine
                (master_table, symbol, affected_date, source_tag,
                 reason, flagged_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                master_table,
                symbol,
                f'{year}-01-01',
                source_tag,
                f'chunk {symbol}:{year}: {reason}',
                'auto:backfill_5y_prices',
            ),
        )
    pg.commit()


def _promote_chunk(
    df,
    symbol: str,
    year: int,
    source_tag: str,
    master_path: Path,
) -> int:
    """Read-concat-dedupe-rewrite the master parquet with the new chunk.

    Returns the number of NEW rows written (after dedupe). Uses the existing
    single-file pattern (matches alpaca_options.py:_append_parquet,
    run_tier_b.py:305) rather than the spec's write_to_dataset because the
    live prices.parquet is a single file, not a partitioned dataset; switching
    formats mid-flight would force every downstream reader to re-validate.

    Caller must have already filtered `df` against `_existing_dates_for` so
    `df` contains only rows not present in master.
    """
    import pandas as pd

    if df is None or df.empty:
        return 0

    # Stamp source_tag into the existing 'source' column (currently all <NA>).
    df = df.copy()
    df['source'] = source_tag

    master_path.parent.mkdir(parents=True, exist_ok=True)
    if master_path.exists():
        existing = pd.read_parquet(master_path)
        # Cast new rows' column dtypes to match existing where possible to
        # avoid surprising consumers (volume is int64 in master).
        for col in ('volume',):
            if col in df.columns and col in existing.columns:
                try:
                    df[col] = df[col].astype(existing[col].dtype)
                except Exception:
                    pass
        merged = pd.concat([existing, df], ignore_index=True)
    else:
        merged = df

    # Idempotency guard: dedupe on (ticker, date) — last-write-wins. Because
    # we filtered against _existing_dates_for upstream, the only duplicates
    # this catches are within `df` itself (Alpaca shouldn't return them, but
    # defensive).
    before = len(merged)
    merged = merged.drop_duplicates(subset=['ticker', 'date'], keep='last')
    after_dedupe = len(merged)
    if before != after_dedupe:
        sys.stderr.write(
            f'  [warn] dedupe collapsed {before - after_dedupe} duplicate rows in {symbol}:{year}\n'
        )

    _atomic_to_parquet(merged, master_path)
    return len(df)


def _run_prices(args: argparse.Namespace, pg) -> None:
    """Daily-bar 5y backfill — stage→validate→promote per (ticker, year) chunk.

    Idempotency contract:
      - Redis status='promoted' chunks are skipped under --resume.
      - Redis status='quarantined' chunks are skipped unless --supersede-quarantine.
      - Master parquet writes filter against _existing_dates_for, so re-running
        a promoted chunk after Redis loss is also safe (zero new rows written).

    Overlap policy (per Task 7 plan, more conservative than spec §2.2.1):
      - If staged data overlaps any existing (ticker, date) rows AND source_tag
        is the canonical v1, REFUSE (quarantine the chunk).
      - With a v2+ source_tag AND OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1, overlap
        is allowed (used for quarantine recovery flow per spec §2.3).
    """
    from src.pipeline.backfillers import universe_prices as up

    rclient = _redis()
    universe = _load_universe(args)
    years = _load_years(args)
    source_tag = args.source_tag
    allow_overwrite = (
        os.environ.get('OPENCLAW_BACKFILL_ALLOW_OVERWRITE') == '1'
    )

    print(
        f'[prices] universe={len(universe)} years={years} '
        f'source_tag={source_tag} dry_run={args.dry_run}'
    )

    promoted_total = 0
    quarantined_total = 0
    skipped_total = 0

    for symbol in universe:
        for year in years:
            chunk_key = f'{symbol}:{year}'
            redis_key = f'backfill:5y:prices:{chunk_key}'
            current_status = rclient.get(redis_key)

            if args.resume and current_status == 'promoted':
                skipped_total += 1
                continue
            if (
                current_status == 'quarantined'
                and not args.supersede_quarantine
            ):
                skipped_total += 1
                continue

            audit_id = _audit_start(pg, 'prices', chunk_key, source_tag)
            try:
                df = up.fetch_ticker_year(symbol, year)
            except Exception as e:
                err = f'fetch failed: {e}'
                sys.stderr.write(f'  [error] {chunk_key}: {err}\n')
                _audit_finish(pg, audit_id, status='failed', err=err)
                _notify_discord(f'[backfill prices] FAILED {chunk_key}: {err}')
                continue

            ok, err = up.validate(df, symbol, year)
            if not ok:
                sys.stderr.write(
                    f'  [quarantine] {chunk_key}: {err}\n'
                )
                _quarantine_chunk(
                    pg, 'prices.parquet', symbol, year, source_tag, err or 'unknown',
                )
                _audit_finish(pg, audit_id, status='quarantined', err=err)
                rclient.set(redis_key, 'quarantined')
                _notify_discord(
                    f'[backfill prices] QUARANTINED {chunk_key}: {err}'
                )
                quarantined_total += 1
                continue

            chunk_sha = up.sha256(df)

            if args.dry_run:
                print(f'[dry-run] {chunk_key}: {len(df)} rows valid')
                _audit_finish(
                    pg, audit_id, status='validated',
                    rows=len(df), sha=chunk_sha,
                )
                continue

            # PROMOTE — overlap policy gate.
            existing_dates = _existing_dates_for(MASTER_PRICES, symbol, year)
            df_new = df[~df['date'].isin(existing_dates)]
            had_overlap = (len(df_new) < len(df))

            if had_overlap and source_tag == CANONICAL_SOURCE_TAG and not allow_overwrite:
                reason = (
                    f'overlap with existing {len(df) - len(df_new)} rows '
                    f'under v1 source_tag; refusing to overwrite'
                )
                sys.stderr.write(f'  [quarantine] {chunk_key}: {reason}\n')
                _quarantine_chunk(
                    pg, 'prices.parquet', symbol, year, source_tag, reason,
                )
                _audit_finish(pg, audit_id, status='quarantined', err=reason)
                rclient.set(redis_key, 'quarantined')
                _notify_discord(
                    f'[backfill prices] QUARANTINED {chunk_key}: {reason}'
                )
                quarantined_total += 1
                continue

            if df_new.empty:
                # Already-promoted — Redis-status restore case.
                print(f'  [{chunk_key}] no new rows; marking promoted')
                _audit_finish(
                    pg, audit_id, status='promoted',
                    rows=0, sha=chunk_sha,
                )
                rclient.set(redis_key, 'promoted')
                promoted_total += 1
                continue

            # If overlap was allowed (v2 + env gate), drop the overlapping rows
            # from the existing master AND write df (which includes the
            # corrected rows). Otherwise just append df_new.
            if had_overlap and allow_overwrite:
                import pandas as pd
                existing_full = pd.read_parquet(MASTER_PRICES)
                year_start = f'{year}-01-01'
                year_end = f'{year}-12-31'
                drop_mask = (
                    (existing_full['ticker'] == symbol)
                    & (existing_full['date'] >= year_start)
                    & (existing_full['date'] <= year_end)
                    & (existing_full['date'].isin(df['date']))
                )
                kept = existing_full[~drop_mask]
                df_stamped = df.copy()
                df_stamped['source'] = source_tag
                # Match dtype of volume to existing
                if 'volume' in kept.columns and 'volume' in df_stamped.columns:
                    try:
                        df_stamped['volume'] = df_stamped['volume'].astype(
                            kept['volume'].dtype
                        )
                    except Exception:
                        pass
                merged = pd.concat([kept, df_stamped], ignore_index=True)
                MASTER_PRICES.parent.mkdir(parents=True, exist_ok=True)
                _atomic_to_parquet(merged, MASTER_PRICES)
                rows_written = len(df_stamped)
            else:
                rows_written = _promote_chunk(
                    df_new, symbol, year, source_tag, MASTER_PRICES,
                )

            _audit_finish(
                pg, audit_id, status='promoted',
                rows=rows_written, sha=chunk_sha,
            )
            rclient.set(redis_key, 'promoted')
            _invalidate_cache('prices.parquet')
            print(f'  [{chunk_key}] promoted {rows_written} rows')
            promoted_total += 1

    print(
        f'[prices] DONE promoted={promoted_total} '
        f'quarantined={quarantined_total} skipped={skipped_total}'
    )


def _enumerate_month_ends(years: list[int]) -> list[_date]:
    """Return the last calendar day of each month within `years`, capped at
    today (we never produce a snapshot for a future date).

    Note: "last calendar day", not "last trading day". The downstream resolver
    reads with snapshot_date <= as_of so producing month-ends on Saturday/
    Sunday is harmless (the row will still be the most-recent snapshot for
    Monday morning queries). Using calendar-ends keeps the driver pure (no
    market-calendar dependency) — the spec calls this an acceptable proxy.
    """
    from calendar import monthrange
    today = _date.today()
    out: list[_date] = []
    for y in sorted(set(years)):
        for m in range(1, 13):
            d = _date(y, m, monthrange(y, m)[1])
            if d <= today:
                out.append(d)
    return out


def _validate_metadata(df, snapshot_date: _date) -> tuple[bool, Optional[str]]:
    """Validation gate before promotion. Returns (ok, error_str_or_None).

    Two thresholds are calibrated to Phase B v1 reality:
      1. row_count >= 30 — backfill universe is 404 tickers (v1 floor).
         Earliest snapshots (pre-1990) will only have a few dozen tickers
         that had IPO'd by then, which is still useful for historical
         in_sp500 + sector data.
      2. market_cap top-10 floor only fires when market_cap is populated.
         FMP Starter 403s on historical-market-capitalization, so v1
         historical snapshots have market_cap=None. Allow None-only
         snapshots to land rather than quarantine the entire history.
    """
    if df is None or df.empty:
        return False, 'empty'
    if len(df) < 30:
        return False, f'row_count_too_low ({len(df)})'
    if df['symbol'].duplicated().any():
        return False, 'duplicate_symbol'

    # Market-cap sanity floor only when we have caps to evaluate.
    caps = df['market_cap'].dropna() if 'market_cap' in df.columns else None
    if caps is not None and not caps.empty:
        top10 = caps.nlargest(10)
        if float(top10.max()) < 1e11:
            return False, 'top10_market_cap_implausible'

    return True, None


def _run_metadata(args: argparse.Namespace, pg) -> None:
    """Monthly metadata snapshot backfill — Task 8.

    For each calendar-month-end in `--years` (capped at today) we:
      1. Build a structurally-complete snapshot via build_month_snapshot.
      2. Validate row count / dup-symbol / top-10 mega-cap floor.
      3. dry-run → audit('validated'); otherwise bulk-insert with
         ON CONFLICT DO NOTHING (historical rows are append-only — first
         promotion wins, contra the daily writer which UPSERTs today's row).
      4. Mark Redis status='promoted' so --resume can skip it.

    Note: historical market_cap is None across the board until FMP
    historical-market-capitalization access is restored (2026-05-22 probe:
    403 on Starter tier). _validate_metadata will therefore report
    `top10_market_cap_implausible` for every month at the moment — this is
    the documented degradation and a real signal worth landing in audit.
    """
    from src.pipeline.backfillers.universe_metadata import build_month_snapshot

    rclient = _redis()
    universe = _load_universe(args)
    years = _load_years(args)
    months = _enumerate_month_ends(years)
    source_tag = args.source_tag

    print(
        f'[metadata] universe={len(universe)} years={years} '
        f'months={len(months)} source_tag={source_tag} dry_run={args.dry_run}'
    )

    promoted_total = 0
    quarantined_total = 0
    skipped_total = 0

    for snapshot_date in months:
        chunk_key = f'{snapshot_date.isoformat()}:metadata'
        redis_key = f'backfill:5y:metadata:{chunk_key}'
        current_status = rclient.get(redis_key)

        if args.resume and current_status == 'promoted':
            skipped_total += 1
            continue
        if (
            current_status == 'quarantined'
            and not args.supersede_quarantine
        ):
            skipped_total += 1
            continue

        audit_id = _audit_start(pg, 'metadata', chunk_key, source_tag)
        try:
            from src.pipeline.market_cap_lookup import build_market_cap_lookup
            mcaps = build_market_cap_lookup(list(universe), snapshot_date)
            df = build_month_snapshot(
                snapshot_date, universe, pg, market_cap_lookup=mcaps,
            )
        except Exception as e:
            err = f'build_month_snapshot failed: {e}'
            sys.stderr.write(f'  [error] {chunk_key}: {err}\n')
            _audit_finish(pg, audit_id, status='failed', err=err)
            _notify_discord(f'[backfill metadata] FAILED {chunk_key}: {err}')
            continue

        ok, err = _validate_metadata(df, snapshot_date)
        if not ok:
            sys.stderr.write(f'  [quarantine] {chunk_key}: {err}\n')
            # No master parquet for metadata — the row just doesn't land.
            _audit_finish(pg, audit_id, status='quarantined', err=err)
            rclient.set(redis_key, 'quarantined')
            _notify_discord(
                f'[backfill metadata] QUARANTINED {chunk_key}: {err}'
            )
            quarantined_total += 1
            continue

        if args.dry_run:
            print(f'[dry-run] {chunk_key}: {len(df)} rows valid')
            _audit_finish(pg, audit_id, status='validated', rows=len(df))
            continue

        # Bulk INSERT — DO NOTHING preserves first promotion for historical rows.
        rows_data = [
            (
                r.snapshot_date, r.symbol, r.asset_class, r.exchange, r.status,
                bool(r.tradable), bool(r.shortable), bool(r.fractionable),
                bool(r.easy_to_borrow), r.market_cap, r.adv_usd_20d,
                r.sector, r.industry, bool(r.options_eligible),
                bool(r.in_sp500), bool(r.in_r1000), bool(r.in_r3000),
                r.listed_date, r.delisted_date, source_tag,
            )
            for r in df.itertuples(index=False)
        ]
        try:
            with pg.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO ticker_metadata_snapshots (
                        snapshot_date, symbol, asset_class, exchange, status,
                        tradable, shortable, fractionable, easy_to_borrow,
                        market_cap, adv_usd_20d, sector, industry,
                        options_eligible, in_sp500, in_r1000, in_r3000,
                        listed_date, delisted_date, source_tag
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (snapshot_date, symbol) DO NOTHING
                    """,
                    rows_data,
                )
            pg.commit()
        except Exception as e:
            pg.rollback()
            err = f'insert failed: {e}'
            sys.stderr.write(f'  [error] {chunk_key}: {err}\n')
            _audit_finish(pg, audit_id, status='failed', err=err)
            _notify_discord(f'[backfill metadata] FAILED {chunk_key}: {err}')
            continue

        _audit_finish(pg, audit_id, status='promoted', rows=len(df))
        rclient.set(redis_key, 'promoted')
        print(f'  [{chunk_key}] promoted {len(df)} rows')
        promoted_total += 1

    print(
        f'[metadata] DONE promoted={promoted_total} '
        f'quarantined={quarantined_total} skipped={skipped_total}'
    )


def _options_date_window(args: argparse.Namespace) -> list[_date]:
    """Build the [start, end] inclusive date list for options backfill.

    Defaults (when neither flag set): last 14 days ending yesterday — matches
    the typical cutover-gap operator window. Calendar dates only; weekends
    fall through naturally because the validate() step rejects empty chunks
    (no bars on Sat/Sun → quarantined → moves on).
    """
    from datetime import timedelta
    today = _date.today()
    if args.end_date:
        end = _date.fromisoformat(args.end_date)
    else:
        end = today - timedelta(days=1)
    if args.start_date:
        start = _date.fromisoformat(args.start_date)
    else:
        start = end - timedelta(days=13)  # inclusive 14-day window
    if start > end:
        raise SystemExit(f'--start-date {start} after --end-date {end}')
    out: list[_date] = []
    d = start
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


def _run_options(args: argparse.Namespace, pg) -> None:
    """Options-EOD backfill — per (date, ticker) stage→validate→promote loop.

    Scope: this is the cutover-gap path generalized. Phase B does NOT do a
    full 5y options backfill (deferred to SP-3). The narrow gap is the
    revocation-to-self-archive window for options_eligible tickers, typically
    0-14 days.

    Idempotency contract (matches _run_prices):
      - Redis status='promoted' chunks skipped under --resume.
      - Redis status='quarantined' chunks skipped unless --supersede-quarantine.
      - Promotion uses alpaca_options._append_parquet which itself dedupes on
        (date, contract_symbol), so re-runs after Redis loss are also safe.
    """
    from src.pipeline.backfillers import universe_options as uo
    from src.pipeline.backfillers.alpaca_options import _append_parquet

    rclient = _redis()
    universe = _load_universe(args)
    dates = _options_date_window(args)
    source_tag = args.source_tag

    print(
        f'[options] universe={len(universe)} dates={dates[0]}..{dates[-1]} '
        f'({len(dates)} days) source_tag={source_tag} dry_run={args.dry_run}'
    )

    promoted_total = 0
    quarantined_total = 0
    skipped_total = 0

    for d in dates:
        for symbol in universe:
            chunk_key = f'{d.isoformat()}:{symbol}'
            redis_key = f'backfill:5y:options:{chunk_key}'
            current_status = rclient.get(redis_key)

            if args.resume and current_status == 'promoted':
                skipped_total += 1
                continue
            if (
                current_status == 'quarantined'
                and not args.supersede_quarantine
            ):
                skipped_total += 1
                continue

            audit_id = _audit_start(pg, 'options', chunk_key, source_tag)
            try:
                contracts = uo.enumerate_contracts_for_date(d, [symbol])
                if not contracts:
                    err = 'no contracts enumerated'
                    sys.stderr.write(f'  [quarantine] {chunk_key}: {err}\n')
                    _audit_finish(pg, audit_id, status='quarantined', err=err)
                    rclient.set(redis_key, 'quarantined')
                    quarantined_total += 1
                    continue
                df = uo.fetch_eod_for_contracts(contracts, d)
            except Exception as e:
                err = f'fetch failed: {e}'
                sys.stderr.write(f'  [error] {chunk_key}: {err}\n')
                _audit_finish(pg, audit_id, status='failed', err=err)
                _notify_discord(f'[backfill options] FAILED {chunk_key}: {err}')
                continue

            ok, err = uo.validate(df, d)
            if not ok:
                sys.stderr.write(f'  [quarantine] {chunk_key}: {err}\n')
                _audit_finish(pg, audit_id, status='quarantined', err=err)
                rclient.set(redis_key, 'quarantined')
                _notify_discord(
                    f'[backfill options] QUARANTINED {chunk_key}: {err}'
                )
                quarantined_total += 1
                continue

            if args.dry_run:
                print(f'[dry-run] {chunk_key}: {len(df)} rows valid')
                _audit_finish(
                    pg, audit_id, status='validated', rows=len(df),
                )
                continue

            # PROMOTE — _append_parquet handles dedupe on (date, contract_symbol).
            try:
                _append_parquet(df.to_dict('records'), parquet_path=MASTER_OPTIONS)
            except Exception as e:
                err = f'append failed: {e}'
                sys.stderr.write(f'  [error] {chunk_key}: {err}\n')
                _audit_finish(pg, audit_id, status='failed', err=err)
                _notify_discord(f'[backfill options] FAILED {chunk_key}: {err}')
                continue

            _audit_finish(pg, audit_id, status='promoted', rows=len(df))
            rclient.set(redis_key, 'promoted')
            _invalidate_cache('options_eod.parquet')
            print(f'  [{chunk_key}] promoted {len(df)} rows')
            promoted_total += 1

    print(
        f'[options] DONE promoted={promoted_total} '
        f'quarantined={quarantined_total} skipped={skipped_total}'
    )


_DISPATCH = {
    'prices': _run_prices,
    'metadata': _run_metadata,
    'options': _run_options,
}


# ── Entrypoint ───────────────────────────────────────────────────────────────
def main(argv: Optional[list] = None) -> None:
    args = _build_parser().parse_args(argv)

    # Pure-env safety gate runs before any IO so tests can assert rc=2 without
    # a live Postgres.
    _check_source_tag_gate(args.source_tag)

    _ensure_dirs()

    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    try:
        runner = _DISPATCH[args.target]
        runner(args, pg)
    finally:
        try:
            pg.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
