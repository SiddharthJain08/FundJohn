"""Intraday financials adapter — tier-1 (14:30 ET) acting-set ingest.

Three-tier ingestion (operator directive 2026-07-29): an ACTING strategy must
never decide on the previous day's EOD collect. For fundamentals that bites on
exactly one day per name — the day it reports — and it bites hard: the three
consumers (S_fama_french_anomaly_dissection, S_prism_vq_cross_section_factor,
S_value_momentum_everywhere) rank the cross-section on margins, ROE and
multiples, so a reporter scored on last quarter's numbers is ranked against
peers on a different basis entirely.

SCOPE IS THE EARNINGS CALENDAR, not the universe. Sweeping 5,173 tickers × 4
endpoints at FMP's ~5/s cap is a multi-hour job; the names whose fundamentals
can have changed since the last collect are the ones that just reported.
Measured 2026-07-30: ~283 in-universe reporters per day, ~270s at the
backfiller's 1.0s/ticker pacing.

The window is [as_of-1, as_of]: an after-close reporter on day T-1 publishes
too late for T-1's 14:30 pass, so T picks it up.

Publication lag is NOT a problem — verified 2026-07-30 on six 07-29 reporters
(ALRS, VICI, AMRN, CVNA, VFC, ALKT): FMP carried the June quarter for all six
the same day, while the master still held only March. The lag runs the other
way; this adapter is what closes it.

Rows are built by build_financial_rows — the SAME function the EOD backfiller
uses — so intraday and EOD fundamentals can never drift into different column
semantics.

DEDUP KEYS ON (ticker, period-end DATE), not on `period`. The master's
`period` labels are polluted ('Q2', '2025Q4' and 'undefinedQ2' all occur, and
AAPL carries the same 2026-03-28 quarter under two of them), so a label-keyed
anti-join would re-add quarters the master already holds. The period-end date
is unambiguous and is what engine.load_aux_data actually sorts on.

Writes a day-scoped overlay: data/derived/intraday/<date>/financials.parquet.
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
CALENDAR = 'https://financialmodelingprep.com/stable/earnings-calendar'
# Matches the backfiller's MIN_BACKFILL_CALLS: 4 calls/ticker at 1.0s keeps
# the steady state near FMP Starter's ~5/s cap (the 2026-07-03 sweep 429'd on
# 409/581 tickers at 0.05s).
PACING_S = float(os.environ.get('OPENCLAW_INTRADAY_FIN_PACING_S', '1.0'))
QUARTERS = 2          # current + prior, so a restatement of the prior lands too
_TIMEOUT = 30


class IntradayFinancialsError(RuntimeError):
    """Raised when the reporter calendar cannot be read at all."""


def reporters(as_of: pd.Timestamp, universe) -> list[str]:
    """In-universe tickers that reported over [as_of-1, as_of] and already
    have an actual EPS — a scheduled-but-unreleased row has nothing new to
    fetch."""
    import requests

    key = os.environ.get('FMP_API_KEY', '')
    if not key:
        raise IntradayFinancialsError('FMP_API_KEY not set')
    uni = {t.strip().upper() for t in universe}
    frm = (as_of - pd.Timedelta(days=1)).strftime('%Y-%m-%d')
    to = as_of.strftime('%Y-%m-%d')
    try:
        r = requests.get(CALENDAR, timeout=_TIMEOUT,
                         params={'from': frm, 'to': to, 'apikey': key})
        r.raise_for_status()
        data = r.json() or []
    except Exception as exc:  # noqa: BLE001
        raise IntradayFinancialsError(f'earnings calendar failed: {exc}') from exc
    if not isinstance(data, list):
        raise IntradayFinancialsError(f'earnings calendar returned {type(data).__name__}')
    out = []
    for rec in data:
        sym = (rec.get('symbol') or '').strip().upper()
        if sym in uni and rec.get('epsActual') is not None:
            out.append(sym)
    return sorted(set(out))


def master_period_keys(tickers) -> set:
    """{(ticker, period_end_date)} already in the master, for the given
    tickers. Keyed on the DATE because `period` labels are unreliable."""
    path = ROOT / 'data' / 'master' / 'financials.parquet'
    if not path.exists():
        return set()
    try:
        import pyarrow.parquet as pq
        df = pq.read_table(path, columns=['ticker', 'date']).to_pandas()
    except Exception as exc:  # noqa: BLE001
        logger.warning('intraday_financials: master read failed (%s) — NOT '
                       'deduping; refusing to risk duplicate periods', exc)
        raise IntradayFinancialsError(f'master key read failed: {exc}') from exc
    want = set(tickers)
    df = df[df['ticker'].isin(want)]
    return {(t, str(d)[:10]) for t, d in
            df.itertuples(index=False, name=None)}


def build_overlay(tickers, as_of: pd.Timestamp, budget_s: float | None = None):
    """Newly-published quarters for today's in-universe reporters.

    Returns (DataFrame in master column shape, stats)."""
    import sys as _sys
    _sys.path.insert(0, str(ROOT / 'workspaces' / 'default' / 'tools'))
    _sys.path.insert(0, str(ROOT / 'src'))
    import fmp as fmp_tool
    from pipeline.backfillers.fmp import build_financial_rows

    t0 = time.monotonic()
    cands = reporters(as_of, tickers)
    stats = {'universe': len(set(tickers)), 'reporters': len(cands),
             'fetched': 0, 'failed': 0, 'skipped_budget': 0,
             'dup_in_master': 0, 'rows': 0, 'failed_sample': []}
    if not cands:
        stats['elapsed_s'] = round(time.monotonic() - t0, 1)
        logger.info('intraday_financials: no in-universe reporters for %s — '
                    'nothing to fetch (a quiet day, not a failure)', as_of.date())
        return pd.DataFrame(), stats

    known = master_period_keys(cands)
    rows_out: list[dict] = []
    for i, tk in enumerate(cands):
        if budget_s is not None and (time.monotonic() - t0) >= budget_s:
            stats['skipped_budget'] = len(cands) - i
            logger.warning('intraday_financials: budget %ss expired — %d of %d '
                           'reporters not fetched', budget_s,
                           stats['skipped_budget'], len(cands))
            break
        try:
            statements = fmp_tool.get_financial_statements(tk, period='quarterly', limit=QUARTERS)
            metrics = fmp_tool.get_key_metrics(tk, limit=QUARTERS)
            ratios = fmp_tool.get_ratios(tk, limit=QUARTERS)
            balance = fmp_tool.get_balance_sheet(tk, period='quarterly', limit=QUARTERS)
        except Exception as exc:  # noqa: BLE001
            stats['failed'] += 1
            if len(stats['failed_sample']) < 10:
                stats['failed_sample'].append(f'{tk}: {str(exc)[:80]}')
            continue
        stats['fetched'] += 1
        for row in build_financial_rows(tk, statements, metrics, ratios, balance):
            if (row['ticker'], str(row['date'])[:10]) in known:
                stats['dup_in_master'] += 1
                continue
            rows_out.append(row)
        time.sleep(PACING_S)

    stats['elapsed_s'] = round(time.monotonic() - t0, 1)
    stats['rows'] = len(rows_out)
    df = pd.DataFrame(rows_out)
    logger.info('intraday_financials: %d new period-row(s) for %d of %d '
                'reporter(s) (%d already in master, %d failed) in %ss',
                stats['rows'], df['ticker'].nunique() if not df.empty else 0,
                len(cands), stats['dup_in_master'], stats['failed'],
                stats['elapsed_s'])
    return df, stats
