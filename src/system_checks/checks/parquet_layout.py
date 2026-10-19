"""Storage-tagged check: prices.parquet row groups must stay date-tight.

Row groups spanning years defeat min/max date pruning. Evaluated from the
parquet footer only (zero data read). A FAIL means the master needs the A1
re-sort. page_index is informational (the daily DuckDB COPY append never
writes one), so it never affects the verdict.
"""
from __future__ import annotations

import os
from pathlib import Path

from ..registry import check
from ..types import Status

_MASTER_DIR = Path(os.environ.get('OPENCLAW_MASTER_DIR',
                                  '/root/openclaw/data/master'))


@check(name='prices_rowgroup_span', tags=['storage'], requires=['fs'])
def _prices_rowgroup_span():
    path = _MASTER_DIR / 'prices.parquet'
    if not path.exists():
        return Status.SKIP, f'no {path}'
    try:
        from src.data.parquet_layout import layout_verdict, rowgroup_span_report
    except ImportError:  # run with src/ on the path
        from data.parquet_layout import layout_verdict, rowgroup_span_report  # type: ignore
    verdict, detail = layout_verdict(rowgroup_span_report(path))
    if verdict == 'FAIL':
        detail += ('; fix = controller-run scripts/resort_prices_master.py (R1); '
                   'do not auto-run')
    return Status[verdict], detail
