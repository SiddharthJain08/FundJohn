"""Storage-tagged check: every master under data/master/ is still being fed.

Guards the orphaned-refresh class. Three confirmed instances of plausible
code nothing calls letting a store silently freeze:
  * options_aggregates_enriched froze at 2026-04-22 when the aggregates
    collector was retired (caught 2026-07-29, options_aux_freshness.py);
  * correlation_matrix.py computed a matrix nothing consumed (deleted d4271f1);
  * fetch_fmp_earnings_calendar's docstring claimed "called daily by
    pipeline_orchestrator" with ZERO callers — earnings.parquet froze at
    last_updated 2026-04-30 and every engine run logged "0 upcoming events"
    for three months (2026-08-06 remediation spec §3).

The exit-code/skip pattern makes this class invisible: every pipeline day is
green while the store decays. So this check asserts data recency directly,
per master, from a declared cadence table — and WARNs on any master parquet
that has NO declared cadence, so the next new store must either get a row
here or show up in every health digest.

Memory safety: freshness comes from parquet row-group STATISTICS (zero data
read — options_eod is 1.2GB and a bare read_parquet OOM-killed a probe on
this box). Files whose writer recorded no stats fall back to a single-column
read only when small.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path

from ..registry import check
from ..types import Status

_MASTER_DIR = Path(os.environ.get('OPENCLAW_MASTER_DIR',
                                  '/root/openclaw/data/master'))

# filename → (freshness column | None to use file mtime, max lag in days).
# Lag allows the store's natural cadence plus a long weekend / holiday.
_CADENCES: dict[str, tuple[str | None, int]] = {
    'prices.parquet':             ('date', 5),
    'options_eod.parquet':        ('date', 5),
    'sentiment.parquet':          ('date', 5),
    'insider.parquet':            ('date', 7),
    'macro.parquet':              ('date', 7),
    'vol_indices.parquet':        ('date', 7),
    'iv_history.parquet':         ('date', 7),
    'prices_30m.parquet':         ('date', 7),
    'historical_regimes.parquet': ('date', 7),
    'crypto_bars_1h.parquet':     ('ts_utc', 3),    # 24/7 market
    'intraday_features.parquet':  ('ts_utc', 7),
    # earnings: the refresh appends forward-calendar rows and reported
    # actuals daily, stamping last_updated (ISO str) — see spec §3.
    'earnings.parquet':           ('last_updated', 7),
    # earnings_calendar is overwritten (not appended) daily by
    # ingest_earnings_calendar.py, so mtime IS its freshness.
    'earnings_calendar.parquet':  (None, 4),
    # financials max(date) is a fiscal period END; quarterly cadence.
    'financials.parquet':         ('date', 120),
    'shares_outstanding.parquet': ('asof_date', 35),
    # corporate_actions is sparse/event-driven; collector touches it on run.
    'corporate_actions.parquet':  (None, 45),
}

# Covered by a dedicated, stricter check — do not double-report here.
_COVERED_ELSEWHERE = {'options_aggregates_enriched.parquet'}  # options_aux_freshness

_SMALL_ENOUGH_FOR_COLUMN_READ = 100 * 1024 * 1024  # bytes


def _coerce(v):
    """Row-group stat / cell → date, best effort. None when unparseable."""
    if v is None:
        return None
    if isinstance(v, bytes):
        v = v.decode('utf-8', 'replace')
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v[:19]).date()
        except ValueError:
            return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def _max_from_stats(path: Path, column: str):
    """Max of `column` from row-group statistics — zero data read."""
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(path)
    try:
        idx = pf.schema_arrow.names.index(column)
    except ValueError:
        return None, f'no column {column!r}'
    best = None
    for rg in range(pf.metadata.num_row_groups):
        st = pf.metadata.row_group(rg).column(idx).statistics
        if st is None or not st.has_min_max:
            return None, 'no row-group stats'
        d = _coerce(st.max)
        if d is not None and (best is None or d > best):
            best = d
    return best, None


def _max_from_column(path: Path, column: str):
    import pandas as pd
    s = pd.read_parquet(path, columns=[column])[column]
    return max((d for d in (_coerce(v) for v in s) if d is not None),
               default=None)


@check(name='master_freshness', tags=['storage'], requires=[])
def _master_freshness():
    if not _MASTER_DIR.is_dir():
        return Status.WARN, f'master dir missing: {_MASTER_DIR}'
    today = date.today()
    stale, missing, unknown, unreadable = [], [], [], []

    for fname, (column, max_lag) in _CADENCES.items():
        path = _MASTER_DIR / fname
        if not path.exists():
            missing.append(fname)
            continue
        if column is None:
            newest = datetime.fromtimestamp(path.stat().st_mtime).date()
        else:
            try:
                newest, why = _max_from_stats(path, column)
                if newest is None and why == 'no row-group stats':
                    if path.stat().st_size <= _SMALL_ENOUGH_FOR_COLUMN_READ:
                        newest = _max_from_column(path, column)
                    else:
                        unreadable.append(f'{fname} ({why}, too big to scan)')
                        continue
                elif newest is None:
                    unreadable.append(f'{fname} ({why})')
                    continue
            except Exception as e:  # noqa: BLE001
                unreadable.append(f'{fname} ({e})')
                continue
        lag = (today - newest).days
        if lag > max_lag:
            stale.append(f'{fname} {lag}d stale (max {max_lag}d, newest {newest})')

    declared = set(_CADENCES) | _COVERED_ELSEWHERE
    for p in sorted(_MASTER_DIR.glob('*.parquet')):
        if p.name.startswith('.'):
            continue  # editor/backup droppings, not masters
        if p.name not in declared:
            unknown.append(p.name)

    if stale:
        extra = f'; undeclared: {unknown}' if unknown else ''
        return Status.FAIL, f'STALE master(s): {"; ".join(stale)}{extra}'
    notes = []
    if missing:
        notes.append(f'missing: {missing}')
    if unknown:
        notes.append(f'no declared cadence (add to _CADENCES): {unknown}')
    if unreadable:
        notes.append(f'unreadable: {unreadable}')
    if notes:
        return Status.WARN, '; '.join(notes)
    return Status.PASS, f'{len(_CADENCES)} masters within cadence'
