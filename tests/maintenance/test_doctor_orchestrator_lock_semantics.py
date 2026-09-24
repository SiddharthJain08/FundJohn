"""tests/maintenance/test_doctor_orchestrator_lock_semantics.py —
`check_orchestrator_lock` ruled semantics (QD E1/E3 fix round 1, ruled item
4): diagnostic, not a gate. A live holder (same host or another host)
PASSes; only a dead same-host holder, or an unparseable/no-expiry value,
WARNs; a vanished key (ttl == -2) is ignored outright.

No real Redis: `redis.from_url` is a fake module object injected via
monkeypatch.setitem(sys.modules, 'redis', ...).
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from lib.run_lock import make_value  # noqa: E402
from maintenance import doctor as doc  # noqa: E402

ME = socket.gethostname()
KEY = 'pipeline:run_lock:2026-09-14'


class _FakeClient:
    def __init__(self, store, ttls, boom_on_ttl_of=None):
        self._store = store
        self._ttls = ttls
        self._boom_on_ttl_of = boom_on_ttl_of

    def scan_iter(self, pattern, count=20):
        prefix = pattern.rstrip('*')
        return iter([k for k in list(self._store) if k.startswith(prefix)])

    def ttl(self, k):
        if self._boom_on_ttl_of is not None and k == self._boom_on_ttl_of:
            raise ConnectionError('redis dropped mid-scan')
        return self._ttls.get(k, -2)

    def get(self, k):
        return self._store.get(k)


class _FakeRedisModule:
    def __init__(self, client=None, raise_on_from_url=None):
        self._client = client
        self._raise = raise_on_from_url

    def from_url(self, *a, **kw):
        if self._raise is not None:
            raise self._raise
        return self._client


def _install(monkeypatch, client=None, raise_on_from_url=None):
    monkeypatch.setitem(sys.modules, 'redis',
                        _FakeRedisModule(client=client, raise_on_from_url=raise_on_from_url))


def test_no_keys_is_pass(monkeypatch):
    _install(monkeypatch, client=_FakeClient({}, {}))
    r = doc.check_orchestrator_lock()
    assert r['severity'] == doc.PASS
    assert 'no locks held' in r['detail']


def test_live_same_host_holder_is_pass(monkeypatch):
    val = make_value(pid=os.getpid(), host=ME, started_at='2026-09-14T10:00:00+00:00')
    _install(monkeypatch, client=_FakeClient({KEY: val}, {KEY: 3600}))
    r = doc.check_orchestrator_lock()
    assert r['severity'] == doc.PASS
    assert f'held by {ME}:{os.getpid()}' in r['detail']
    assert 'ttl=3600s' in r['detail']


def test_live_other_host_holder_is_pass_with_owner(monkeypatch):
    val = make_value(pid=4242, host='otherbox', started_at='2026-09-14T10:00:00+00:00')
    _install(monkeypatch, client=_FakeClient({KEY: val}, {KEY: 3600}))
    r = doc.check_orchestrator_lock()
    assert r['severity'] == doc.PASS
    assert 'held by otherbox:4242' in r['detail']


def test_dead_same_host_holder_is_warn(monkeypatch):
    # A pid essentially guaranteed dead on this box.
    dead_pid = 999999
    val = make_value(pid=dead_pid, host=ME, started_at='2026-09-14T10:00:00+00:00')
    _install(monkeypatch, client=_FakeClient({KEY: val}, {KEY: 3600}))
    r = doc.check_orchestrator_lock()
    assert r['severity'] == doc.WARN
    assert f'stale lock, holder pid {dead_pid} dead' in r['detail']


def test_no_expiry_key_is_warn(monkeypatch):
    val = make_value(pid=os.getpid(), host=ME, started_at='2026-09-14T10:00:00+00:00')
    _install(monkeypatch, client=_FakeClient({KEY: val}, {KEY: -1}))
    r = doc.check_orchestrator_lock()
    assert r['severity'] == doc.WARN
    assert 'no-expiry' in r['detail']


def test_unparseable_value_is_warn(monkeypatch):
    _install(monkeypatch, client=_FakeClient({KEY: 'not-a-valid-value'}, {KEY: 3600}))
    r = doc.check_orchestrator_lock()
    assert r['severity'] == doc.WARN
    assert 'unparseable' in r['detail']


def test_vanished_key_ttl_minus_2_is_ignored(monkeypatch):
    """ttl == -2 means the key vanished between the scan and this read —
    not a lock. The only key present has ttl=-2, so this must read exactly
    like 'no locks held', not WARN."""
    val = make_value(pid=os.getpid(), host=ME, started_at='2026-09-14T10:00:00+00:00')
    _install(monkeypatch, client=_FakeClient({KEY: val}, {KEY: -2}))
    r = doc.check_orchestrator_lock()
    assert r['severity'] == doc.PASS
    assert 'no locks held' in r['detail']


def test_connection_error_on_connect_is_warn(monkeypatch):
    _install(monkeypatch, raise_on_from_url=ConnectionError('refused'))
    r = doc.check_orchestrator_lock()
    assert r['severity'] == doc.WARN
    assert 'redis error' in r['detail'].lower()


def test_error_mid_loop_is_warn_not_a_false_all_clear(monkeypatch):
    """Two keys; the SECOND key's ttl() raises. The whole try wraps the
    read loop, so this must WARN (unknown lock state) — never silently
    `continue` past the failing key and report a false PASS 'no locks
    held', which is the exact bug this fix-round-1 item targets."""
    key2 = 'pipeline:run_lock:2026-09-15'
    val1 = make_value(pid=os.getpid(), host=ME, started_at='2026-09-14T10:00:00+00:00')
    val2 = make_value(pid=os.getpid(), host=ME, started_at='2026-09-15T10:00:00+00:00')
    client = _FakeClient({KEY: val1, key2: val2}, {KEY: 3600, key2: 3600},
                         boom_on_ttl_of=key2)
    _install(monkeypatch, client=client)
    r = doc.check_orchestrator_lock()
    assert r['severity'] == doc.WARN
    assert 'redis error' in r['detail'].lower()
