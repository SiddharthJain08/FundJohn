"""SP-7 Phase B Task 14 — 12-week sentinel decision logic."""
from __future__ import annotations
import subprocess
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def test_due_and_gate_false_no_seed():
    """due=True + coherence gate False → seed subprocess must not be called."""
    calls = []
    with patch.object(cls, 'is_due', return_value=True), \
         patch.object(cls, '_coherence_green', return_value=False), \
         patch('scripts.check_ladder_saturday.subprocess.run',
               side_effect=lambda cmd, **kw: calls.append(cmd) or MagicMock(returncode=0)), \
         patch.dict('sys.modules', {'redis': MagicMock()}), \
         patch('sys.argv', ['check_ladder_saturday.py']):
        rc = cls.main()
    assert rc == 0
    assert not calls, f'seed was called unexpectedly: {calls}'


def test_due_and_gate_true_seed_called():
    """due=True + coherence gate True → seed subprocess must be called once."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0)

    with patch.object(cls, 'is_due', return_value=True), \
         patch.object(cls, '_coherence_green', return_value=True), \
         patch('scripts.check_ladder_saturday.subprocess.run',
               side_effect=fake_run), \
         patch.dict('sys.modules', {'redis': MagicMock()}), \
         patch('sys.argv', ['check_ladder_saturday.py']):
        rc = cls.main()
    assert rc == 0
    assert len(calls) == 1
    assert 'seed' in calls[0]
