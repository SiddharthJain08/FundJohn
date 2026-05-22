"""
ingest_earnings_calendar.py
===========================
Ingest the forward earnings calendar (next 14 days + trailing 14 for
post-event analysis) to /root/openclaw/data/master/earnings_calendar.parquet.

Strategy that depends on this file
---------------------------------
* S-HV17 Earnings Straddle Fade → next_earnings_date (per ticker,
  within 1-2 trading days) to identify straddles pricing a rich implied
  move relative to historical post-earnings moves.

Schema (long-format):
    ticker (str)
    next_earnings_date (date)
    time (str)           — 'bmo', 'amc', or ''
    eps_estimate (float, optional)
    revenue_estimate (float, optional)
    fiscal_period (str, optional)

Data source
-----------
Yahoo Finance per-ticker `Ticker.calendar` API. The FMP `earning_calendar`
bulk endpoint returns 403 on the current plan tier, so we fan out across
the active universe (read from prices.parquet) and call yfinance once
per ticker to retrieve the next upcoming earnings date and consensus
estimates. Slower than a single bulk call but uses no paid API quota.

Usage
-----
    python ingest_earnings_calendar.py                  # ±14d window
    python ingest_earnings_calendar.py --window 30      # ±30d window
    python ingest_earnings_calendar.py --dry-run

Author: Claude / FundJohn research, 2026-04-23.
"""
from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

DATA_DIR = Path("/root/openclaw/data/master")
OUT_PATH = DATA_DIR / "earnings_calendar.parquet"
PRICES_PATH = DATA_DIR / "prices.parquet"


def _load_universe(universe_file: str | None) -> list[str]:
    if universe_file and Path(universe_file).exists():
        with open(universe_file) as f:
            return sorted({ln.strip() for ln in f
                           if ln.strip() and not ln.startswith('#')})
    if PRICES_PATH.exists():
        df = pd.read_parquet(PRICES_PATH, columns=['ticker'])
        return sorted(df['ticker'].astype(str).unique().tolist())
    return []


def _earliest_future_date(raw, today: date) -> date | None:
    """yfinance returns 'Earnings Date' as either a single date or a list of
    candidate dates (often two — bmo/amc rumors). Return the earliest
    forward-looking date, or None if all are in the past."""
    if raw is None:
        return None
    candidates = raw if isinstance(raw, (list, tuple)) else [raw]
    fwd = []
    for c in candidates:
        try:
            d = c.date() if hasattr(c, 'date') else pd.to_datetime(c).date()
        except Exception:
            continue
        if d >= today:
            fwd.append(d)
    return min(fwd) if fwd else None


def yfinance_calendar(tickers: list[str], today: date,
                      throttle_s: float = 0.25) -> pd.DataFrame:
    """Per-ticker Ticker(t).calendar fan-out via cboe_vol_indices.

    SP-1: yfinance access is routed through src/ingestion/cboe_vol_indices
    (sole allowed importer). throttle_s is passed for CLI compat but
    throttling now happens inside get_forward_earnings_calendar."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from src.ingestion.cboe_vol_indices import get_forward_earnings_calendar

    raw_df = get_forward_earnings_calendar(tickers)

    rows: list[dict] = []
    failed = 0
    for _, row in raw_df.iterrows():
        t = row['ticker']
        cal = row['calendar']
        if not cal:
            continue
        nxt = _earliest_future_date(cal.get('Earnings Date'), today)
        if not nxt:
            continue
        rows.append({
            'ticker':            t,
            'next_earnings_date': nxt,
            'time':              '',  # yfinance doesn't expose bmo/amc
            'eps_estimate':      cal.get('Earnings Average'),
            'revenue_estimate':  cal.get('Revenue Average'),
            'fiscal_period':     None,
        })

    failed = len(tickers) - len(raw_df)
    if failed > 0:
        print(f"  [yf] {failed}/{len(tickers)} tickers returned no calendar data", file=sys.stderr)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return df.sort_values(['ticker', 'next_earnings_date']).reset_index(drop=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--window', type=int, default=14,
                   help='± days around today (kept for CLI compat; yfinance returns one '
                        'forward date per ticker regardless)')
    p.add_argument('--universe-file', default=None,
                   help='Optional txt file with tickers to retain (default = prices.parquet universe)')
    p.add_argument('--throttle', type=float, default=0.25,
                   help='Seconds to sleep between yfinance ticker calls (default 0.25)')
    p.add_argument('--limit', type=int, default=None,
                   help='Cap how many tickers to fetch (debug aid)')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today()

    universe = _load_universe(args.universe_file)
    if args.limit:
        universe = universe[:args.limit]
    print(f"earnings_calendar ingest — yfinance per-ticker, universe size {len(universe)}")

    df = yfinance_calendar(universe, today, throttle_s=args.throttle)
    print(f"  upcoming events : {len(df):,}")

    if args.dry_run:
        print("  --dry-run: not writing")
        return

    df.to_parquet(OUT_PATH, index=False)
    print(f"  wrote → {OUT_PATH}")


if __name__ == "__main__":
    main()
