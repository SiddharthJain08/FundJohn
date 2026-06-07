"""SP-7 Phase B Task 14 — 12-week sentinel decision logic."""
from __future__ import annotations
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from scripts import check_ladder_saturday as cls


def test_due_when_never_run():
    assert cls.is_due(None, today=date(2026, 6, 6)) is True


def test_due_at_84_days():
    assert cls.is_due('2026-03-14', today=date(2026, 6, 6)) is True   # 84d
    assert cls.is_due('2026-03-15', today=date(2026, 6, 6)) is False  # 83d


def test_garbage_value_is_due():
    assert cls.is_due('not-a-date', today=date(2026, 6, 6)) is True
