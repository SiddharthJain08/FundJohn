"""Keeper policy for execution_signals corruption-twin repair (2026-06-04).

All 97 dup groups have BOTH twins carrying signal_pnl children (the corrupt
unique index let the engine double-insert on 05-13/20/22 and P&L tracked both).
Keeper = the twin the system is still actively marking: greatest
(max_pnl_date, n_children); tiebreak = oldest created_at (the original row —
the later insert is the corruption artifact).
"""
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / 'scripts')):
    if p not in sys.path:
        sys.path.insert(0, p)

from repair_execution_signals_dups import pick_keeper  # noqa: E402


def _twin(id_, created, mx, n):
    return {'id': id_, 'created_at': created, 'max_pnl_date': mx, 'n_children': n}


def test_keeper_is_actively_marked_twin():
    a = _twin('A', datetime(2026, 5, 13, 15, 8), date(2026, 6, 3), 14)
    b = _twin('B', datetime(2026, 5, 13, 15, 43), date(2026, 5, 19), 4)
    assert pick_keeper([a, b])['id'] == 'A'
    assert pick_keeper([b, a])['id'] == 'A'  # order-independent


def test_keeper_by_children_count_when_same_max_date():
    a = _twin('A', datetime(2026, 5, 13, 15, 8), date(2026, 5, 19), 3)
    b = _twin('B', datetime(2026, 5, 13, 15, 43), date(2026, 5, 19), 7)
    assert pick_keeper([a, b])['id'] == 'B'


def test_tiebreak_keeps_original_oldest_row():
    a = _twin('A', datetime(2026, 5, 13, 15, 8), date(2026, 5, 19), 4)
    b = _twin('B', datetime(2026, 5, 13, 15, 43), date(2026, 5, 19), 4)
    assert pick_keeper([a, b])['id'] == 'A'


def test_childless_twin_never_beats_childed():
    a = _twin('A', datetime(2026, 5, 13, 15, 8), None, 0)
    b = _twin('B', datetime(2026, 5, 13, 15, 43), date(2026, 5, 14), 1)
    assert pick_keeper([a, b])['id'] == 'B'
