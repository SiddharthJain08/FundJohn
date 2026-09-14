"""tests/execution/test_run_lock_wiring.py — orchestrator uses the shared,
owned, renewed run lock and exits rc=75 when another run owns today.

Fake Redis only; no subprocess is ever spawned (run_step is stubbed).
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import pipeline_orchestrator as po  # noqa: E402
from lib import run_lock  # noqa: E402


class FakeRedis:
    def __init__(self, store=None):
        self.store = dict(store or {})
        self.ttls = {}

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.store:
            return None
        self.store[k] = v
        if ex is not None:
            self.ttls[k] = ex
        return True

    def get(self, k):
        return self.store.get(k)

    def delete(self, k):
        self.store.pop(k, None)
        self.ttls.pop(k, None)

    def expire(self, k, ttl):
        if k not in self.store:
            return False
        self.ttls[k] = ttl
        return True

    def setex(self, k, ttl, v):
        self.store[k] = v
        self.ttls[k] = ttl

    def publish(self, *a, **kw):
        return 0


DATE = '2026-09-14'
KEY = f'pipeline:run_lock:{DATE}'


class TestLockHelpers(unittest.TestCase):
    def setUp(self):
        po.LOCK_VALUE = None

    def tearDown(self):
        po.LOCK_VALUE = None

    def test_acquire_writes_the_shared_key_with_an_owned_value(self):
        r = FakeRedis()
        self.assertTrue(po.acquire_lock(r, DATE))
        self.assertIn(KEY, r.store)
        self.assertIsNotNone(run_lock.parse_value(r.store[KEY]))
        self.assertEqual(run_lock.parse_value(r.store[KEY])[1], os.getpid())
        self.assertEqual(po.LOCK_VALUE, r.store[KEY])

    def test_acquire_refuses_when_a_live_holder_owns_the_key(self):
        r = FakeRedis({KEY: f'{po._HOST}:{os.getpid()}:T0'})
        self.assertFalse(po.acquire_lock(r, DATE))

    def test_renew_extends_our_ttl_to_step_timeout_plus_120(self):
        r = FakeRedis()
        po.acquire_lock(r, DATE)
        self.assertTrue(po.renew_lock(r, DATE, run_lock.ttl_for(9000)))
        self.assertEqual(r.ttls[KEY], 9120)

    def test_release_is_value_checked(self):
        r = FakeRedis()
        po.acquire_lock(r, DATE)
        r.store[KEY] = 'otherbox:222:T9'          # someone took over
        self.assertFalse(po.release_lock(r, DATE))
        self.assertEqual(r.store[KEY], 'otherbox:222:T9')

    def test_release_deletes_our_own_lock(self):
        r = FakeRedis()
        po.acquire_lock(r, DATE)
        self.assertTrue(po.release_lock(r, DATE))
        self.assertNotIn(KEY, r.store)


class TestRunStepRenews(unittest.TestCase):
    def test_run_step_calls_renew_with_the_resolved_step_timeout(self):
        seen = []
        # `false` exits 1 immediately; we only care that renew fired first.
        orig = po._resolve_script
        po._resolve_script = lambda script, run_date: (['false'], 777)
        try:
            ok, rc = po.run_step('engine', DATE, dict(os.environ),
                                 renew=lambda t: seen.append(t))
        finally:
            po._resolve_script = orig
        self.assertFalse(ok)
        self.assertEqual(seen, [777])

    def test_run_step_without_renew_is_unchanged(self):
        orig = po._resolve_script
        po._resolve_script = lambda script, run_date: (['true'], 5)
        try:
            ok, rc = po.run_step('engine', DATE, dict(os.environ))
        finally:
            po._resolve_script = orig
        self.assertTrue(ok)
        self.assertEqual(rc, 0)


class TestMainBusyLock(unittest.TestCase):
    def test_main_returns_75_and_posts_when_another_run_owns_today(self):
        posts = []
        r = FakeRedis({KEY: f'{po._HOST}:{os.getpid()}:T0'})
        orig = (po.get_redis, po.notify, po.run_step, po.is_completed_today, po.read_checkpoint)
        po.get_redis = lambda: r
        po.notify = lambda msg, channel='pipeline-feed': posts.append((channel, msg))
        po.run_step = lambda *a, **kw: (_ for _ in ()).throw(AssertionError('no step may run'))
        po.is_completed_today = lambda _r, _d: False
        po.read_checkpoint = lambda _r: None
        os.environ.setdefault('POSTGRES_URI', 'postgresql://stub/stub')
        try:
            rc = po.main(['--date', DATE, '--steps', 'report'])
        finally:
            (po.get_redis, po.notify, po.run_step,
             po.is_completed_today, po.read_checkpoint) = orig
        self.assertEqual(rc, 75)
        self.assertEqual(rc, run_lock.LOCK_BUSY_RC)
        self.assertTrue(any('[lock] held by' in m for _c, m in posts), posts)
        self.assertEqual(r.store[KEY], f'{po._HOST}:{os.getpid()}:T0')


class TestRenewLostIsFatal(unittest.TestCase):
    """Controller ruling (Task 1 review, carried into this task's brief):
    a renew() that reports we no longer own the lock is FATAL — the
    orchestrator must stop before running the next step, log '[lock] lost',
    post to Discord, and exit rc=75. This is distinct from TestMainBusyLock
    (busy at acquire time, before any step runs): here the lock is acquired
    cleanly, the first step succeeds, and the SECOND renew (before the
    second step) reports loss — the harder, mid-cycle case the ruling names.

    `_resolve_script` is stubbed for speed/determinism (real scripts are
    never spawned — `run_step` itself is NOT stubbed, so the real renew
    wiring runs). Because main() also calls `_resolve_script` once per step
    up front to size the initial lock TTL, "which steps ran" is tracked by
    wrapping `run_step` itself (called exactly once per step attempted),
    not by counting `_resolve_script` calls.
    """

    def test_main_stops_before_the_next_step_when_renew_reports_lock_loss(self):
        posts = []
        r = FakeRedis()
        ran_scripts = []
        orig = (po.get_redis, po.notify, po._resolve_script, po.run_step,
                po.is_completed_today, po.read_checkpoint)
        po.get_redis = lambda: r
        po.notify = lambda msg, channel='pipeline-feed': posts.append((channel, msg))
        po._resolve_script = lambda script, run_date: (['true'], 5)
        po.is_completed_today = lambda _r, _d: False
        po.read_checkpoint = lambda _r: None
        os.environ.setdefault('POSTGRES_URI', 'postgresql://stub/stub')

        orig_run_step = po.run_step

        def _tracking_run_step(script, run_date, env, renew=None):
            ran_scripts.append(script)
            return orig_run_step(script, run_date, env, renew=renew)

        po.run_step = _tracking_run_step

        # First renew (before step 1, 'signals') succeeds normally; the
        # second (before step 2, 'handoff') reports we no longer own the
        # lock — simulating a takeover that happened mid-cycle.
        renew_calls = []
        orig_renew = run_lock.renew

        def _fake_renew(*a, **kw):
            renew_calls.append(1)
            return len(renew_calls) == 1

        run_lock.renew = _fake_renew
        try:
            rc = po.main(['--date', DATE, '--steps', 'signals,handoff,trade'])
        finally:
            (po.get_redis, po.notify, po._resolve_script, po.run_step,
             po.is_completed_today, po.read_checkpoint) = orig
            run_lock.renew = orig_renew

        self.assertEqual(rc, run_lock.LOCK_BUSY_RC)
        self.assertEqual(rc, 75)
        self.assertTrue(any('[lock] lost' in m for _c, m in posts), posts)
        # 'signals' (engine) ran and succeeded; 'handoff' (trade_handoff_
        # builder) was attempted and its renew lost the lock before spawn;
        # 'trade' (regime_blended_sizer_live) must never have been attempted
        # at all — the cycle stopped before its step, the actual claim.
        self.assertEqual(ran_scripts, ['engine', 'trade_handoff_builder'])


if __name__ == '__main__':
    unittest.main()
