"""Intraday options chain adapter — tier-1 (15:00 ET) acting-set ingest.

Three-tier ingestion directive (2026-07-29): every category an ACTING strategy
consumes must be ingested fresh at the ~15:00 ET pre-compute; an acting
strategy must never decide on the previous day's EOD collect. Options are the
heaviest such category — this module fetches today's chain per acting ticker
and aggregates it into the SAME per-(ticker, date) row shape the EOD path
produces, so the signal engine sees today's surface instead of yesterday's.

Source: `alpaca data option chain --underlying-symbol X` (OPRA feed) returns
latest quote/trade, dailyBar and greeks + impliedVolatility per contract.

Two measured provider facts shape this adapter (2026-07-29):
  * 0-DTE contracts carry NO greeks and NO impliedVolatility (verified on SPY
    same-day expiries: greeks all zero, iv null, while dailyBar.v is
    populated). They are excluded — a chain of nulls would drag iv_front down
    rather than inform it. The EOD builder keeps them because its rows are
    already IV-less; the resulting front-expiry choice can differ on an expiry
    day, which is documented rather than hidden.
  * open_interest is absent from the feed entirely, so every OI-weighted field
    (gex, contracts_liquid, iv_centroid_delta, surface_premium) resolves to
    None — never a computed zero. See build_options_aggregates for the same
    guard on the historical path.

Writes a day-scoped OVERLAY, never a master: data/derived/intraday/<date>/
options.parquet (atomic tmp + os.replace). The master options_eod.parquet
remains the append-only EOD record written by the post-close collector.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
OVERLAY_ROOT = ROOT / 'data' / 'derived' / 'intraday'
CLI = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')

# Front expiry is chosen at <=45 DTE and the back leg at ~55-95 DTE
# (engine.load_aux_data term-structure bands), so 100 days covers both.
MAX_DTE = int(os.environ.get('OPENCLAW_INTRADAY_OPTIONS_MAX_DTE', '100'))
PAGE_LIMIT = 500
# Deepest measured chain (SPY, 2026-07-30) is ~15 pages; 24 leaves headroom
# without letting one pathological underlying eat the wall-clock budget.
MAX_PAGES = int(os.environ.get('OPENCLAW_INTRADAY_OPTIONS_MAX_PAGES', '24'))
# The fetch is network-bound, not CPU-bound, so the 2-core box tolerates more
# in flight than it has cores. Keep it modest: these are subprocesses.
WORKERS = int(os.environ.get('OPENCLAW_INTRADAY_OPTIONS_WORKERS', '8'))
_TIMEOUT = 45
# OCC symbol: ROOT + YYMMDD + C/P + strike*1000 (8 digits).
_OCC = re.compile(r'^(?P<root>[A-Z]+)(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})'
                  r'(?P<cp>[CP])(?P<strike>\d{8})$')


class IntradayOptionsError(RuntimeError):
    """Raised when the adapter cannot fetch anything at all."""


def _run_cli(args: list[str]):
    try:
        out = subprocess.run([CLI, '-q'] + args, capture_output=True, text=True,
                             timeout=_TIMEOUT, env=os.environ)
    except Exception as e:  # noqa: BLE001
        logger.warning('intraday_options: CLI invocation failed (%s)', e)
        return None
    if out.returncode != 0:
        logger.warning('intraday_options: CLI rc=%s stderr=%s',
                       out.returncode, (out.stderr or '')[:200])
        return None
    try:
        return json.loads(out.stdout or '{}')
    except Exception:  # noqa: BLE001
        return None


def _decode_occ(sym: str) -> dict | None:
    m = _OCC.match(sym or '')
    if not m:
        return None
    g = m.groupdict()
    return {
        'expiry': pd.Timestamp(f"20{g['y']}-{g['m']}-{g['d']}"),
        'option_type': 'CALL' if g['cp'] == 'C' else 'PUT',
        'strike': int(g['strike']) / 1000.0,
    }


def fetch_chain_rows(ticker: str, as_of: pd.Timestamp,
                     row_ticker: str | None = None) -> list[dict]:
    """Today's chain for one underlying, shaped like options_eod columns.

    0-DTE is excluded (no greeks/IV from the provider); expiries beyond
    MAX_DTE are not requested. Pages until exhausted.

    A CLI failure on the FIRST page raises: "the provider refused" and "this
    underlying has no listed options" must not both surface as an empty list,
    or an outage reads as a clean, empty ingest."""
    gte = (as_of + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
    lte = (as_of + pd.Timedelta(days=MAX_DTE)).strftime('%Y-%m-%d')
    rows: list[dict] = []
    page = None
    for _page_i in range(MAX_PAGES):
        args = ['data', 'option', 'chain', '--underlying-symbol', ticker,
                '--expiration-date-gte', gte, '--expiration-date-lte', lte,
                '--limit', str(PAGE_LIMIT)]
        if page:
            args += ['--page-token', page]
        payload = _run_cli(args)
        if not isinstance(payload, dict):
            if _page_i == 0:
                raise IntradayOptionsError(f'{ticker}: chain request failed')
            # A mid-pagination failure keeps the pages already collected but
            # is not silent — the driver records it as a truncated chain.
            logger.warning('intraday_options: %s truncated at page %d', ticker, _page_i)
            break
        snaps = payload.get('snapshots') or {}
        for occ, snap in snaps.items():
            dec = _decode_occ(occ)
            if not dec:
                continue
            greeks = snap.get('greeks') or {}
            daily = snap.get('dailyBar') or {}
            trade = snap.get('latestTrade') or {}
            rows.append({
                'ticker': row_ticker or ticker,
                'date': as_of.normalize(),
                'expiry': dec['expiry'],
                'strike': dec['strike'],
                'option_type': dec['option_type'],
                # Absent from this feed — kept as a column so the aggregate
                # math's has_oi guard sees NaN and reports UNKNOWN, not 0.
                'open_interest': None,
                'implied_volatility': snap.get('impliedVolatility'),
                'delta': greeks.get('delta'),
                'gamma': greeks.get('gamma'),
                'theta': greeks.get('theta'),
                'vega': greeks.get('vega'),
                'volume': daily.get('v'),
                # `close` feeds the aggregate's `spot`; intraday the last trade
                # is the better proxy, falling back to the day bar's close.
                'close': (trade.get('p') or daily.get('c') or None),
            })
        page = payload.get('next_page_token')
        if not page:
            break
    return rows


RAW_COLS = ['ticker', 'date', 'expiry', 'strike', 'option_type', 'open_interest',
            'implied_volatility', 'delta', 'gamma', 'theta', 'vega', 'volume', 'close']


def fetch_raw_frame(tickers, as_of: pd.Timestamp, budget_s: float | None = None,
                    workers: int | None = None):
    """Concurrent per-ticker chain fetch → (raw DataFrame, stats).

    Returns rows in options_eod column shape so the LIVE engine can consume
    them with ZERO changes to its field math: every derived field in
    engine.load_aux_data keys off ``chain['date'].max()``, so today-dated raw
    rows make the whole surface same-day by construction.

    ``budget_s`` is a HARD global wall clock. When it expires the fetch stops
    submitting and returns what it has: a partial overlay degrades to the EOD
    panel per-ticker, whereas overrunning into the 15:00 compute would delay
    execution itself. Every skipped ticker is counted in ``stats``, never
    dropped quietly."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from ingestion.close_proxy_snapshot import _to_alpaca_equity

    requested = list(dict.fromkeys(tickers))
    # Symbol classes the provider has no underlying for (indices `^IXIC`,
    # preferreds `T-PRC`, rights/warrants/units, FX). Requesting them yields a
    # guaranteed 400 that would otherwise be counted as a fetch FAILURE and
    # muddy the coverage signal the whole three-tier design depends on.
    pairs = [(tk, _to_alpaca_equity(tk)) for tk in requested]
    tickers = [(tk, sym) for tk, sym in pairs if sym]
    workers = workers or WORKERS
    t0 = time.monotonic()
    stats = {'attempted': 0, 'ok': 0, 'empty': 0, 'failed': 0,
             'skipped_budget': 0, 'requested': len(requested), 'rows': 0,
             'skipped_class': len(pairs) - len(tickers), 'failed_sample': []}
    frames: list[pd.DataFrame] = []

    def _expired() -> bool:
        return budget_s is not None and (time.monotonic() - t0) >= budget_s

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        it = iter(tickers)
        submitted = 0

        def _submit_next() -> bool:
            nonlocal submitted
            try:
                tk, sym = next(it)
            except StopIteration:
                return False
            pending[pool.submit(fetch_chain_rows, sym, as_of, tk)] = tk
            submitted += 1
            return True

        for _ in range(workers):
            if not _submit_next():
                break
        while pending:
            done = next(as_completed(list(pending)))
            tk = pending.pop(done)
            stats['attempted'] += 1
            try:
                rows = done.result()
            except Exception as exc:  # noqa: BLE001
                stats['failed'] += 1
                if len(stats['failed_sample']) < 10:
                    stats['failed_sample'].append(f'{tk}: {str(exc)[:80]}')
            else:
                if rows:
                    stats['ok'] += 1
                    frames.append(pd.DataFrame(rows, columns=RAW_COLS))
                else:
                    stats['empty'] += 1
            if not _expired():
                _submit_next()
        stats['skipped_budget'] = len(tickers) - submitted

    stats['elapsed_s'] = round(time.monotonic() - t0, 1)
    if stats['skipped_budget']:
        logger.warning('intraday_options: wall-clock budget %ss expired — %d of '
                       '%d tickers not requested', budget_s,
                       stats['skipped_budget'], len(tickers))
    df = (pd.concat(frames, ignore_index=True) if frames
          else pd.DataFrame(columns=RAW_COLS))
    stats['rows'] = len(df)
    logger.info('intraday_options: %d rows / %d ok / %d empty / %d failed / '
                '%d budget-skipped / %d non-optionable in %ss',
                stats['rows'], stats['ok'], stats['empty'], stats['failed'],
                stats['skipped_budget'], stats['skipped_class'], stats['elapsed_s'])
    return df, stats


