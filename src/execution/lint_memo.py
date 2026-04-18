"""
lint_memo.py — SO-6 memo format enforcement for the FundJohn pipeline.

Required fields (SO-6):
  strategy_id, run_timestamp, cycle_date, sharpe, max_drawdown,
  signal_count, top_signals

Usage:
  from execution.lint_memo import lint_memo, write_veto_rows
  ok, missing = lint_memo(memo_dict)
"""

from typing import Tuple, List

REQUIRED_FIELDS: List[str] = [
    'strategy_id',
    'run_timestamp',
    'cycle_date',
    'sharpe',
    'max_drawdown',
    'signal_count',
    'top_signals',
]


def lint_memo(memo: dict) -> Tuple[bool, List[str]]:
    """
    Validate memo against REQUIRED_FIELDS.
    Returns (ok, missing_fields). ok=True only when missing_fields is empty.
    """
    missing = [f for f in REQUIRED_FIELDS if memo.get(f) is None]
    return (len(missing) == 0), missing


def write_veto_rows(conn, run_date: str, strategy_id: str, missing_fields: List[str]) -> None:
    """Write one veto_log row per missing field. conn is a live psycopg2 connection."""
    with conn.cursor() as cur:
        for field in missing_fields:
            cur.execute(
                """
                INSERT INTO veto_log (run_date, strategy_id, veto_reason, field_name)
                VALUES (%s, %s, 'missing_field', %s)
                """,
                [run_date, strategy_id, field],
            )
    conn.commit()
