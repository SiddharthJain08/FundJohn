"""Polygon / Massive backfiller — options EOD chains, live flow.

Phase 1 stub. Real implementation reuses
src/ingestion/massive_client.download_options_day_bars(trade_date) with a loop
over the date range. Wiring follows in Phase 2.
"""
from datetime import date


def backfill(column_name: str, from_date: date, to_date: date) -> int:
    raise NotImplementedError(
        f'polygon backfiller: column={column_name!r}. Loop '
        f'massive_client.download_options_day_bars over {from_date}..{to_date}.'
    )