def build_overlay(tickers, as_of: pd.Timestamp,
                  raw: pd.DataFrame | None = None) -> pd.DataFrame:
    """Aggregate today's chains for `tickers` into per-(ticker, date) rows.

    Reuses build_options_aggregates._aggregate_day verbatim, so the intraday
    surface and the historical panel are computed by ONE definition.

    Pass ``raw`` (from fetch_raw_frame) to aggregate an already-fetched frame
    instead of re-hitting the provider — the driver writes both artifacts from
    a single fetch."""
    import sys
    sys.path.insert(0, str(ROOT / 'scripts'))
    from build_options_aggregates import _aggregate_day, OUT_COLS

    tickers = list(tickers)
    if raw is None:
        raw, _ = fetch_raw_frame(tickers, as_of)
    # `spot` must be the UNDERLYING price. _aggregate_day deliberately leaves
    # it None (chain['close'] is a CONTRACT price — using it produced ~$10
    # "spots" for SPY); intraday the hardened close-proxy snapshot IS the
    # underlying's current price, which is exactly what the 15:00 chain wants.
    spot_by: dict = {}
    try:
        from ingestion.close_proxy_snapshot import fetch_close_proxy
        spot_by = fetch_close_proxy(tickers, None) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning('intraday_options: spot snapshot failed (%s) — '
                       'spot left NULL', exc)

    out_rows = []
    greeks = ['delta', 'gamma', 'theta', 'vega']
    for tk, df in (raw.groupby('ticker') if not raw.empty else []):
        # Mirror the historical builder's degenerate-row drop.
        df = df[~(df[greeks].fillna(0) == 0).all(axis=1)]
        if df.empty:
            continue
        row = _aggregate_day(df, as_of.normalize())
        if row is not None:
            row['spot'] = spot_by.get(tk)
            out_rows.append(row)
    if not out_rows:
        raise IntradayOptionsError(
            f'intraday options fetch produced no aggregate rows for '
            f'{len(list(tickers))} ticker(s) on {as_of.date()}')
    return pd.DataFrame(out_rows, columns=OUT_COLS)


def overlay_path(as_of: pd.Timestamp, category: str = 'options') -> Path:
    return OVERLAY_ROOT / as_of.strftime('%Y-%m-%d') / f'{category}.parquet'


def write_overlay(df: pd.DataFrame, as_of: pd.Timestamp,
                  category: str = 'options') -> Path:
    path = overlay_path(as_of, category)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.parquet.tmp')
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    logger.info('intraday_options: wrote %d rows -> %s', len(df), path)
    return path


def load_overlay(as_of: pd.Timestamp, category: str = 'options'):
    """Today's overlay, or None when the tier-1 ingest has not run."""
    path = overlay_path(as_of, category)
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        logger.warning('intraday_options: overlay unreadable (%s)', e)
        return None
