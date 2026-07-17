"""tests/test_close_inflight_lock.py — execute-phase concurrency lock.

The 3:55pm close-execute sets execute:close:inflight:{date}; a concurrent
intraday-regime redeploy must defer rather than race the submission.
"""
from __future__ import annotations
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src')); sys.path.insert(0, str(ROOT / 'scripts'))
from execution import alpaca_executor as ae  # noqa: E402
import redeploy_pipeline as rp  # noqa: E402

KEY = 'execute:close:inflight:2026-05-27'


class FakeRedis:
    def __init__(self, store=None):
        self.store = dict(store or {})
        self.calls = []

    def setex(self, k, ttl, v):
        self.calls.append(('setex', k, ttl, v)); self.store[k] = v

    def delete(self, k):
        self.calls.append(('delete', k)); self.store.pop(k, None)

    def get(self, k):
        return self.store.get(k)


class TestExecutorSetClear(unittest.TestCase):
    def test_set_writes_key_with_ttl(self):
        r = FakeRedis()
        self.assertTrue(ae._set_close_inflight('2026-05-27', ttl=300, _r=r))
        self.assertEqual(r.store.get(KEY), '1')
        self.assertIn(('setex', KEY, 300, '1'), r.calls)

    def test_clear_deletes_key(self):
        r = FakeRedis({KEY: '1'})
        ae._clear_close_inflight('2026-05-27', _r=r)
        self.assertNotIn(KEY, r.store)

    def test_set_no_redis_is_falsey_no_raise(self):
        old = ae._executor_redis
        ae._executor_redis = lambda: None
        try:
            self.assertFalse(ae._set_close_inflight('2026-05-27'))
        finally:
            ae._executor_redis = old


class TestRedeployDefers(unittest.TestCase):
    def test_defers_when_inflight_set(self):
        self.assertTrue(rp._close_execute_inflight(FakeRedis({KEY: '1'}), '2026-05-27'))

    def test_proceeds_when_not_set(self):
        self.assertFalse(rp._close_execute_inflight(FakeRedis(), '2026-05-27'))

    def test_proceeds_when_redis_none(self):
        self.assertFalse(rp._close_execute_inflight(None, '2026-05-27'))


if __name__ == '__main__':
    unittest.main()
