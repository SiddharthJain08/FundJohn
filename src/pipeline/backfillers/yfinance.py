"""Yahoo Finance backfiller — stock OHLCV, vol indices, earnings calendar.

Supported column_name values:
  prices, price_data, close, volume, open, high, low      → bulk universe prices
  VIX, VVIX, VIX3M                                        → individual vol-index series
                                                            into macro.parquet
  vol_indices                                             → wide-format
                                                            vol_indices.parquet (^VIX, ^VVIX, ^VIX9D)
  earnings_calendar                                       → forward earnings calendar
                                                            from yfinance per-ticker
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'workspaces' / 'default' / 'tools'))
sys.path.insert(0, str(ROOT / 'src'))


PRICE_COLUMNS = {'prices', 'price_data', 'close', 'open', 'high', 'low', 'volume'}
VOL_INDICES   = {'VIX', 'VVIX', 'VIX3M'}

# Per-ticker quarterly EPS surprises + revenue. FMP's /earnings-surprises and
# /earning_calendar endpoints both 404/403 on the current Massive plan, so
# `earnings` was retargeted from FMP to yfinance 2026-04-30. yfinance returns
# the last 4 quarters of EPS actual/estimate via Ticker.earnings_history and
# revenue via Ticker.quarterly_income_stmt['Total Revenue'].
EARNINGS_COLUMNS = {'earnings', 'earnings_surprise_pct', 'earnings_date'}

# Columns delegated to standalone src/ingestion/ingest_*.py scripts via
# subprocess — their argparse-driven main() reads sys.argv, which the
# `python3 -m src.pipeline.backfillers <id>` parent has already populated
# with non-script args. Subprocess isolation avoids the conflict.
DELEGATED_INGEST_SCRIPTS = {
    'vol_indices':       'ingest_vol_indices',
    'earnings_calendar': 'ingest_earnings_calendar',
}


def backfill(column_name: str, from_date: date, to_date: date) -> int:
    if column_name in PRICE_COLUMNS:
        return _backfill_prices(from_date, to_date)
    if column_name in VOL_INDICES:
        return _backfill_vol_index(column_name, from_date, to_date)
    if column_name in EARNINGS_COLUMNS:
        return _backfill_earnings()
    if column_name in DELEGATED_INGEST_SCRIPTS:
        return _delegate_to_ingest_script(
            DELEGATED_INGEST_SCRIPTS[column_name],
            ROOT / 'data' / 'master' / f'{column_name}.parquet',
        )
    raise NotImplementedError(
        f'yfinance backfiller has no handler for column={column_name!r}'
    )


def _backfill_prices(from_date: date, to_date: date) -> int:
    # Reuse the existing bulk downloader — it already handles universe, parquet
    # merge, and the yfinance MultiIndex reshape.
    from master_dataset import refresh_prices_bulk
    total = 0
    cur = from_date
    while cur <= to_date:
        try:
            n = refresh_prices_bulk(cur)
            total += int(n or 0)
        except Exception as e:
            print(f'  [yfinance] {cur} failed: {e}')
        # master_dataset writes one trading day per call; step one day.
        cur = date.fromordinal(cur.toordinal() + 1)
    return total


def _active_universe():
    """Active tickers for fan-out backfills. Prefers universe_config table,
    falls back to prices.parquet."""
    import os
    try:
        import psycopg2
        conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT ticker FROM universe_config WHERE active = TRUE ORDER BY ticker")
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        if rows:
            return rows
    except Exception as e:
        print(f'  [yfinance] universe_config unreachable ({e}); falling back to prices.parquet')
    import pandas as pd
    df = pd.read_parquet(ROOT / 'data' / 'master' / 'prices.parquet', columns=['ticker'])
    return sorted(df['ticker'].astype(str).unique().tolist())


def _backfill_earnings() -> int:
    """Per-ticker quarterly EPS + revenue via yfinance. Writes to
    earnings.parquet (ticker, date, eps_actual, eps_estimated,
    revenue_actual, revenue_estimated). yfinance doesn't expose forward
    revenue consensus — `revenue_estimated` stays NaN, matching the
    pre-existing parquet's mixed-fill state."""
    import time
    import pandas as pd
    import sys as _sys
    sys.path.insert(0, str(ROOT / 'src'))
    from data.parquet_store import MASTER_DIR, append_dedup

    try:
        import yfinance as yf
    except ImportError as e:
        raise RuntimeError(f'yfinance not installed: {e}')

    earn_path = MASTER_DIR / 'earnings.parquet'
    tickers = _active_universe()
    print(f'  [yfinance] earnings: {len(tickers)} tickers')

    rows = []
    failed = 0
    for i, ticker in enumerate(tickers, 1):
        try:
            tk = yf.Ticker(ticker)
            hist = tk.earnings_history
        except Exception as e:
            failed += 1
            if failed <= 5:
                print(f'  [yfinance] {ticker} earnings_history: {e}', file=_sys.stderr)
            time.sleep(1.0)
            continue
        if hist is None or hist.empty:
            time.sleep(1.0)
            continue

        # quarterly_income_stmt is optional — used only for revenue join.
        # Several tickers (ETFs, indices) raise here; fall through to
        # revenue=NaN rather than skipping the EPS rows.
        revenue_by_q = {}
        try:
            qi = tk.quarterly_income_stmt
            if qi is not None and not qi.empty and 'Total Revenue' in qi.index:
                rev = qi.loc['Total Revenue']
                for q, v in rev.items():
                    revenue_by_q[pd.Timestamp(q).date().isoformat()] = float(v) if v == v else None
        except Exception:
            pass

        today_iso = pd.Timestamp.today().date().isoformat()
        for q, r in hist.iterrows():
            try:
                d_ts = pd.Timestamp(q)
                d_iso = d_ts.date().isoformat()
            except Exception:
                continue
            rows.append({
                'ticker':             ticker,
                'date':               d_ts,        # match existing parquet datetime64[us] dtype
                'eps_actual':         _safe_float(r.get('epsActual')),
                'eps_estimated':      _safe_float(r.get('epsEstimate')),
                'revenue_actual':     revenue_by_q.get(d_iso),
                'revenue_estimated':  None,  # yfinance has no forward consensus
                'last_updated':       today_iso,   # match existing schema
            })

        if i % 25 == 0:
            print(f'  [yfinance] earnings progress: {i}/{len(tickers)} tickers, {len(rows)} rows')
        time.sleep(1.0)

    if not rows:
        print(f'  [yfinance] no earnings rows fetched (failed={failed}/{len(tickers)})')
        return 0
    df = pd.DataFrame(rows)
    total = append_dedup(earn_path, df, ['ticker', 'date'], mode='replace')
    print(f'  [yfinance] earnings merged — {len(rows)} rows written, total={total}, failed={failed}')
    return len(rows)


