"""SEC EDGAR backfiller — Form 4 insider, 10-K/Q/8-K filings.

Phase 1 stub. Real implementation reuses src/ingestion/edgar_client.py.
"""
from datetime import date


def backfill(column_name: str, from_date: date, to_date: date) -> int:
    raise NotImplementedError(
        f'edgar backfiller: column={column_name!r}. Wire edgar_client for '
        f'{from_date}..{to_date}.'
    )
