"""
ingest_earnings_master.py
=========================
Keep data/master/earnings.parquet fed. This is the file every earnings
consumer actually reads (engine.py earnings_dte, S_earnings_sue_pead,
S_pre_earnings_vol_runup, S_ast_earnings_announcement_premium) — and it
froze at last_updated 2026-04-30 because its only refresh path was
FMP-based dead code with zero callers (2026-08-06 remediation spec §3).

Two feeds, both yfinance-sourced (the data ledger's provider of record for
earnings since 2026-04-30; both FMP earnings endpoints 403 on the current
key):

1. FORWARD MERGE (cheap, every run): earnings_calendar.parquet — already
   refreshed daily by ingest_earnings_calendar.py via collector.js — has
   next_earnings_date + estimates per ticker. Rows for (ticker, date) pairs
   the master lacks are APPENDED with eps_actual NaN, so `date >= today`
   consumers see upcoming events again.

2. ACTUALS TOP-UP (bounded, every run): master rows whose date just passed
   with eps_actual still NaN identify tickers that reported recently. Those
   tickers get one Ticker.earnings_dates call each (via the
   cboe_vol_indices yfinance gateway) and the reported EPS is filled IN
   PLACE (±1 day tolerance for bmo/amc date skew). Rows are never dropped.

--backfill sweeps an explicit ticker set (or the full prices universe)
through the actuals fetch — used once to repair the 2026-04-30 → today gap,
then again only after long outages.

Append-only contract (CLAUDE.md): rows and columns are only ever added or
NaN-filled; nothing is deleted. Writes are atomic (tmp + os.replace).

Usage
-----
    python3 src/ingestion/ingest_earnings_master.py               # daily
    python3 src/ingestion/ingest_earnings_master.py --dry-run
    python3 src/ingestion/ingest_earnings_master.py --backfill --limit 100
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
MASTER_PATH = ROOT / 'data' / 'master' / 'earnings.parquet'
CALENDAR_PATH = ROOT / 'data' / 'master' / 'earnings_calendar.parquet'
PRICES_PATH = ROOT / 'data' / 'master' / 'prices.parquet'

MASTER_COLUMNS = ['ticker', 'date', 'eps_actual', 'eps_estimated',
                  'revenue_actual', 'revenue_estimated', 'last_updated']

# How far back an unreported (eps_actual NaN) past event still triggers an
# actuals fetch. Covers a long weekend + a few missed runs.
ACTUALS_LOOKBACK_DAYS = 14


def _load_master() -> pd.DataFrame:
    if MASTER_PATH.exists():
        df = pd.read_parquet(MASTER_PATH)
        df['date'] = pd.to_datetime(df['date'], errors='coerce')
        return df
    return pd.DataFrame(columns=MASTER_COLUMNS)


def forward_rows(master: pd.DataFrame, today: date) -> pd.DataFrame:
    """Calendar rows the master lacks, shaped to the master schema."""
    if not CALENDAR_PATH.exists():
        return pd.DataFrame(columns=MASTER_COLUMNS)
    cal = pd.read_parquet(CALENDAR_PATH)
    if cal.empty:
        return pd.DataFrame(columns=MASTER_COLUMNS)
    cal = cal.rename(columns={'next_earnings_date': 'date',
                              'eps_estimate': 'eps_estimated',
                              'revenue_estimate': 'revenue_estimated'})
    cal['date'] = pd.to_datetime(cal['date'], errors='coerce')
    cal = cal.dropna(subset=['ticker', 'date'])
    have = set(zip(master['ticker'], master['date']))
    fresh = cal[[not (t, d) in have
                 for t, d in zip(cal['ticker'], cal['date'])]].copy()
    if fresh.empty:
        return pd.DataFrame(columns=MASTER_COLUMNS)
    fresh['eps_actual'] = float('nan')
    fresh['revenue_actual'] = float('nan')
    fresh['last_updated'] = today.isoformat()
    return fresh[MASTER_COLUMNS]


def tickers_needing_actuals(master: pd.DataFrame, today: date) -> list[str]:
    """Tickers with a just-passed event still missing its reported EPS."""
    if master.empty:
        return []
    lo = pd.Timestamp(today - timedelta(days=ACTUALS_LOOKBACK_DAYS))
    hi = pd.Timestamp(today)  # exclusive: today's event reports amc at best
    win = master[(master['date'] >= lo) & (master['date'] < hi)
                 & (master['eps_actual'].isna())]
    return sorted(win['ticker'].unique())


def apply_actuals(master: pd.DataFrame, actuals: pd.DataFrame,
                  today: date) -> tuple[pd.DataFrame, int, int]:
    """Fill reported EPS in place (±1d date tolerance); append unseen events.

    Returns (new_master, n_filled, n_appended). Never drops a row.
    """
    if actuals is None or actuals.empty:
        return master, 0, 0
    actuals = actuals.dropna(subset=['ticker', 'date']).copy()
    actuals['date'] = pd.to_datetime(actuals['date'], errors='coerce')
    actuals = actuals.dropna(subset=['date'])

    master = master.copy()
    filled = 0
    appended_rows = []
    by_ticker = {t: g for t, g in master.groupby('ticker')}
    for _, row in actuals.iterrows():
        t, d = row['ticker'], row['date']
        grp = by_ticker.get(t)
        idx = None
        if grp is not None:
            near = grp[(grp['date'] - d).abs() <= pd.Timedelta(days=1)]
            if not near.empty:
                idx = (near['date'] - d).abs().idxmin()
        if idx is not None:
            if pd.isna(master.at[idx, 'eps_actual']) and pd.notna(row.get('eps_actual')):
                master.at[idx, 'eps_actual'] = row['eps_actual']
                if pd.isna(master.at[idx, 'eps_estimated']):
                    master.at[idx, 'eps_estimated'] = row.get('eps_estimated')
                master.at[idx, 'last_updated'] = today.isoformat()
                filled += 1
        elif pd.notna(row.get('eps_actual')) or pd.notna(row.get('eps_estimated')):
            appended_rows.append({
                'ticker': t, 'date': d,
                'eps_actual': row.get('eps_actual'),
                'eps_estimated': row.get('eps_estimated'),
                'revenue_actual': float('nan'),
                'revenue_estimated': float('nan'),
                'last_updated': today.isoformat(),
            })
    if appended_rows:
        master = pd.concat([master, pd.DataFrame(appended_rows)],
                           ignore_index=True)
    return master, filled, len(appended_rows)


def _atomic_write(df: pd.DataFrame) -> None:
    df = df.sort_values(['ticker', 'date']).reset_index(drop=True)
    tmp = Path(str(MASTER_PATH) + '.tmp')
    df.to_parquet(tmp, index=False)
    import os
    os.replace(tmp, MASTER_PATH)


def _fetch_actuals(tickers: list[str], throttle_s: float) -> pd.DataFrame:
    sys.path.insert(0, str(ROOT))
    from src.ingestion.cboe_vol_indices import get_earnings_history
    return get_earnings_history(tickers, throttle_s=throttle_s)


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--backfill', action='store_true',
                   help='actuals sweep over --tickers/--limit of the prices '
                        'universe instead of the recently-reported set')
    p.add_argument('--tickers', default=None,
                   help='comma-separated explicit ticker set (backfill)')
    p.add_argument('--limit', type=int, default=None,
                   help='cap ticker count (backfill debug aid)')
    p.add_argument('--throttle', type=float, default=0.4)
    args = p.parse_args(argv)

    today = date.today()
    master = _load_master()
    n0 = len(master)
    print(f'earnings master: {n0} rows, '
          f'{master["ticker"].nunique() if n0 else 0} tickers')

    # 1. Forward merge (skipped in backfill mode — backfill is actuals-only).
    fwd = pd.DataFrame(columns=MASTER_COLUMNS)
    if not args.backfill:
        fwd = forward_rows(master, today)
        print(f'  forward merge: +{len(fwd)} upcoming events from calendar')

    # 2. Actuals.
    if args.backfill:
        if args.tickers:
            targets = sorted({t.strip() for t in args.tickers.split(',') if t.strip()})
        else:
            targets = sorted(pd.read_parquet(
                PRICES_PATH, columns=['ticker'])['ticker'].unique())
        if args.limit:
            targets = targets[:args.limit]
    else:
        targets = tickers_needing_actuals(master, today)
    print(f'  actuals fetch: {len(targets)} tickers')

    if args.dry_run:
        print(f'  --dry-run: not fetching actuals, not writing '
              f'(would merge {len(fwd)} forward rows)')
        return 0

    actuals = _fetch_actuals(targets, args.throttle) if targets else None
    master, filled, appended = apply_actuals(master, actuals, today)
    print(f'  actuals: filled {filled} in place, appended {appended} new events')

    if not fwd.empty:
        master = pd.concat([master, fwd], ignore_index=True)

    if len(master) < n0:
        print(f'  ABORT: merge would shrink master {n0} → {len(master)} rows '
              f'— append-only violated, not writing', file=sys.stderr)
        return 1

    _atomic_write(master)
    print(f'  wrote {len(master)} rows ({len(master) - n0:+d}) → {MASTER_PATH}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
