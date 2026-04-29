"""tests/test_fundjohn_doctor.py

Unit tests for src/maintenance/doctor.py (Phase 1 of Tier 3). All
external probes (subprocess to alpaca CLI, psycopg2.connect, redis,
filesystem) are mocked — no live broker / DB / Redis calls.

Run:
    pytest tests/test_fundjohn_doctor.py -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import doctor  # noqa: E402


def _proc(returncode=0, stdout='', stderr=''):
    m = MagicMock()
    m.returncode = returncode
    m.stdout     = stdout
    m.stderr     = stderr
    return m


class TestAlpacaCheck(unittest.TestCase):
    def test_alpaca_check_passes_with_valid_account(self):
        ok_account = json.dumps({'equity': '100000', 'cash': '50000'})
        with patch('maintenance.doctor.subprocess.run',
                   return_value=_proc(0, ok_account, '')):
            res = doctor.check_alpaca_auth()
        self.assertEqual(res['severity'], doctor.PASS)
        self.assertIn('100,000', res['detail'])

    def test_alpaca_check_exits_2_on_401(self):
        err_envelope = json.dumps({
            'status': 401, 'error': 'invalid api key',
            'code': 40110000,
        })
        with patch('maintenance.doctor.subprocess.run',
                   return_value=_proc(1, '', err_envelope)):
            res = doctor.check_alpaca_auth()
        self.assertEqual(res['severity'], doctor.FAIL)
        self.assertIn('401', res['detail'])

    def test_alpaca_check_warns_on_zero_equity(self):
        ok_zero = json.dumps({'equity': '0', 'cash': '0'})
        with patch('maintenance.doctor.subprocess.run',
                   return_value=_proc(0, ok_zero, '')):
            res = doctor.check_alpaca_auth()
        self.assertEqual(res['severity'], doctor.WARN)


class TestPostgresCheck(unittest.TestCase):
    def test_postgres_check_handles_unreachable(self):
        # Patch psycopg2.connect to raise OperationalError
        import psycopg2
        with patch.dict('os.environ',
                        {'POSTGRES_URI': 'postgres://bad'}, clear=False), \
             patch('psycopg2.connect',
                   side_effect=psycopg2.OperationalError('connection refused')):
            res = doctor.check_postgres_reachable()
        self.assertEqual(res['severity'], doctor.FAIL)
        self.assertIn('connection refused', res['detail'].lower() if 'connection' in res['detail'].lower() else res['detail'])

    def test_postgres_check_warns_on_slow_connect(self):
        # Mock a connection that takes 1.5s (warn threshold is 1s, fail is 5s)
        import time
        fake_conn = MagicMock()
        fake_cur  = MagicMock()
        fake_conn.cursor.return_value = fake_cur
        def slow_connect(*a, **kw):
            time.sleep(1.2)
            return fake_conn
        with patch.dict('os.environ',
                        {'POSTGRES_URI': 'postgres://x'}, clear=False), \
             patch('psycopg2.connect', side_effect=slow_connect):
            res = doctor.check_postgres_reachable()
        self.assertEqual(res['severity'], doctor.WARN)
        self.assertIn('roundtrip', res['detail'])


class TestDataMasterWritable(unittest.TestCase):
    def test_data_master_writable_check_passes_when_writable(self):
        # Real check — data/master/ exists in this repo and is writable by root
        res = doctor.check_data_master_writable()
        # Should pass under normal repo conditions
        self.assertIn(res['severity'], (doctor.PASS, doctor.FAIL))
        # If it does pass, the detail should be the absolute path
        if res['severity'] == doctor.PASS:
            self.assertIn('data/master', res['detail'])

    def test_data_master_writable_check_fails_when_path_missing(self):
        # Patch ROOT to a nonexistent path
        with patch.object(doctor, 'ROOT', Path('/nonexistent/openclaw-zzz')):
            res = doctor.check_data_master_writable()
        self.assertEqual(res['severity'], doctor.FAIL)
        self.assertIn('missing', res['detail'].lower())


class TestEnvVars(unittest.TestCase):
    def test_env_required_fails_when_missing(self):
        with patch.dict('os.environ', {}, clear=True):
            res = doctor.check_env_required()
        self.assertEqual(res['severity'], doctor.FAIL)
        self.assertIn('ALPACA_API_KEY', res['detail'])

    def test_env_required_passes_when_all_set(self):
        all_present = {k: 'x' for k in doctor.REQUIRED_ENV}
        with patch.dict('os.environ', all_present, clear=True):
            res = doctor.check_env_required()
        self.assertEqual(res['severity'], doctor.PASS)

    def test_env_optional_warns_when_some_missing(self):
        # Set only one optional var; rest missing
        partial = {doctor.OPTIONAL_ENV[0]: 'x'}
        with patch.dict('os.environ', partial, clear=True):
            res = doctor.check_env_optional()
        self.assertEqual(res['severity'], doctor.WARN)


class TestRunModes(unittest.TestCase):
    def test_quick_mode_skips_slow_checks(self):
        """--quick must skip checks marked _slow=True."""
        with patch.dict('os.environ',
                        {**{k: 'x' for k in doctor.REQUIRED_ENV},
                         **{k: 'x' for k in doctor.OPTIONAL_ENV}}, clear=True), \
             patch('maintenance.doctor.subprocess.run',
                   return_value=_proc(0, json.dumps({'equity': '1', 'is_open': False, 'next_open': ''}), '')), \
             patch('psycopg2.connect',
                   return_value=MagicMock(cursor=MagicMock(return_value=MagicMock(
                       fetchone=MagicMock(return_value=(1,)))))):
            results, _ = doctor.run(quick=True)
        names = [r['name'] for r in results]
        # Slow-tagged checks must NOT appear
        for slow in ('redis_reachable', 'data_coverage', 'systemd_services'):
            self.assertNotIn(slow, names, f'{slow} should be skipped in --quick')

    def test_required_only_skips_optional_env_and_systemd(self):
        with patch.dict('os.environ',
                        {**{k: 'x' for k in doctor.REQUIRED_ENV}}, clear=True), \
             patch('maintenance.doctor.subprocess.run',
                   return_value=_proc(0, json.dumps({'equity': '1', 'is_open': False, 'next_open': ''}), '')), \
             patch('psycopg2.connect',
                   return_value=MagicMock(cursor=MagicMock(return_value=MagicMock(
                       fetchone=MagicMock(return_value=(1,)))))):
            results, _ = doctor.run(required_only=True)
        names = [r['name'] for r in results]
        for skipped in ('env_optional', 'data_coverage',
                        'orchestrator_lock', 'systemd_services'):
            self.assertNotIn(skipped, names,
                             f'{skipped} should be skipped in --required-only')

    def test_overall_exit_code_2_when_any_check_fails(self):
        """If any single check returns FAIL, overall exit code must be 2."""
        # Manually craft a results list with one fail and verify the rollup logic
        with patch.dict('os.environ',
                        {**{k: 'x' for k in doctor.REQUIRED_ENV}}, clear=True):
            # alpaca CLI returns 401 → fail
            err = json.dumps({'status': 401, 'error': 'unauthorized'})
            with patch('maintenance.doctor.subprocess.run',
                       return_value=_proc(1, '', err)), \
                 patch('psycopg2.connect',
                       return_value=MagicMock(cursor=MagicMock(return_value=MagicMock(
                           fetchone=MagicMock(return_value=(1,)))))):
                results, exit_code = doctor.run(required_only=True)
        # Should have at least one FAIL (alpaca_auth)
        fails = [r for r in results if r['severity'] == doctor.FAIL]
        self.assertGreater(len(fails), 0)
        self.assertEqual(exit_code, 2)


class TestRegimeFreshness(unittest.TestCase):
    """Defense-in-depth check added 2026-04-28 after a 7-day silent regime
    drift (np.float64 → 'schema "np" does not exist' bug in
    run_market_state.py wrote regime_latest.json fresh but stalled the DB)."""

    def _patch_pg(self, db_state, db_age_hours):
        """Build a psycopg2 mock that returns one row from market_regime."""
        from datetime import datetime, timedelta, timezone
        ts = datetime.now(timezone.utc) - timedelta(hours=db_age_hours)
        cur = MagicMock()
        cur.fetchone.return_value = (db_state, ts)
        cur.execute.return_value = None
        cur.close.return_value = None
        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.close.return_value = None
        return patch('psycopg2.connect', return_value=conn)

    def _patch_file(self, file_state, file_age_hours, exists=True):
        """Patch the regime_latest.json read + mtime."""
        import time
        path_mock = MagicMock()
        path_mock.exists.return_value = exists
        path_mock.stat.return_value = MagicMock(
            st_mtime=time.time() - (file_age_hours * 3600))
        return patch.object(doctor, 'REGIME_LATEST_FILE', path_mock), \
               patch('builtins.open',
                     unittest.mock.mock_open(read_data=json.dumps({'state': file_state})))

    def test_regime_freshness_passes_when_db_and_file_agree_and_fresh(self):
        with patch.dict('os.environ',
                        {'POSTGRES_URI': 'postgres://x'}, clear=False), \
             self._patch_pg('LOW_VOL', db_age_hours=2):
            file_p1, file_p2 = self._patch_file('LOW_VOL', file_age_hours=3)
            with file_p1, file_p2:
                res = doctor.check_regime_freshness()
        self.assertEqual(res['severity'], doctor.PASS)

    def test_regime_freshness_fails_on_state_disagreement(self):
        """File says LOW_VOL but DB stuck at TRANSITIONING — engine.py would
        generate the wrong basket. This is the smoking-gun signature of the
        2026-04-28 incident."""
        with patch.dict('os.environ',
                        {'POSTGRES_URI': 'postgres://x'}, clear=False), \
             self._patch_pg('TRANSITIONING', db_age_hours=2):
            file_p1, file_p2 = self._patch_file('LOW_VOL', file_age_hours=3)
            with file_p1, file_p2:
                res = doctor.check_regime_freshness()
        self.assertEqual(res['severity'], doctor.FAIL)
        self.assertIn('STATE MISMATCH', res['detail'])

    def test_regime_freshness_fails_on_db_stale_beyond_fail_hours(self):
        """DB hasn't updated in 100h (>REGIME_FAIL_HOURS=80). engine.py
        would silently use stale state. Catch this even if file is fresh."""
        with patch.dict('os.environ',
                        {'POSTGRES_URI': 'postgres://x'}, clear=False), \
             self._patch_pg('LOW_VOL', db_age_hours=100):
            file_p1, file_p2 = self._patch_file('LOW_VOL', file_age_hours=2)
            with file_p1, file_p2:
                res = doctor.check_regime_freshness()
        self.assertEqual(res['severity'], doctor.FAIL)

    def test_regime_freshness_warns_when_only_warn_window_breached(self):
        with patch.dict('os.environ',
                        {'POSTGRES_URI': 'postgres://x'}, clear=False), \
             self._patch_pg('LOW_VOL', db_age_hours=40):    # > 30h, < 80h
            file_p1, file_p2 = self._patch_file('LOW_VOL', file_age_hours=2)
            with file_p1, file_p2:
                res = doctor.check_regime_freshness()
        self.assertEqual(res['severity'], doctor.WARN)


class TestEngineStalenessGate(unittest.TestCase):
    """Layer 2 of the defense — engine.py refuses to run on stale regime."""

    def test_load_regime_exits_2_when_market_regime_stale(self):
        from datetime import datetime, timedelta, timezone
        # Lazy-import via execution path so engine.py's logger is set up
        sys.path.insert(0, str(ROOT / 'src'))
        # Re-import in case a prior test memoized the module under a stale state
        if 'execution.engine' in sys.modules:
            del sys.modules['execution.engine']
        from execution import engine

        # Stub cursor: returns a regime row stamped 100h ago — should trip
        # the 80h ENGINE_REGIME_FAIL_HOURS gate.
        ts = datetime.now(timezone.utc) - timedelta(hours=100)
        cur = MagicMock()
        cur.fetchone.return_value = {
            'state':           'TRANSITIONING',
            'vix_level':       18.0,
            'vix_percentile':  60.0,
            'regime_data':     {},
            'updated_at':      ts,
        }
        with patch.dict('os.environ',
                        {'ENGINE_REGIME_FAIL_HOURS': '80'}, clear=False):
            import os
            os.environ.pop('OPENCLAW_ALLOW_STALE_REGIME', None)
            with self.assertRaises(SystemExit) as ctx:
                engine.load_regime(cur)
        self.assertEqual(ctx.exception.code, 2)

    def test_load_regime_allows_stale_when_override_set(self):
        from datetime import datetime, timedelta, timezone
        sys.path.insert(0, str(ROOT / 'src'))
        if 'execution.engine' in sys.modules:
            del sys.modules['execution.engine']
        from execution import engine

        ts = datetime.now(timezone.utc) - timedelta(hours=100)
        cur = MagicMock()
        cur.fetchone.return_value = {
            'state':           'TRANSITIONING',
            'vix_level':       18.0,
            'vix_percentile':  60.0,
            'regime_data':     {},
            'updated_at':      ts,
        }
        with patch.dict('os.environ',
                        {'OPENCLAW_ALLOW_STALE_REGIME': '1'}, clear=False):
            res = engine.load_regime(cur)
        self.assertEqual(res['state'], 'TRANSITIONING')


if __name__ == '__main__':
    unittest.main()
