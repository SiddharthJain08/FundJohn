"""tests/test_engine_close_proxy_injection.py — load_prices close[t]-proxy injection."""
from __future__ import annotations
import os
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import engine  # noqa: E402
from ingestion import close_proxy_snapshot as cps  # noqa: E402
from ingestion.close_proxy_snapshot import CloseProxyError  # noqa: E402


class TestCloseProxyInjection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        md = Path(self.tmp.name) / 'data' / 'master'
        md.mkdir(parents=True)
        df = pd.DataFrame({
            'ticker': ['AAPL', 'AAPL', 'MSFT', 'MSFT'],
            'date': pd.to_datetime(['2026-05-25', '2026-05-26', '2026-05-25', '2026-05-26']),
            'close': [100.0, 101.0, 200.0, 201.0],
        })
        df.to_parquet(md / 'prices.parquet', index=False)
        self._old_root = engine.ROOT
        self._old_fetch = cps.fetch_close_proxy
        engine.ROOT = Path(self.tmp.name)
        os.environ.pop('OPENCLAW_CLOSE_PROXY_SNAPSHOT', None)

    def tearDown(self):
        engine.ROOT = self._old_root
        cps.fetch_close_proxy = self._old_fetch
        os.environ.pop('OPENCLAW_CLOSE_PROXY_SNAPSHOT', None)
        self.tmp.cleanup()

    def test_flag_off_is_eod_only(self):
        wide = engine.load_prices(['AAPL', 'MSFT'])
        self.assertEqual(list(wide.index.strftime('%Y-%m-%d')), ['2026-05-25', '2026-05-26'])

    def test_flag_on_injects_today_et_row(self):
        os.environ['OPENCLAW_CLOSE_PROXY_SNAPSHOT'] = '1'
        cps.fetch_close_proxy = lambda u, d: {'AAPL': 150.0, 'MSFT': 300.0}
        wide = engine.load_prices(['AAPL', 'MSFT'])
        today = pd.Timestamp.now(tz='America/New_York').normalize().tz_localize(None)
        self.assertEqual(wide.index.max(), today)
        self.assertEqual(wide.loc[today, 'AAPL'], 150.0)
        self.assertEqual(wide.loc[today, 'MSFT'], 300.0)

    def test_flag_on_fetch_error_propagates(self):
        os.environ['OPENCLAW_CLOSE_PROXY_SNAPSHOT'] = '1'

        def boom(u, d):
            raise CloseProxyError('fetch failed')

        cps.fetch_close_proxy = boom
        with self.assertRaises(CloseProxyError):
            engine.load_prices(['AAPL', 'MSFT'])


if __name__ == '__main__':
    unittest.main()
