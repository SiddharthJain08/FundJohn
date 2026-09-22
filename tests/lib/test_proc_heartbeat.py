"""tests/lib/test_proc_heartbeat.py — per-process identity in Redis (QD §5 E3)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from lib import proc_heartbeat as ph  # noqa: E402


class _FakePipeline:
    """Buffers hset/expire calls and applies them ALL AT ONCE on execute() —
    mirrors redis-py's MULTI/EXEC pipeline closely enough for this test
    double: the parent's `hashes`/`ttls` are never touched key-by-key, so a
    half-written key (fields with no TTL) can never be observed through it.
    Ops are applied directly against the parent's dicts (NOT via the
    parent's own hset()/expire() methods), so `direct_hset_calls` /
    `direct_expire_calls` stay a clean signal of whether a caller bypassed
    the pipeline and hit the bare top-level methods instead."""
    def __init__(self, parent):
        self._parent = parent
        self._ops = []

    def hset(self, key, mapping=None, **kw):
        self._ops.append(('hset', key, dict(mapping or kw)))
        return self

    def expire(self, key, ttl):
        self._ops.append(('expire', key, int(ttl)))
        return self

    def execute(self):
        results = []
        for op, key, val in self._ops:
            if op == 'hset':
                self._parent.hashes.setdefault(key, {}).update(val)
                results.append(len(val))
            else:
                self._parent.ttls[key] = val
                results.append(True)
        self._ops = []
        return results


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.ttls = {}
        self.direct_hset_calls = 0
        self.direct_expire_calls = 0
        self.pipeline_count = 0

    def hset(self, key, mapping=None, **kw):
        self.direct_hset_calls += 1
        self.hashes.setdefault(key, {}).update(mapping or kw)
        return len(mapping or kw)

    def expire(self, key, ttl):
        self.direct_expire_calls += 1
        self.ttls[key] = ttl
        return True

    def delete(self, key):
        self.hashes.pop(key, None)
        self.ttls.pop(key, None)

    def pipeline(self, transaction=True):
        self.pipeline_count += 1
        return _FakePipeline(self)


class _BlowsUpMidTransactionPipeline:
    """Applies the hset half, then raises — simulating a connection that
    drops between the two commands inside MULTI/EXEC. Proves write()
    reports failure (returns None) rather than a caller mistaking a
    half-applied transaction for success; real Redis's MULTI/EXEC would
    never leave this partial state durably visible, but write()'s contract
    — "None on ANY error" — must hold regardless of how the pipeline fails."""
    def __init__(self, parent):
        self._parent = parent

    def hset(self, key, mapping=None, **kw):
        self._parent.hashes.setdefault(key, {}).update(mapping or kw)
        return self

    def expire(self, key, ttl):
        raise RuntimeError('connection dropped mid-transaction')

    def execute(self):
        raise RuntimeError('connection dropped mid-transaction')


class ExplodingRedis:
    def hset(self, *a, **kw):
        raise RuntimeError('redis down')

    def expire(self, *a, **kw):
        raise RuntimeError('redis down')

    def delete(self, *a, **kw):
        raise RuntimeError('redis down')

    def pipeline(self, transaction=True):
        raise RuntimeError('redis down')


class TestKeyAndRss(unittest.TestCase):
    def test_key_shape(self):
        self.assertEqual(ph.heartbeat_key(host='vps1', pid=42), 'proc:vps1:42')
        self.assertTrue(ph.heartbeat_key().startswith('proc:'))
        self.assertTrue(ph.heartbeat_key().endswith(f':{os.getpid()}'))

    def test_rss_of_this_process_is_positive(self):
        v = ph.rss_mb()
        self.assertIsNotNone(v)
        self.assertGreater(v, 0.0)

    def test_rss_of_a_dead_pid_is_none(self):
        self.assertIsNone(ph.rss_mb(999999))

    def test_default_ttl_is_180(self):
        self.assertEqual(ph.DEFAULT_TTL_S, 180)


class TestWrite(unittest.TestCase):
    def test_writes_every_field_and_sets_the_ttl(self):
        r = FakeRedis()
        key = ph.write(r, step='signals', argv=['python3', 'engine.py', '--date', '2026-09-14'],
                       pid=42, host='vps1', started_at='2026-09-14T10:00:00+00:00')
        self.assertEqual(key, 'proc:vps1:42')
        h = r.hashes[key]
        self.assertEqual(h['step'], 'signals')
        self.assertEqual(h['argv'], 'python3 engine.py --date 2026-09-14')
        self.assertEqual(h['host'], 'vps1')
        self.assertEqual(h['pid'], '42')
        self.assertEqual(h['started_at'], '2026-09-14T10:00:00+00:00')
        self.assertIn('updated_at', h)
        self.assertIn('rss_mb', h)
        self.assertTrue(all(isinstance(v, str) for v in h.values()), h)
        self.assertEqual(r.ttls[key], 180)

    def test_ttl_is_overridable_for_long_blocking_children(self):
        r = FakeRedis()
        key = ph.write(r, step='fleet:S_x', pid=7, host='vps1', ttl_s=3900)
        self.assertEqual(r.ttls[key], 3900)

    def test_never_raises_when_redis_is_down(self):
        self.assertIsNone(ph.write(ExplodingRedis(), step='signals', pid=1, host='h'))
        ph.clear(ExplodingRedis(), pid=1, host='h')      # must not raise

    def test_clear_removes_the_key(self):
        r = FakeRedis()
        ph.write(r, step='signals', pid=42, host='vps1')
        ph.clear(r, pid=42, host='vps1')
        self.assertNotIn('proc:vps1:42', r.hashes)

    def test_writes_hset_and_expire_through_one_pipeline_not_two_bare_calls(self):
        """Fix round 1, minor item 1: hset + expire must run inside ONE
        pipeline/MULTI so a half-written key (fields with no TTL) can never
        become immortal if the connection drops between two separate
        top-level calls."""
        r = FakeRedis()
        ph.write(r, step='signals', pid=42, host='vps1')
        self.assertEqual(r.pipeline_count, 1)
        self.assertEqual(r.direct_hset_calls, 0)
        self.assertEqual(r.direct_expire_calls, 0)

    def test_write_returns_none_when_the_pipeline_blows_up_mid_transaction(self):
        r = FakeRedis()
        r.pipeline = lambda transaction=True: _BlowsUpMidTransactionPipeline(r)
        self.assertIsNone(ph.write(r, step='signals', pid=42, host='vps1'))


class TestProcRegistryCheck(unittest.TestCase):
    """The system check reads the same keys the writers produce."""

    def test_check_is_registered_with_the_agents_tag(self):
        from system_checks import all_checks  # noqa: E402 (imports checks/ as a side effect)
        reg = all_checks()
        self.assertIn('proc_registry', reg)
        self.assertIn('agents', reg['proc_registry']['tags'])
        self.assertEqual(reg['proc_registry']['requires'], ['fs'])


if __name__ == '__main__':
    unittest.main()
