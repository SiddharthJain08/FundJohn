"""tests/test_handoff_regime_source.py

Guards `trade_handoff_builder.load_regime` against the silent-fallback
bug discovered 2026-04-29: the function used to query `regime_states`
(a table that does not exist), let the exception fall through, and
return a hard-coded `TRANSITIONING` with scale=0.55. On a LOW_VOL day
that silently downsized every Kelly-sized trade to 55% of intent.

Single-source rule: handoff and engine.py must read the *same* table
(`market_regime`) so a healthy DB can never produce divergent regime
labels in the same cycle.

Run:
    pytest tests/test_handoff_regime_source.py -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import trade_handoff_builder as thb  # noqa: E402


def _fake_conn(rows):
    """Return a psycopg2.connect mock that yields a cursor producing `rows`."""
    cur = MagicMock()
    cur.fetchone.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


class TestHandoffRegimeSource(unittest.TestCase):
    def test_reads_low_vol_with_scale_1(self):
        """Scale must be 1.0 in LOW_VOL — anything else silently
        downsizes positions (the exact 2026-04-29 impact)."""
        row = {
            'state': 'LOW_VOL',
            'vix_level': 17.83,
            'vix_percentile': 56.6,
            'regime_data': {'stress_score': 39},
            'updated_at': datetime.now(timezone.utc) - timedelta(hours=1),
        }
        with patch.object(thb.psycopg2, 'connect', return_value=_fake_conn(row)):
            out = thb.load_regime('postgresql://x:y@h:5432/d')
        self.assertEqual(out['state'], 'LOW_VOL')
        self.assertEqual(out['scale'], 1.0)
        self.assertEqual(out['vix_level'], 17.83)

    def test_reads_high_vol_with_scale_035(self):
        row = {
            'state': 'HIGH_VOL', 'vix_level': 28.0, 'vix_percentile': 90.0,
            'regime_data': {}, 'updated_at': datetime.now(timezone.utc),
        }
        with patch.object(thb.psycopg2, 'connect', return_value=_fake_conn(row)):
            out = thb.load_regime('postgresql://x:y@h:5432/d')
        self.assertEqual(out['state'], 'HIGH_VOL')
        self.assertEqual(out['scale'], 0.35)

    def test_stale_regime_exits_2(self):
        """Mirror engine.load_regime: refuse to build a handoff under a
        regime that's older than ENGINE_REGIME_FAIL_HOURS. Otherwise a
        7-day-stuck DB row would silently mis-stamp every cycle."""
        row = {
            'state': 'TRANSITIONING', 'vix_level': 22.0, 'vix_percentile': 60.0,
            'regime_data': {},
            'updated_at': datetime.now(timezone.utc) - timedelta(hours=200),
        }
        with patch.object(thb.psycopg2, 'connect', return_value=_fake_conn(row)):
            with self.assertRaises(SystemExit) as ctx:
                thb.load_regime('postgresql://x:y@h:5432/d')
            self.assertEqual(ctx.exception.code, 2)

    def test_stale_override_allows_run(self):
        """Backtest path: OPENCLAW_ALLOW_STALE_REGIME=1 lifts the gate."""
        import os
        row = {
            'state': 'LOW_VOL', 'vix_level': 17.0, 'vix_percentile': 50.0,
            'regime_data': {},
            'updated_at': datetime.now(timezone.utc) - timedelta(hours=200),
        }
        os.environ['OPENCLAW_ALLOW_STALE_REGIME'] = '1'
        try:
            with patch.object(thb.psycopg2, 'connect', return_value=_fake_conn(row)):
                out = thb.load_regime('postgresql://x:y@h:5432/d')
            self.assertEqual(out['state'], 'LOW_VOL')
        finally:
            os.environ.pop('OPENCLAW_ALLOW_STALE_REGIME', None)

    def test_no_silent_transitioning_on_db_error(self):
        """The pre-fix bug: a non-existent table threw inside the try,
        was swallowed, returned hard-coded TRANSITIONING. The new code
        prints a [WARN] (cron alert-bubbler will catch) and still
        returns the safe default — we don't crash the cycle, but the
        operator now sees the failure instead of silently downsizing."""
        # Simulate the pre-fix failure: connect succeeds, query throws.
        cur = MagicMock()
        cur.execute.side_effect = Exception('relation "regime_states" does not exist')
        conn = MagicMock()
        conn.cursor.return_value = cur
        captured = []
        original_print = print
        def _capture(*a, **kw):
            captured.append(' '.join(str(x) for x in a))
            original_print(*a, **kw)
        with patch.object(thb.psycopg2, 'connect', return_value=conn), \
             patch('builtins.print', side_effect=_capture):
            out = thb.load_regime('postgresql://x:y@h:5432/d')
        self.assertEqual(out['state'], 'TRANSITIONING')   # safe default ok
        self.assertTrue(any('[WARN]' in line and 'regime' in line.lower()
                            for line in captured),
                        f'expected [WARN] surfaced for cron bubbler, got: {captured}')


if __name__ == '__main__':
    unittest.main()
