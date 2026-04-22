"""Alpha Vantage backfiller — macro / technical indicators.

Phase 1 stub.
"""
from datetime import date


def backfill(column_name: str, from_date: date, to_date: date) -> int:
    raise NotImplementedError(
        f'alphavantage backfiller: column={column_name!r}. Wire the AV client '
        f'for {from_date}..{to_date}.'
    )
