"""tests/test_executor_handoff_freshness.py — execute-phase handoff freshness gate."""
from __future__ import annotations
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import alpaca_executor as ae  # noqa: E402

ET = ZoneInfo('America/New_York')


class TestHandoffFreshness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, date_str):
        p = self.dir / f'{date_str}_sized.json'
        p.write_text('{"orders": []}')
        return p

    def test_missing_handoff_not_fresh(self):
        ok, why = ae._handoff_fresh('2026-05-27', _dir=self.dir)
        self.assertFalse(ok)
        self.assertIn('missing', why)

    def test_written_today_is_fresh(self):
        now = datetime.now(ET)
        self._write(now.strftime('%Y-%m-%d'))
        ok, why = ae._handoff_fresh(now.strftime('%Y-%m-%d'), _dir=self.dir, _now=now)
        self.assertTrue(ok, why)

    def test_stale_file_not_fresh(self):
        now = datetime.now(ET)
        p = self._write('2026-05-27')
        old = (now - timedelta(days=2)).timestamp()
        os.utime(p, (old, old))
        ok, why = ae._handoff_fresh('2026-05-27', _dir=self.dir, _now=now)
        self.assertFalse(ok)
        self.assertIn('stale', why)


if __name__ == '__main__':
    unittest.main()
