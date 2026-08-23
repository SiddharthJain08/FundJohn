"""Intraday insider (Form 4) adapter — tier-1 (14:30 ET) acting-set ingest.

Three-tier ingestion (operator directive 2026-07-29): every category an ACTING
strategy consumes must be fetched fresh before the 15:00 compute reads it.
Insider is the category where that matters most literally — Form 4s must be
filed within two business days and post to EDGAR *throughout the trading day*,
so the previous EOD collect is genuinely behind by the time we size.

Scope comes from the STREAM, not the universe. The EOD backfiller walks
`/insider-trading/search?symbol=X` once per ticker — 5,173 calls against a
~5/s plan cap, which is not a 30-minute job. `/insider-trading/latest` returns
the global filing stream most-recent-first at 1,000 rows/page; measured
2026-07-30, one page spans ~1.5 days of filings, so a handful of pages covers
everything since the last collect. We page until a page is entirely older than
`since`, then filter to the acting universe.

DEDUP IS A CORRECTNESS REQUIREMENT HERE, not hygiene. Unlike the options
overlay (where the engine takes `chain['date'].max()` and a duplicate is
inert), engine.load_aux_data builds a LIST of every transaction per ticker —
so a row already in the master would be counted twice by
S_insider_drawdown_confirmation. We anti-join on the master's own key and
dedup within the fetch, since FMP paging can repeat rows across boundaries.

The key uses `date`, not `filing_date`: they are equal wherever both are
present (verified across 243,541 master rows) but `filing_date` is NULL in
7.1% of history, and NULL never matches in a join. Overlay rows populate both
so the EOD writer's own INSIDER_KEYS still works when it later ingests the
same filings.

Writes a day-scoped overlay: data/derived/intraday/<date>/insider.parquet.
The master remains the append-only EOD record.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
BASE = 'https://financialmodelingprep.com/stable/insider-trading/latest'
PAGE_LIMIT = int(os.environ.get('OPENCLAW_INTRADAY_INSIDER_PAGE_LIMIT', '1000'))
MAX_PAGES = int(os.environ.get('OPENCLAW_INTRADAY_INSIDER_MAX_PAGES', '8'))
_TIMEOUT = 30

# Master column order (data/master/insider.parquet).
RAW_COLS = ['ticker', 'date', 'transaction_date', 'insider_name', 'role',
            'transaction_type', 'shares', 'price_per_share', 'net_value',
            'shares_owned_after', 'filing_date']
# INSIDER_KEYS with filing_date -> date (see module docstring).
DEDUP_KEYS = ['ticker', 'date', 'insider_name', 'transaction_type', 'shares']


def _record_fmp(endpoint, status, body):
    """data_provider_health, best-effort (2026-08-23)."""
    try:
        from src.maintenance.provider_health import record_http
        return record_http('fmp', endpoint, status, body)
    except Exception:  # noqa: BLE001
        return None


class IntradayInsiderError(RuntimeError):
    """Raised when the filing stream cannot be read at all."""


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _to_row(rec: dict) -> dict | None:
    sym = (rec.get('symbol') or '').strip().upper()
    filed = rec.get('filingDate')
    if not sym or not filed:
        return None
    filed = str(filed)[:10]
    shares = _f(rec.get('securitiesTransacted'))
    price = _f(rec.get('price'))
    return {
        'ticker': sym,
        # `date` mirrors filing_date — that is the master's own convention
        # (equal in 100% of rows where both are present).
        'date': filed,
        'transaction_date': (str(rec.get('transactionDate'))[:10]
                             if rec.get('transactionDate') else None),
        'insider_name': rec.get('reportingName'),
        'role': rec.get('typeOfOwner'),
        'transaction_type': rec.get('transactionType'),
        'shares': shares,
        'price_per_share': price,
        'net_value': (shares * price) if (shares is not None and price is not None) else None,
        'shares_owned_after': _f(rec.get('securitiesOwned')),
        'filing_date': filed,
    }


def fetch_latest_filings(since: str, budget_s: float | None = None,
                         max_pages: int | None = None):
    """Global Form-4 stream back to `since` (inclusive, 'YYYY-MM-DD').

    Returns (rows, stats). Pages until a page holds nothing at/after `since`.
    A failure on page 0 raises — "the provider refused" and "no filings today"
    must not both surface as an empty list."""
    import requests

    key = os.environ.get('FMP_API_KEY', '')
    if not key:
        raise IntradayInsiderError('FMP_API_KEY not set')
    max_pages = max_pages or MAX_PAGES
    t0 = time.monotonic()
    rows: list[dict] = []
    stats = {'pages': 0, 'raw_rows': 0, 'oldest_seen': None, 'since': since,
             'budget_expired': False, 'http_errors': 0}

    for page in range(max_pages):
        if budget_s is not None and (time.monotonic() - t0) >= budget_s:
            stats['budget_expired'] = True
            logger.warning('intraday_insider: budget %ss expired after %d page(s) '
                           '— stream truncated at %s', budget_s, page,
                           stats['oldest_seen'])
            break
        r = None
        try:
            r = requests.get(BASE, timeout=_TIMEOUT, params={
                'page': page, 'limit': PAGE_LIMIT, 'apikey': key})
            _record_fmp('insider-trading/latest', r.status_code, getattr(r, 'text', ''))
            r.raise_for_status()
            data = r.json() or []
        except Exception as exc:  # noqa: BLE001
            if r is None:   # transport failure — no response to classify
                _record_fmp('insider-trading/latest', None, str(exc))
            stats['http_errors'] += 1
            if page == 0:
                raise IntradayInsiderError(f'insider stream page 0 failed: {exc}')
            logger.warning('intraday_insider: page %d failed (%s) — stream '
                           'truncated', page, exc)
            break
        if not isinstance(data, list) or not data:
            break
        stats['pages'] += 1
        stats['raw_rows'] += len(data)
        page_dates = [str(rec.get('filingDate') or '')[:10] for rec in data]
        stats['oldest_seen'] = min([d for d in page_dates if d] or
                                   [stats['oldest_seen'] or ''])
        kept = 0
        for rec in data:
            row = _to_row(rec)
            if row and row['date'] >= since:
                rows.append(row)
                kept += 1
        # The stream is most-recent-first: a page with nothing at/after `since`
        # means everything beyond it is older too.
        if kept == 0:
            break

    stats['elapsed_s'] = round(time.monotonic() - t0, 1)
    return rows, stats


def master_keys(since: str) -> set:
    """Dedup keys already in the master, limited to filings at/after `since`.

    Bounded read: the anti-join only needs the recent tail, and the master is
    243k rows and growing."""
    path = ROOT / 'data' / 'master' / 'insider.parquet'
    if not path.exists():
        return set()
    try:
        import pyarrow.dataset as ds
        import pyarrow.compute as pc
        tbl = ds.dataset(path, format='parquet').to_table(
            columns=DEDUP_KEYS, filter=(pc.field('date') >= since))
        df = tbl.to_pandas()
    except Exception as exc:  # noqa: BLE001
        logger.warning('intraday_insider: master read failed (%s) — NOT '
                       'deduping; refusing to risk double-counted txns', exc)
        raise IntradayInsiderError(f'master key read failed: {exc}') from exc
    return {tuple(t) for t in df.itertuples(index=False, name=None)}


def build_overlay(tickers, as_of: pd.Timestamp, since: str | None = None,
                  budget_s: float | None = None):
    """New-since-`since` Form 4s for `tickers`, deduped against the master.

    `since` defaults to the master's newest filing date, so the overlay is
    exactly the gap the EOD collect has not yet closed."""
    universe = {t.strip().upper() for t in tickers}
    if since is None:
        since = _master_max_date() or (as_of - pd.Timedelta(days=3)).strftime('%Y-%m-%d')

    rows, stats = fetch_latest_filings(since, budget_s=budget_s)
    stats['universe'] = len(universe)
    if not rows:
        stats.update({'rows': 0, 'in_universe': 0, 'dup_in_master': 0})
        return pd.DataFrame(columns=RAW_COLS), stats

    df = pd.DataFrame(rows, columns=RAW_COLS)
    df = df[df['ticker'].isin(universe)]
    stats['in_universe'] = len(df)
    # Within-fetch dedup: FMP paging repeats rows across boundaries.
    df = df.drop_duplicates(subset=DEDUP_KEYS)
    before = len(df)
    known = master_keys(since)
    if known and not df.empty:
        keep = [tuple(t) not in known
                for t in df[DEDUP_KEYS].itertuples(index=False, name=None)]
        df = df[keep]
    stats['dup_in_master'] = before - len(df)
    stats['rows'] = len(df)
    logger.info('intraday_insider: %d new filing(s) for %d universe ticker(s) '
                'since %s (%d already in master, %d pages, %ss)',
                stats['rows'], df['ticker'].nunique() if not df.empty else 0,
                since, stats['dup_in_master'], stats['pages'], stats['elapsed_s'])
    return df.reset_index(drop=True), stats


def _master_max_date() -> str | None:
    path = ROOT / 'data' / 'master' / 'insider.parquet'
    if not path.exists():
        return None
    try:
        import pyarrow.parquet as pq
        col = pq.read_table(path, columns=['date']).column('date')
        vals = [v for v in col.to_pylist() if v]
        return max(vals) if vals else None
    except Exception as exc:  # noqa: BLE001
        logger.warning('intraday_insider: master max-date read failed (%s)', exc)
        return None
