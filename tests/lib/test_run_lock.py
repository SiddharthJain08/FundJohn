"""tests/lib/test_run_lock.py — one owned, renewed run lock (QD spec §5 E1).

No real Redis: FakeRedis below implements only the four calls run_lock uses.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from lib import run_lock  # noqa: E402


class FakeRedis:
    """Minimal in-memory stub: set(nx/ex), get, delete, expire."""

    def __init__(self, store=None):
        self.store = dict(store or {})
        self.ttls = {}
        self.calls = []

    def set(self, k, v, nx=False, ex=None):
        self.calls.append(('set', k, v, nx, ex))
        if nx and k in self.store:
            return None
        self.store[k] = v
        if ex is not None:
            self.ttls[k] = ex
        return True

    def get(self, k):
        return self.store.get(k)

    def delete(self, k):
        self.calls.append(('delete', k))
        self.store.pop(k, None)
        self.ttls.pop(k, None)

    def expire(self, k, ttl):
        self.calls.append(('expire', k, ttl))
        if k not in self.store:
            return False
        self.ttls[k] = ttl
        return True


DATE = '2026-09-14'
KEY = f'pipeline:run_lock:{DATE}'


class TestKeyAndValue(unittest.TestCase):
    def test_key_prefix_comes_from_the_shared_json_file(self):
        cfg = json.loads((ROOT / 'src' / 'lib' / 'run_lock_key.json').read_text())
        self.assertEqual(cfg['prefix'], 'pipeline:run_lock')
        self.assertEqual(run_lock.KEY_PREFIX, cfg['prefix'])
        self.assertEqual(run_lock.lock_key(DATE), KEY)

    def test_value_is_host_pid_start_iso_and_round_trips(self):
        v = run_lock.make_value(pid=4242, host='vps1', started_at='2026-09-14T10:00:00+00:00')
        self.assertEqual(v, 'vps1:4242:2026-09-14T10:00:00+00:00')
        self.assertEqual(run_lock.parse_value(v), ('vps1', 4242, '2026-09-14T10:00:00+00:00'))

    def test_parse_value_rejects_garbage(self):
        self.assertIsNone(run_lock.parse_value(None))
        self.assertIsNone(run_lock.parse_value(''))
        self.assertIsNone(run_lock.parse_value('1'))
        self.assertIsNone(run_lock.parse_value('host:notapid:2026-09-14T10:00:00+00:00'))

    def test_ttl_is_step_timeout_plus_slack_with_a_floor(self):
        self.assertEqual(run_lock.ttl_for(9000), 9120)
        self.assertEqual(run_lock.ttl_for(300), 420)
        self.assertEqual(run_lock.ttl_for(1), run_lock.MIN_TTL_SECONDS)

    def test_lock_busy_rc_is_75(self):
        self.assertEqual(run_lock.LOCK_BUSY_RC, 75)


class TestAcquire(unittest.TestCase):
    def test_acquires_a_free_lock_and_stores_the_owned_value(self):
        r = FakeRedis()
        ok, value = run_lock.acquire(r, DATE, 420, value='vps1:111:T0')
        self.assertTrue(ok)
        self.assertEqual(value, 'vps1:111:T0')
        self.assertEqual(r.store[KEY], 'vps1:111:T0')
        self.assertEqual(r.ttls[KEY], 420)

    def test_refuses_when_held_by_a_live_pid_and_returns_the_holder(self):
        held = f'vps1:{os.getpid()}:T0'
        r = FakeRedis({KEY: held})
        ok, holder = run_lock.acquire(r, DATE, 420, value='vps1:999:T1', host='vps1')
        self.assertFalse(ok)
        self.assertEqual(holder, held)
        self.assertEqual(r.store[KEY], held)

    def test_takes_over_when_the_holder_pid_is_dead_on_this_host(self):
        logs = []
        r = FakeRedis({KEY: 'vps1:999999:T0'})
        ok, value = run_lock.acquire(r, DATE, 420, value='vps1:111:T1',
                                     host='vps1', log=logs.append)
        self.assertTrue(ok)
        self.assertEqual(r.store[KEY], 'vps1:111:T1')
        self.assertTrue(any('stale lock from pid 999999' in m for m in logs), logs)

    def test_never_takes_over_a_lock_held_by_another_host(self):
        r = FakeRedis({KEY: 'otherbox:999999:T0'})
        ok, holder = run_lock.acquire(r, DATE, 420, value='vps1:111:T1', host='vps1')
        self.assertFalse(ok)
        self.assertEqual(holder, 'otherbox:999999:T0')

    def test_refuses_on_an_unparseable_holder_rather_than_stealing(self):
        r = FakeRedis({KEY: '1'})
        ok, holder = run_lock.acquire(r, DATE, 420, value='vps1:111:T1', host='vps1')
        self.assertFalse(ok)
        self.assertEqual(holder, '1')


class TestRenewAndRelease(unittest.TestCase):
    def test_renew_extends_only_our_own_lock(self):
        r = FakeRedis({KEY: 'vps1:111:T0'})
        self.assertTrue(run_lock.renew(r, DATE, 'vps1:111:T0', 9120))
        self.assertEqual(r.ttls[KEY], 9120)

    def test_renew_refuses_when_another_owner_took_over(self):
        logs = []
        r = FakeRedis({KEY: 'vps1:222:T1'})
        self.assertFalse(run_lock.renew(r, DATE, 'vps1:111:T0', 9120, log=logs.append))
        self.assertEqual(r.ttls, {})
        self.assertTrue(any('held by vps1:222:T1' in m for m in logs), logs)

    def test_renew_re_takes_a_lock_that_expired_under_us(self):
        r = FakeRedis()
        self.assertTrue(run_lock.renew(r, DATE, 'vps1:111:T0', 9120))
        self.assertEqual(r.store[KEY], 'vps1:111:T0')

    def test_release_deletes_only_our_own_lock(self):
        r = FakeRedis({KEY: 'vps1:111:T0'})
        self.assertTrue(run_lock.release(r, DATE, 'vps1:111:T0'))
        self.assertNotIn(KEY, r.store)

    def test_release_never_deletes_a_lock_someone_else_now_owns(self):
        logs = []
        r = FakeRedis({KEY: 'vps1:222:T1'})
        self.assertFalse(run_lock.release(r, DATE, 'vps1:111:T0', log=logs.append))
        self.assertEqual(r.store[KEY], 'vps1:222:T1')
        self.assertNotIn(('delete', KEY), r.calls)

    def test_release_of_an_absent_lock_is_a_no_op(self):
        r = FakeRedis()
        self.assertFalse(run_lock.release(r, DATE, 'vps1:111:T0'))

    def test_release_with_no_value_is_a_no_op(self):
        r = FakeRedis({KEY: 'vps1:111:T0'})
        self.assertFalse(run_lock.release(r, DATE, None))
        self.assertIn(KEY, r.store)


if __name__ == '__main__':
    unittest.main()
