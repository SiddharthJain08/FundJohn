"""Tavily backfiller — news + transcripts.

Phase 1 stub. News is ephemeral — historical backfill is rarely meaningful;
the daily collector picks up fresh news from APPROVED rows via the normal
cycle. This stub accepts the call and reports zero rows to avoid blocking
the queue on unsupported history requests.
"""
from datetime import date


def backfill(column_name: str, from_date: date, to_date: date) -> int:
    print(f'  [tavily] no historical backfill for news (column={column_name}); '
          f'live collection will populate from the next cycle onward.')
    return 0
