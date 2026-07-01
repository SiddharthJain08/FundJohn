"""tests/test_rebacktest_runner.py — re-backtest harness pure helpers (Phase 1b)."""
from __future__ import annotations
import sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'scripts'))
import rebacktest_runner as rr  # noqa: E402

MANIFEST = {'strategies': {
    'S_live_a': {'state': 'live'}, 'S_cand_b': {'state': 'candidate'},
    'S_dep_c': {'state': 'deprecated'},
    # S_orphan_d intentionally absent from the manifest
}}
PRIMARY = {'S_live_a', 'S_cand_b', 'S_dep_c', 'S_orphan_d'}


class TestWorklist(unittest.TestCase):
    def test_default_excludes_deprecated_and_orphans(self):
        wl = rr.build_worklist(PRIMARY, MANIFEST)
        self.assertEqual({w['sid'] for w in wl}, {'S_live_a', 'S_cand_b'})
        self.assertTrue(all(w['mode'] == 'strategy-id' for w in wl))

    def test_include_deprecated_adds_them(self):
        wl = rr.build_worklist(PRIMARY, MANIFEST, include_deprecated=True)
        self.assertEqual({w['sid'] for w in wl}, {'S_live_a', 'S_cand_b', 'S_dep_c', 'S_orphan_d'})
        modes = {w['sid']: w['mode'] for w in wl}
        self.assertEqual(modes['S_orphan_d'], 'strategy-file')  # orphan -> file mode
        self.assertEqual(modes['S_dep_c'], 'strategy-id')

    def test_only_and_exclude(self):
        self.assertEqual({w['sid'] for w in rr.build_worklist(PRIMARY, MANIFEST, only=['S_live_a'])},
                         {'S_live_a'})
        self.assertEqual({w['sid'] for w in rr.build_worklist(PRIMARY, MANIFEST, exclude=['S_live_a'])},
                         {'S_cand_b'})


class TestIsDone(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    def test_fresh_run_is_done(self):
        self.assertTrue(rr.is_done(self.start + timedelta(hours=1), self.start))
    def test_stale_run_not_done(self):
        self.assertFalse(rr.is_done(self.start - timedelta(hours=1), self.start))
    def test_missing_run_not_done(self):
        self.assertFalse(rr.is_done(None, self.start))


class TestCmd(unittest.TestCase):
    def test_cmd_has_flag_limits_and_sid(self):
        cmd = rr.build_systemd_cmd({'sid': 'S_live_a', 'mode': 'strategy-id'},
                                   memory_max_g=4, watchdog_sec=5400, log_path='/tmp/x.log')
        j = ' '.join(cmd)
        self.assertIn('OPENCLAW_TRUE_MTM_MARKS=1', j)
        self.assertIn('MemoryMax=4G', j)
        self.assertIn('RuntimeMaxSec=5400', j)
        self.assertIn('--strategy-id', cmd); self.assertIn('S_live_a', cmd)
        # Pin the safety-critical flags: dropping --wait would make subprocess.run
        # non-blocking -> the sequential loop fires all units at once -> OOM.
        self.assertIn('--wait', cmd)
        self.assertIn('--quiet', cmd)
        self.assertIn('--collect', cmd)
        self.assertTrue(any(c.startswith('EnvironmentFile=') for c in cmd))
        self.assertIn('/usr/bin/env', cmd)               # process-level flag override
        self.assertTrue(any(c.startswith('PYTHONPATH=') for c in cmd))
        self.assertTrue(any(c.startswith('WorkingDirectory=') for c in cmd))
    def test_orphan_uses_strategy_file(self):
        cmd = rr.build_systemd_cmd({'sid': 'S_orphan_d', 'mode': 'strategy-file'},
                                   memory_max_g=4, watchdog_sec=5400, log_path='/tmp/x.log')
        self.assertIn('--strategy-file', cmd)
        self.assertTrue(any('S_orphan_d.py' in c for c in cmd))


class TestSummarize(unittest.TestCase):
    def test_tally(self):
        s = rr.summarize([{'sid': 'a', 'status': 'ok'}, {'sid': 'b', 'status': 'fail'},
                          {'sid': 'c', 'status': 'ok'}, {'sid': 'd', 'status': 'skip'}])
        self.assertEqual((s['ok'], s['fail'], s['skip']), (2, 1, 1))
        self.assertEqual(s['failed_sids'], ['b'])


class TestMainDryRun(unittest.TestCase):
    def test_dry_run_launches_no_unit_and_persists_no_state(self):
        # main(--dry-run) must NEVER call subprocess.run (would launch a real
        # backtest unit) and must NOT write state.json (would poison a later
        # real run's resume timestamp).
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(rr, '_connect', return_value=mock.MagicMock()), \
             mock.patch.object(rr, '_primary_sids', return_value={'S_live_a'}), \
             mock.patch.object(rr, '_latest_primary_run_at', return_value=None), \
             mock.patch.object(rr, '_load_manifest',
                               return_value={'strategies': {'S_live_a': {'state': 'live'}}}), \
             mock.patch.object(rr, '_competing_active', return_value=False), \
             mock.patch.object(rr.subprocess, 'run',
                               side_effect=AssertionError('no unit may launch in --dry-run')):
            rc = rr.main(['--dry-run', '--log-dir', td, '--only', 'S_live_a'])
            self.assertEqual(rc, 0)
            self.assertFalse((Path(td) / 'state.json').exists())


if __name__ == '__main__':
    unittest.main()
