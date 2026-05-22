"""Yahoo Finance backfiller — stock OHLCV, vol indices, earnings calendar.

Supported column_name values:
  prices, price_data, close, volume, open, high, low      → bulk universe prices
  VIX, VVIX, VIX3M                                        → individual vol-index series
                                                            into macro.parquet
  vol_indices                                             → wide-format
                                                            vol_indices.parquet (^VIX, ^VVIX, ^VIX9D)
  earnings_calendar                                       → forward earnings calendar
                                                            via yfinance per-ticker

SP-1 note: earnings (historical) backfill moved to FMP-only path.
  Use scripts/backfill_earnings.py or src/pipeline/backfillers/fmp.py
  for historical earnings data. The yfinance _backfill_earnings path has
  been removed per SP-1 provider cutover.
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

# SP-1: EARNINGS_COLUMNS removed — earnings backfill is FMP-only.
# Callers should use scripts/backfill_earnings.py or src/pipeline/backfillers/fmp.py.

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
    if column_name in DELEGATED_INGEST_SCRIPTS:
        return _delegate_to_ingest_script(
            DELEGATED_INGEST_SCRIPTS[column_name],
            ROOT / 'data' / 'master' / f'{column_name}.parquet',
        )
    # SP-1: earnings backfill is FMP-only (see scripts/backfill_earnings.py).
    _fmp_earnings = {'earnings', 'earnings_surprise_pct', 'earnings_date'}
    if column_name in _fmp_earnings:
        raise NotImplementedError(
            f'SP-1: yfinance earnings backfill removed; use FMP path '
            f'(scripts/backfill_earnings.py or src/pipeline/backfillers/fmp.py)'
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


def _backfill_vol_index(name: str, from_date: date, to_date: date) -> int:
    # Delegate to cboe_vol_indices.main() — it is incremental and idempotent
    # by (series_name, date). For deep history backfills (no existing rows) it
    # uses BACKFILL_DAYS (5 years) which covers our needs.
    import importlib
    mod = importlib.import_module('src.ingestion.cboe_vol_indices')
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
