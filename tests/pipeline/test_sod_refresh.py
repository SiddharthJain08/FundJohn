"""tests/test_sod_refresh.py — dashboard-only SOD opening-price ingest."""
from __future__ import annotations
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from pipeline import run_sod_refresh as sod  # noqa: E402


class TestSodRefresh(unittest.TestCase):
    def test_writes_dashboard_json_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / 'dashboard'
            fpath = sod.run(
                _universe_fn=lambda: ['AAPL', 'MSFT'],
                _fetch=lambda u: {'AAPL': 150.0, 'MSFT': 300.0},
                _out_dir=out_dir,
            )
            self.assertTrue(fpath.exists())
            data = json.loads(fpath.read_text())
            self.assertEqual(data['opens'], {'AAPL': 150.0, 'MSFT': 300.0})
            self.assertEqual(data['count'], 2)
            # only the dashboard JSON was written under the injected out_dir
            self.assertEqual([p.name for p in out_dir.iterdir()], [fpath.name])

    def test_empty_opens_still_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            fpath = sod.run(_universe_fn=lambda: [], _fetch=lambda u: {}, _out_dir=Path(tmp))
            self.assertTrue(fpath.exists())
            self.assertEqual(json.loads(fpath.read_text())['count'], 0)


if __name__ == '__main__':
    unittest.main()
