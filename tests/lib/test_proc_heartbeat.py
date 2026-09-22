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


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.ttls = {}

    def hset(self, key, mapping=None, **kw):
        self.hashes.setdefault(key, {}).update(mapping or kw)
        return len(mapping or kw)

    def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    def delete(self, key):
        self.hashes.pop(key, None)
        self.ttls.pop(key, None)


class ExplodingRedis:
    def hset(self, *a, **kw):
        raise RuntimeError('redis down')

    def expire(self, *a, **kw):
        raise RuntimeError('redis down')

    def delete(self, *a, **kw):
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
