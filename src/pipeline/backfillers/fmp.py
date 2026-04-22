"""FMP (Financial Modeling Prep) backfiller — financials, ratios, insider.

Phase 1 stub. Real implementation reuses src/ingestion/pipeline.py (the 3-layer
async ETL) with an explicit date window. Wiring follows in Phase 2.
"""
from datetime import date


def backfill(column_name: str, from_date: date, to_date: date) -> int:
    raise NotImplementedError(
        f'fmp backfiller: column={column_name!r}. Wire src/ingestion/pipeline.py '
        f'with explicit ({from_date}, {to_date}) window.'
    )