def _safe_float(v):
    try:
        f = float(v)
        return f if f == f else None  # NaN check
    except (TypeError, ValueError):
        return None


def _backfill_vol_index(name: str, from_date: date, to_date: date) -> int:
    # Delegate to the existing fetch_vol_indices.main() — it is incremental
    # and idempotent by (series_name, date). For deep history backfills (no
    # existing rows) it uses BACKFILL_DAYS (5 years) which covers our needs.
    import importlib
    mod = importlib.import_module('ingestion.fetch_vol_indices')
    results = mod.main()
    return sum(v for v in results.values() if isinstance(v, int) and v > 0)


def _delegate_to_ingest_script(module_basename: str, out_parquet) -> int:
    """Spawn `python3 src/ingestion/<module_basename>.py` and return the
    parquet row delta. The ingest scripts use argparse and read sys.argv,
    so subprocess avoids argv pollution from the parent
    `python3 -m src.pipeline.backfillers <id>` runner."""
    import subprocess
    import pandas as pd
    script = ROOT / 'src' / 'ingestion' / f'{module_basename}.py'
    if not script.exists():
        raise RuntimeError(f'ingest script not found: {script}')

    def _row_count(p):
        try:
            return len(pd.read_parquet(p)) if Path(p).exists() else 0
        except Exception:
            return 0

    before = _row_count(out_parquet)
    proc = subprocess.run(['python3', str(script)],
                          cwd=str(ROOT), capture_output=True, text=True, timeout=25 * 60)
    if proc.stdout: print(proc.stdout, end='')
    if proc.stderr: print(proc.stderr, end='')
    if proc.returncode != 0:
        raise RuntimeError(
            f'{module_basename} exited {proc.returncode}: {proc.stderr.strip()[-400:]}'
        )
    after = _row_count(out_parquet)
    return max(0, after - before)
