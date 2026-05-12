"""Polygon / Massive backfiller — historical options EOD chains and 30-min
equity bars.

Supported column_name values:
  options_eod   → OPRA EOD chain via Massive S3 flatfiles (ticker, date,
                  expiry, strike, option_type, OHLCV). IV / greeks /
                  open-interest stay NaN — those come from the live websocket
                  capture (massive_ws.py), not the historical flatfile.
  prices_30m    → 30-minute equity bars via Polygon REST aggregates,
                  delegated to src/ingestion/ingest_prices_30m.main().
  iv_history    → derived ATM-30d IV history from existing options_eod.parquet,
                  delegated to src/ingestion/ingest_iv_history.main().

Behavior for options_eod:
  * Incremental — reads existing options_eod.parquet to discover dates
    already covered, only fetches business days in the requested window
    that are missing. Lets the staging worker pass the default 5-year
    range without redoing work that the live capture has already written.
  * Zero-row guard — if every fetched day returns 0 rows the function
    raises rather than reporting silent success. Catches S3 credential /
    bucket misconfigurations.
"""
from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'src' / 'ingestion'))


# OCC option symbol parser — mirrors src/ingestion/massive_ws.py::_parse_occ.
_OCC_RE = re.compile(r'^O:([A-Z.]+)(\d{6})([CP])(\d{8})$')


def _parse_occ(sym: str) -> Optional[Tuple[str, str, str, float]]:
    m = _OCC_RE.match((sym or '').upper())
    if not m:
        return None
    und, date6, cp, strike8 = m.groups()
    expiry = f'20{date6[:2]}-{date6[2:4]}-{date6[4:]}'
    return und, expiry, 'call' if cp == 'C' else 'put', int(strike8) / 1000.0


# Master parquet schema columns we populate from S3 OHLCV. IV / greeks /
# OI / bid / ask are left out of the per-row dict so write_options inserts
# them as NaN — matching the live-capture rows for any field the source
# doesn't provide.
_OPTIONS_OUT_COLS = ['ticker', 'date', 'expiry', 'strike', 'option_type',
                     'open', 'close', 'high', 'low', 'volume', 'transactions']

_FLUSH_EVERY_DAYS = 30

# Cap how far back the options_eod cold-fill will reach in a single run.
# Each fetched day is ~330k contracts and ~40s wall-clock; the staging
# worker kills the child at 30 min so going past ~45 days is unrealistic.
# For deeper history, run this module ad-hoc with explicit dates and
# OPENCLAW_OPTIONS_BACKFILL_DAYS bumped.
_OPTIONS_BACKFILL_DAYS_DEFAULT = 60


def backfill(column_name: str, from_date: date, to_date: date) -> int:
    if column_name == 'options_eod':
        return _backfill_options_eod(from_date, to_date)
    if column_name == 'prices_30m':
        return _delegate_to_ingest_script(
            'ingest_prices_30m',
            ROOT / 'data' / 'master' / 'prices_30m.parquet',
        )
    if column_name == 'iv_history':
        return _delegate_to_ingest_script(
            'ingest_iv_history',
            ROOT / 'data' / 'master' / 'iv_history.parquet',
        )
    raise NotImplementedError(
        f'polygon backfiller has no handler for column={column_name!r}. '
        f"Supported: 'options_eod', 'prices_30m', 'iv_history'."
    )


def _delegate_to_ingest_script(module_basename: str, out_parquet: Path,
                               extra_args: Optional[list] = None) -> int:
    """Spawn `python3 src/ingestion/<module_basename>.py [extra_args]` and
    return the parquet row delta. The ingest scripts use argparse and read
    sys.argv, so we invoke them via subprocess to avoid argv pollution from
    the parent backfill runner (`python3 -m src.pipeline.backfillers <id>`)."""
    import subprocess
    script = ROOT / 'src' / 'ingestion' / f'{module_basename}.py'
    if not script.exists():
        raise RuntimeError(f'ingest script not found: {script}')
    before = _row_count_safe(out_parquet)
    cmd = ['python3', str(script)] + list(extra_args or [])
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=25 * 60)
    if proc.stdout: print(proc.stdout, end='')
    if proc.stderr: print(proc.stderr, end='')
    if proc.returncode != 0:
        raise RuntimeError(
            f'{module_basename} exited {proc.returncode}: {proc.stderr.strip()[-400:]}'
        )
    after = _row_count_safe(out_parquet)
    return max(0, after - before)


