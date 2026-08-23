#!/usr/bin/env python3
"""Producer for data/.cache/fmp_profile.json (sector / industry / ipoDate).

Why this exists (2026-08-23)
----------------------------
src/pipeline/run_ticker_metadata_step.py has read
``data/.cache/fmp_profile.json`` since SP-2 Phase A and
ticker_metadata_writer.py maps ``sector`` / ``industry`` / ``mktCap`` /
``ipoDate`` out of it — but NOTHING ever wrote the file (grep fmp_profile:
consumer + writer only). Result: every ticker_metadata_snapshots row has
sector/industry NULL (14,254 rows on 2026-08-21, 0 with a sector).

What it does
------------
* Universe = active, tradable ``us_equity`` rows of ``alpaca_tradable_universe``
  (~13.4k on 2026-08-23) — the exact symbol set the consumer iterates.
  ``--tickers`` / ``--limit`` narrow it for smoke tests.
* Fetches FMP ``/stable/profile?symbol=X`` ONE symbol per call (the batch form
  ``symbol=A,B`` returns ``[]`` on this key — probed 2026-08-23). Legacy
  ``/api/v3/profile`` is 403 "Legacy Endpoint".
* Refreshes only names MISSING from the cache or older than ``--max-age-days``
  (30): missing first, then stalest. Symbols FMP has no profile for get a
  tombstone ``{"_fetched_at": ts, "_empty": true}`` so they are not re-hit
  every week (they still read as sector=None downstream).
* Pacing ``--sleep`` 0.2 s (FMP Starter 300 req/min); 429/5xx back off
  (1/5/30 s); 401/403 abort the run loudly (key problem, not a data gap).
* Atomic writes (tmp + os.replace), flushed every ``--flush-every`` symbols
  and at the end, so a TimeoutStartSec kill keeps everything fetched so far.
* Prints counters — read THEM, not the exit code.

Sizing: full first run 13.4k × ~0.35 s ≈ 78 min; steady-state weekly runs
touch only new listings plus the >30 d cohort. Scheduled by
openclaw-fmp-profiles.timer (Sat 00:30 UTC, TimeoutStartSec 9000).

Usage
-----
    python3 scripts/refresh_fmp_profiles.py                 # weekly unit
    python3 scripts/refresh_fmp_profiles.py --limit 20      # smoke test
    python3 scripts/refresh_fmp_profiles.py --tickers AAPL,MSFT --force
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CACHE_PATH = ROOT / 'data' / '.cache' / 'fmp_profile.json'
FMP_PROFILE_URL = 'https://financialmodelingprep.com/stable/profile'
HTTP_TIMEOUT_S = 15.0
RETRY_BACKOFFS_S: tuple = (1.0, 5.0, 30.0)
DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_SLEEP_S = 0.2

# Fields ticker_metadata_writer reads (sector, industry, mktCap, ipoDate) plus
# a small identity/classification set useful to other consumers. The raw
# profile carries a multi-KB description + image URL per symbol — dropped so
# the 13k-name cache stays a few MB.
KEEP_FIELDS = (
    'symbol', 'companyName', 'sector', 'industry', 'marketCap', 'ipoDate',
    'exchange', 'exchangeFullName', 'cik', 'isin', 'cusip', 'country',
    'currency', 'isEtf', 'isFund', 'isAdr', 'isActivelyTrading',
)


class FMPAuthError(RuntimeError):
    """401/403 from FMP — the KEY is wrong for this endpoint; stop the run."""


# ── pure helpers ─────────────────────────────────────────────────────────────

def _parse_ts(s) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def needs_refresh(entry: Optional[dict], now: datetime, max_age_days: int) -> bool:
    """Missing, unstamped (legacy), or older than max_age_days -> refetch.
    Tombstones (``_empty``) age out the same way."""
    if not isinstance(entry, dict):
        return True
    ts = _parse_ts(entry.get('_fetched_at'))
    if ts is None:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts) > timedelta(days=max_age_days)


def selection_counters(universe: list[str], cache: dict, now: datetime,
                       max_age_days: int, limit: Optional[int]) -> dict:
    """Honest counters: fresh (skipped) vs stale, and of the stale how many
    this run fetches vs defers to the next run because of --limit."""
    stale = select_symbols(universe, cache, now, max_age_days, None)
    todo = select_symbols(universe, cache, now, max_age_days, limit)
    return {'universe': len(universe), 'stale': len(stale), 'to_fetch': len(todo),
            'skipped_fresh': len(universe) - len(stale),
            'deferred_by_limit': len(stale) - len(todo)}


def select_symbols(universe: list[str], cache: dict, now: datetime,
                   max_age_days: int, limit: Optional[int]) -> list[str]:
    """Symbols to fetch this run: missing first (alphabetical), then the
    stalest by _fetched_at; fresh entries skipped; optional cap."""
    due = [s for s in universe if needs_refresh(cache.get(s), now, max_age_days)]

    def _key(s: str):
        e = cache.get(s)
        stamped = isinstance(e, dict) and _parse_ts(e.get('_fetched_at')) is not None
        return (stamped, e.get('_fetched_at', '') if stamped else '', s)

    due.sort(key=_key)
    if limit is not None and limit >= 0:
        due = due[:limit]
    return due


def normalize_profile(raw: Optional[dict], now: datetime) -> dict:
    """Trim a /stable/profile row to KEEP_FIELDS, alias marketCap -> mktCap
    (the writer's field name from the v3 era), stamp _fetched_at.
    None/empty -> tombstone."""
    stamp = now.isoformat()
    if not raw:
        return {'_fetched_at': stamp, '_empty': True}
    out = {k: raw.get(k) for k in KEEP_FIELDS if k in raw}
    out['mktCap'] = raw.get('marketCap')
    out['_fetched_at'] = stamp
    return out


def atomic_write_json(path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.tmp.{os.getpid()}')
    tmp.write_text(json.dumps(data, sort_keys=True, separators=(',', ':')))
    os.replace(tmp, path)


def load_cache(path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _http_get(url, params=None, timeout=None):
    import requests
    return requests.get(url, params=params, timeout=timeout)


def fetch_profile(symbol: str, api_key: str) -> Optional[dict]:
    """One /stable/profile call. Returns the first row, None when FMP has no
    profile, raises FMPAuthError on 401/403; 429/5xx retried with backoff."""
    backoffs = list(RETRY_BACKOFFS_S) + [None]
    for delay in backoffs:
        r = _http_get(FMP_PROFILE_URL, params={'symbol': symbol, 'apikey': api_key},
                      timeout=HTTP_TIMEOUT_S)
        code = getattr(r, 'status_code', 0)
        if code == 200:
            data = r.json()
            if isinstance(data, list):
                return data[0] if data else None
            return data or None
        if code in (401, 403):
            raise FMPAuthError(f'FMP {code} on /stable/profile for {symbol} — key/plan problem')
        if code == 402:
            raise FMPAuthError(f'FMP 402 on /stable/profile for {symbol} — plan limit hit')
        if code == 404:
            return None
        if delay is None:
            raise RuntimeError(f'FMP HTTP {code} for {symbol} after retries')
        time.sleep(delay)
    return None  # unreachable


# ── universe ─────────────────────────────────────────────────────────────────

def _universe(args) -> list[str]:
    if args.tickers:
        return [t.strip().upper() for t in args.tickers.split(',') if t.strip()]
    dsn = os.environ.get('POSTGRES_URI', '')
    if not dsn:
        raise SystemExit('POSTGRES_URI not set (needed for the Alpaca universe)')
    from src.pipeline.backfillers.edgar_shares import alpaca_active_universe
    return alpaca_active_universe(dsn)


# ── main ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='refresh_fmp_profiles')
    ap.add_argument('--tickers', default=None, help='comma list (default: Alpaca active universe)')
    ap.add_argument('--limit', type=int, default=None, help='max symbols to FETCH this run')
    ap.add_argument('--max-age-days', type=int, default=DEFAULT_MAX_AGE_DAYS)
    ap.add_argument('--force', action='store_true', help='refetch even if fresh')
    ap.add_argument('--sleep', type=float, default=DEFAULT_SLEEP_S)
    ap.add_argument('--flush-every', type=int, default=500)
    ap.add_argument('--cache', default=str(CACHE_PATH))
    ap.add_argument('--dry-run', action='store_true', help='select only; no HTTP, no write')
    args = ap.parse_args(argv)

    api_key = os.environ.get('FMP_API_KEY', '')
    if not api_key and not args.dry_run:
        raise SystemExit('FMP_API_KEY not set')

    now = datetime.now(timezone.utc)
    cache_path = Path(args.cache)
    cache = load_cache(cache_path)
    universe = _universe(args)
    max_age = -1 if args.force else args.max_age_days
    todo = select_symbols(universe, cache, now, max_age, args.limit)
    cnt = selection_counters(universe, cache, now, max_age, args.limit)
    print(f'[fmp_profile] universe={cnt["universe"]} cached={len(cache)} stale={cnt["stale"]} '
          f'to_fetch={cnt["to_fetch"]} skipped_fresh={cnt["skipped_fresh"]} '
          f'deferred_by_limit={cnt["deferred_by_limit"]} '
          f'max_age_days={args.max_age_days} limit={args.limit} cache={cache_path}')
    if args.dry_run:
        return 0

    fetched = empty = errors = 0
    t0 = time.time()
    try:
        for i, sym in enumerate(todo, 1):
            try:
                raw = fetch_profile(sym, api_key)
            except FMPAuthError:
                raise
            except Exception as e:  # noqa: BLE001 — one bad symbol must not end the sweep
                errors += 1
                sys.stderr.write(f'[fmp_profile] {sym}: {e}\n')
                time.sleep(args.sleep)
                continue
            cache[sym] = normalize_profile(raw, datetime.now(timezone.utc))
            if raw:
                fetched += 1
            else:
                empty += 1
            if i % args.flush_every == 0:
                atomic_write_json(cache_path, cache)
                print(f'[fmp_profile] {i}/{len(todo)} fetched={fetched} empty={empty} '
                      f'errors={errors} {time.time() - t0:.0f}s', flush=True)
            time.sleep(args.sleep)
    except FMPAuthError as e:
        atomic_write_json(cache_path, cache)
        print(f'[fmp_profile] ABORT {e} (progress saved: fetched={fetched} empty={empty})',
              file=sys.stderr)
        return 2
    finally:
        atomic_write_json(cache_path, cache)

    with_sector = sum(1 for v in cache.values() if isinstance(v, dict) and v.get('sector'))
    print(f'[fmp_profile] DONE to_fetch={len(todo)} fetched={fetched} empty={empty} '
          f'errors={errors} cache_size={len(cache)} with_sector={with_sector} '
          f'elapsed={time.time() - t0:.0f}s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
