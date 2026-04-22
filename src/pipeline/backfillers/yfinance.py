"""Yahoo Finance backfiller — stock OHLCV and vol indices.

Supported column_name values:
  prices, price_data, close, volume, open, high, low      → bulk universe prices
  VIX, VVIX, VIX3M                                        → vol index daily close
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


def backfill(column_name: str, from_date: date, to_date: date) -> int:
    if column_name in PRICE_COLUMNS:
        return _backfill_prices(from_date, to_date)
    if column_name in VOL_INDICES:
        return _backfill_vol_index(column_name, from_date, to_date)
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
    # Delegate to the existing fetch_vol_indices.main() — it is incremental
    # and idempotent by (series_name, date). For deep history backfills (no
    # existing rows) it uses BACKFILL_DAYS (5 years) which covers our needs.
    import importlib
    mod = importlib.import_module('ingestion.fetch_vol_indices')
    results = mod.main()
    return sum(v for v in results.values() if isinstance(v, int) and v > 0)
