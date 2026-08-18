"""tests/strategies/test_auto_demote_registry_sync.py — 2026-08-18.

The engine trades on strategy_registry.status='approved', not on the manifest
state. Promotion is registry-first (promotion_service.js), but until 2026-08-18
the Python auto-demote path (auto_demote_negative_sharpe, called from
strategy_weights.rebuild) wrote ONLY the manifest — every auto-demotion of a
promoted strategy stranded a stale 'approved' registry row (15 accumulated by
08-18, the dashboard's "trading but not shown live" cohort).

Pins the fix:
  1. _sync_registry_demotion SQL shape — UPDATE strategy_registry to
     'pending_approval' (mirrors REGISTRY_STATUS_FOR[candidate] in
     promotion_service.js).
  2. auto_demote_negative_sharpe is registry-first: the registry gate closes
     BEFORE the manifest transition.
  3. A registry-sync failure SKIPS the manifest demotion for that sid (never
     the dangerous half-state: manifest candidate + registry approved).

All DB access is stubbed — no production Postgres contact.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import lifecycle as lc  # noqa: E402


def _write_manifest(tmpdir: Path, sids: list[str]) -> Path:
    p = tmpdir / 'manifest.json'
    p.write_text(json.dumps({
        'strategies': {
            sid: {'state': 'live', 'state_since': '2026-07-13T00:00:00Z',
                  'metadata': {}}
            for sid in sids
        },
        'decommissioned': {},
    }))
    return p


class _FakeCursor:
    def __init__(self, log):
        self._log = log

    def execute(self, sql, params=None):
        self._log.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, log):
        self._log = log

    def cursor(self):
        return _FakeCursor(self._log)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestSyncRegistryDemotionSql(unittest.TestCase):
    def test_updates_registry_to_pending_approval(self):
        calls = []
        with patch('psycopg2.connect', return_value=_FakeConn(calls)), \
             patch.dict('os.environ', {'POSTGRES_URI': 'postgres://stub'}):
            lc._sync_registry_demotion('S_x')
        self.assertEqual(len(calls), 1)
        sql, params = calls[0]
        self.assertIn('UPDATE strategy_registry', sql)
        self.assertIn('SET status', sql)
        # ('pending_approval', sid) — the REGISTRY_STATUS_FOR[candidate] value.
        self.assertEqual(params, ('pending_approval', 'S_x'))


class TestAutoDemoteRegistryFirst(unittest.TestCase):
    def _run(self, tmpdir, targets, sync_side_effect=None):
        """Run auto_demote_negative_sharpe against a tmp manifest with the
        negative-Sharpe finder and the registry sync stubbed."""
        manifest = _write_manifest(Path(tmpdir), targets)
        timeline = []

        def _sync(sid, status='pending_approval'):
            timeline.append(('registry', sid))
            if sync_side_effect is not None:
                sync_side_effect(sid)

        real_transition = lc.LifecycleStateMachine.transition

        def _tracked_transition(self, sid, *a, **kw):
            timeline.append(('manifest', sid))
            return real_transition(self, sid, *a, **kw)

        import execution.strategy_weights as sw
        with patch.object(sw, 'find_negative_across_all_eligible',
                          return_value=list(targets)), \
             patch.object(lc, '_sync_registry_demotion', side_effect=_sync), \
             patch.object(lc.LifecycleStateMachine, 'transition',
                          _tracked_transition):
            demoted = lc.auto_demote_negative_sharpe(
                manifest_path=str(manifest))
        states = {sid: rec['state']
                  for sid, rec in json.loads(manifest.read_text())['strategies'].items()}
        return demoted, timeline, states

    def test_registry_closes_before_manifest_transition(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            demoted, timeline, states = self._run(td, ['S_a', 'S_b'])
        self.assertEqual(sorted(demoted), ['S_a', 'S_b'])
        self.assertEqual(states, {'S_a': 'candidate', 'S_b': 'candidate'})
        # Per sid: the registry event strictly precedes the manifest event.
        for sid in ('S_a', 'S_b'):
            reg_i = timeline.index(('registry', sid))
            man_i = timeline.index(('manifest', sid))
            self.assertLess(reg_i, man_i,
                            f'{sid}: registry gate must close before the manifest write')

    def test_registry_failure_skips_manifest_demotion(self):
        import tempfile

        def _fail_sb(sid):
            if sid == 'S_b':
                raise RuntimeError('db unreachable')

        with tempfile.TemporaryDirectory() as td:
            demoted, timeline, states = self._run(
                td, ['S_a', 'S_b'], sync_side_effect=_fail_sb)
        # S_a demoted normally; S_b left LIVE in the manifest (consistent
        # state — retried next rebuild), and NOT reported as demoted.
        self.assertEqual(demoted, ['S_a'])
        self.assertEqual(states['S_a'], 'candidate')
        self.assertEqual(states['S_b'], 'live')
        self.assertNotIn(('manifest', 'S_b'), timeline)


if __name__ == '__main__':
    unittest.main()