def _row_count_safe(path) -> int:
    try:
        if path and Path(path).exists():
            return len(pd.read_parquet(path, columns=['date']))
    except Exception:
        pass
    return 0


def _backfill_options_eod(from_date: date, to_date: date) -> int:
    import os
    from data.parquet_store import write_options, OPTIONS_PATH  # type: ignore
    import massive_client  # type: ignore

    cap_days = int(os.environ.get('OPENCLAW_OPTIONS_BACKFILL_DAYS',
                                  _OPTIONS_BACKFILL_DAYS_DEFAULT))
    earliest = to_date - timedelta(days=cap_days)
    if from_date < earliest:
        print(f'  [polygon] clamping options_eod window from {from_date} to {earliest} '
              f'(OPENCLAW_OPTIONS_BACKFILL_DAYS={cap_days}). Daily live capture '
              f'(massive_ws.py) keeps this column fresh; this backfiller fills gaps only.')
        from_date = earliest

    existing_dates: set[str] = set()
    if Path(OPTIONS_PATH).exists():
        try:
            df_existing = pd.read_parquet(OPTIONS_PATH, columns=['date'])
            existing_dates = set(df_existing['date'].astype(str).unique())
        except Exception as e:
            print(f'  [polygon] could not read existing options_eod ({e}); will fetch full window')

    target_days = []
    cur = from_date
    while cur <= to_date:
        if cur.weekday() < 5:  # business days
            target_days.append(cur)
        cur += timedelta(days=1)
    missing_days = [d for d in target_days if d.isoformat() not in existing_dates]

    print(f'  [polygon] options_eod: window={from_date}..{to_date} '
          f'business_days={len(target_days)} already_covered={len(target_days) - len(missing_days)} '
          f'to_fetch={len(missing_days)}')

    if not missing_days:
        return 0

    total_rows = 0
    days_with_rows = 0
    batch: list[dict] = []
    for i, d in enumerate(missing_days, 1):
        ds = d.isoformat()
        try:
            df = massive_client.download_options_day_bars(ds)
        except Exception as e:
            print(f'  [polygon] {ds} download error: {e}')
            df = pd.DataFrame()

        if df.empty:
            continue

        days_with_rows += 1
        sym_col = 'ticker' if 'ticker' in df.columns else None
        if sym_col is None:
            print(f'  [polygon] {ds}: no ticker column in source — skipping')
            continue

        for row in df.itertuples(index=False):
            r = row._asdict()
            occ = r.get(sym_col)
            parsed = _parse_occ(occ)
            if not parsed:
                continue
            und, expiry, opt_type, strike = parsed
            close_px = r.get('close')
            batch.append({
                'ticker':       und,
                'date':         ds,
                'expiry':       expiry,
                'strike':       strike,
                'option_type':  opt_type,
                'market_price': close_px,
                'open':         r.get('open'),
                'close':        close_px,
                'high':         r.get('high'),
                'low':          r.get('low'),
                'volume':       r.get('volume'),
                'transactions': r.get('transactions'),
            })

        if i % _FLUSH_EVERY_DAYS == 0 and batch:
            write_options(batch)
            total_rows += len(batch)
            print(f'  [polygon] flushed {len(batch)} rows ({i}/{len(missing_days)} days)')
            batch = []

    if batch:
        write_options(batch)
        total_rows += len(batch)

    if days_with_rows == 0:
        raise RuntimeError(
            f'options_eod backfill fetched {len(missing_days)} days but every '
            f'response was empty — check Massive S3 credentials '
            f'(MASSIVE_ACCESS_KEY_ID / MASSIVE_SECRET_KEY) and bucket access.'
        )
    print(f'  [polygon] options_eod backfill complete — {total_rows} rows over '
          f'{days_with_rows}/{len(missing_days)} fetched days')
    return total_rows
