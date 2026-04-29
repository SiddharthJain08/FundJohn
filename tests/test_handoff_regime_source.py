"""tests/test_handoff_regime_source.py

Guards `trade_handoff_builder.load_regime` against the silent-fallback
class of bugs. History:

  - Pre-2026-04-29: queried a non-existent `regime_states` table, swallowed
    the exception, returned hard-coded TRANSITIONING with scale=0.55.
    On a LOW_VOL day this silently halved every Kelly-sized position.
  - 2026-04-29 (commit 479de45): repointed at `market_regime` DB to match
    engine.py.
  - 2026-04-29 (file-primary refactor): both engine and handoff now
    read `regime_latest.json` directly. The DB is append-only history.
    File-primary eliminates the file/DB drift class entirely — the
    HMM script writes the file atomically; whatever's on disk is what
    consumers see.

Run:
    pytest tests/test_handoff_regime_source.py -v
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import trade_handoff_builder as thb  # noqa: E402


def _write_regime_file(tmp_path: pathlib.Path, payload: dict, mtime_offset_hours=1):
    """Write a synthetic regime_latest.json into tmp_path and return its path."""
    f = tmp_path / 'regime_latest.json'
    f.write_text(json.dumps(payload))
    target = time.time() - (mtime_offset_hours * 3600)
    os.utime(str(f), (target, target))
    return f


class TestHandoffRegimeSource(unittest.TestCase):
    def setUp(self):
        # Each test gets its own tmp dir + module-level REGIME_LATEST_FILE
        # rebind. Tests must not leak across each other.
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = pathlib.Path(self._tmp.name)
        self._orig_path = thb.REGIME_LATEST_FILE

    def tearDown(self):
        thb.REGIME_LATEST_FILE = self._orig_path
        self._tmp.cleanup()
        os.environ.pop('OPENCLAW_ALLOW_STALE_REGIME', None)

    def test_reads_low_vol_with_scale_1(self):
        """Scale must be 1.0 in LOW_VOL — anything else silently
        downsizes positions (the exact 2026-04-29 impact)."""
        thb.REGIME_LATEST_FILE = _write_regime_file(self._tmp_path, {
            'state':        'LOW_VOL',
            'vix_level':    17.83,
            'features':     {'vix': 17.83},
            'stress_score': 39,
        })
        out = thb.load_regime()
        self.assertEqual(out['state'], 'LOW_VOL')
        self.assertEqual(out['scale'], 1.0)
        self.assertEqual(out['vix_level'], 17.83)
        self.assertEqual(out['stress'], 39.0)

    def test_reads_high_vol_with_scale_035(self):
        thb.REGIME_LATEST_FILE = _write_regime_file(self._tmp_path, {
            'state':        'HIGH_VOL',
            'vix_level':    28.0,
            'features':     {'vix': 28.0},
            'stress_score': 70,
        })
        out = thb.load_regime()
        self.assertEqual(out['state'], 'HIGH_VOL')
        self.assertEqual(out['scale'], 0.35)

    def test_stale_file_exits_2(self):
        """Mirror engine.load_regime: refuse to build a handoff under a
        regime file older than ENGINE_REGIME_FAIL_HOURS. Otherwise a
        days-stuck file would silently mis-stamp every cycle."""
        thb.REGIME_LATEST_FILE = _write_regime_file(
            self._tmp_path, {'state': 'TRANSITIONING'},
            mtime_offset_hours=200,
        )
        with self.assertRaises(SystemExit) as ctx:
            thb.load_regime()
        self.assertEqual(ctx.exception.code, 2)

    def test_stale_override_allows_run(self):
        """Backtest path: OPENCLAW_ALLOW_STALE_REGIME=1 lifts the gate."""
        thb.REGIME_LATEST_FILE = _write_regime_file(
            self._tmp_path, {'state': 'LOW_VOL', 'features': {'vix': 17.0}},
            mtime_offset_hours=200,
        )
        os.environ['OPENCLAW_ALLOW_STALE_REGIME'] = '1'
        out = thb.load_regime()
        self.assertEqual(out['state'], 'LOW_VOL')

    def test_missing_file_exits_2(self):
        """No file → no regime → must hard-fail. Don't fall back to a
        guessed default that would silently mis-scale positions."""
        thb.REGIME_LATEST_FILE = self._tmp_path / 'does_not_exist.json'
        with self.assertRaises(SystemExit) as ctx:
            thb.load_regime()
        self.assertEqual(ctx.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
