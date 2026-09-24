"""tests/system_checks/test_proc_registry_check.py — proc_registry SKIPs on
ANY Redis error, uses lib.run_lock.pid_alive, caps scan_iter while
iterating, and reports an age=<s> column (QD E3 fix round 1, ruled item 3).

No real Redis: `redis.from_url` is a fake module object injected via
monkeypatch.setitem(sys.modules, 'redis', ...) — the same technique used
elsewhere in this repo for inline `import redis` call sites.
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from system_checks.types import Status
from system_checks.checks.agents import _proc_registry


class _FakeClient:
    """hgetall raises on the `boom_on`'th call (1-indexed) to simulate a
    Redis connection that dies partway through the read loop."""
    def __init__(self, hashes, boom_on=None):
        self._hashes = hashes
        self._boom_on = boom_on
        self._n = 0

    def scan_iter(self, pattern, count=100):
        for k in list(self._hashes.keys()):
            yield k

    def hgetall(self, key):
        self._n += 1
        if self._boom_on is not None and self._n == self._boom_on:
            raise ConnectionError('redis dropped mid-scan')
        return self._hashes[key]


class _CountingClient(_FakeClient):
    """Records every key scan_iter actually yields, so the cap-while-
    iterating behavior can be asserted directly."""
    def __init__(self, hashes, pulled):
        super().__init__(hashes)
        self._pulled = pulled

    def scan_iter(self, pattern, count=100):
        for k in list(self._hashes.keys()):
            self._pulled.append(k)
            yield k


class _FakeRedisModule:
    def __init__(self, client=None, raise_on_from_url=None):
        self._client = client
        self._raise = raise_on_from_url

    def from_url(self, *a, **kw):
        if self._raise is not None:
            raise self._raise
        return self._client


def _entry(pid=None, host=None, step='signals', rss='10', updated_at='2026-09-14T10:00:00+00:00'):
    h = {'step': step, 'rss_mb': rss, 'updated_at': updated_at}
    if pid is not None:
        h['pid'] = pid
    if host is not None:
        h['host'] = host
    return h


def test_connection_error_returns_skip(monkeypatch):
    monkeypatch.setitem(sys.modules, 'redis',
                        _FakeRedisModule(raise_on_from_url=ConnectionError('refused')))
    status, detail = _proc_registry()
    assert status == Status.SKIP
    assert 'redis error' in detail.lower()


def test_error_mid_loop_returns_skip_not_error(monkeypatch):
    """A connection that dies on the SECOND key (not the first) must still
    SKIP — the try wraps the whole read loop, so a mid-scan drop can never
    surface as an unhandled ERROR from the runner."""
    hashes = {
        'proc:h1:1': _entry(pid='1', host='h1'),
        'proc:h1:2': _entry(pid='2', host='h1'),
        'proc:h1:3': _entry(pid='3', host='h1'),
    }
    client = _FakeClient(hashes, boom_on=2)
    monkeypatch.setitem(sys.modules, 'redis', _FakeRedisModule(client=client))
    status, detail = _proc_registry()
    assert status == Status.SKIP
    assert 'redis error' in detail.lower()


def test_missing_pid_field_is_treated_as_not_live(monkeypatch):
    """No 'pid' key at all on a same-host entry: `pid_alive(None)` returns
    False (can't parse -> not live), so this is a ghost — not a crash and
    not silently ignored."""
    me = socket.gethostname()
    hashes = {f'proc:{me}:missing': _entry(pid=None, host=me)}
    client = _FakeClient(hashes)
    monkeypatch.setitem(sys.modules, 'redis', _FakeRedisModule(client=client))
    status, detail = _proc_registry()
    assert status == Status.WARN
    assert 'ghost' in detail.lower()


def test_no_keys_returns_pass(monkeypatch):
    client = _FakeClient({})
    monkeypatch.setitem(sys.modules, 'redis', _FakeRedisModule(client=client))
    status, detail = _proc_registry()
    assert status == Status.PASS
    assert 'no live' in detail.lower()


def test_live_holder_on_another_host_is_pass_not_ghost(monkeypatch):
    """A pid that can't be verified because it's on a DIFFERENT host must
    never be probed with pid_alive — it just shows up in the roster."""
    me = socket.gethostname()
    hashes = {'proc:otherbox:999': _entry(pid='999', host='otherbox')}
    client = _FakeClient(hashes)
    monkeypatch.setitem(sys.modules, 'redis', _FakeRedisModule(client=client))
    status, detail = _proc_registry()
    assert status == Status.PASS
    assert 'ghost' not in detail.lower()
    assert me not in ('otherbox',)  # sanity: fixture host really is "other"


def test_scan_is_capped_while_iterating_no_full_materialisation(monkeypatch):
    """25 entries in the fake registry; the check must pull AT MOST its
    bound (20) out of scan_iter, proving it never materialises (e.g. via
    sorted(list(...))) the whole result set first."""
    me = socket.gethostname()
    hashes = {
        f'proc:{me}:{i}': _entry(pid=str(i), host=me)
        for i in range(1, 26)
    }
    pulled: list = []
    client = _CountingClient(hashes, pulled)
    monkeypatch.setitem(sys.modules, 'redis', _FakeRedisModule(client=client))
    _proc_registry()
    assert len(pulled) == 20


def test_age_column_present_in_each_line(monkeypatch):
    me = socket.gethostname()
    # A live pid (this test process itself) so this test isn't ALSO
    # asserting ghost-detection behavior — that's covered separately above.
    hashes = {f'proc:{me}:1': _entry(pid=str(os.getpid()), host=me,
                                     updated_at='2026-09-14T10:00:00+00:00')}
    client = _FakeClient(hashes)
    monkeypatch.setitem(sys.modules, 'redis', _FakeRedisModule(client=client))
    status, detail = _proc_registry()
    assert status == Status.PASS
    assert 'age=' in detail
