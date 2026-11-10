"""system_checks crypto_breaker_evaluating (I4): offline, every dependency faked."""
from __future__ import annotations

import datetime as dt

from src.system_checks.checks import crypto_breaker as chk
from src.system_checks.types import Status


def _run(monkeypatch, *, tick='1', n=2, ts=None, n_exc=None, ts_exc=None):
    if tick is None:
        monkeypatch.delenv('OPENCLAW_CRYPTO_BREAKER_TICK', raising=False)
    else:
        monkeypatch.setenv('OPENCLAW_CRYPTO_BREAKER_TICK', tick)

    def count():
        if n_exc:
            raise n_exc
        return n

    def newest():
        if ts_exc:
            raise ts_exc
        return ts
    monkeypatch.setattr(chk, '_crypto_position_count', count)
    monkeypatch.setattr(chk, '_newest_eval_ts', newest)
    return chk._crypto_breaker_evaluating()


def _ago(minutes, naive=False):
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)
    return t.replace(tzinfo=None) if naive else t


def test_skip_when_tick_off(monkeypatch):
    assert _run(monkeypatch, tick=None)[0] is Status.SKIP
    assert _run(monkeypatch, tick='0')[0] is Status.SKIP


def test_fail_open_when_cli_or_db_unavailable(monkeypatch):
    st, d = _run(monkeypatch, n_exc=FileNotFoundError('alpaca'))
    assert st is Status.SKIP and 'CLI unavailable' in d
    st, d = _run(monkeypatch, ts_exc=RuntimeError('db down'))
    assert st is Status.SKIP and 'DB unavailable' in d


def test_pass_when_flat_or_fresh(monkeypatch):
    assert _run(monkeypatch, n=0, ts=_ago(600))[0] is Status.PASS
    assert _run(monkeypatch, ts=_ago(6))[0] is Status.PASS
    assert _run(monkeypatch, ts=_ago(6, naive=True))[0] is Status.PASS


def test_warn_when_stale_or_empty_while_crypto_held(monkeypatch):
    st, d = _run(monkeypatch, ts=_ago(45))
    assert st is Status.WARN and '2 crypto positions' in d and 'blind' in d
    st, d = _run(monkeypatch, ts=_ago(45, naive=True))
    assert st is Status.WARN
    st, d = _run(monkeypatch, ts=None)
    assert st is Status.WARN and 'empty' in d


def test_position_count_reads_cli(monkeypatch):
    import json
    import subprocess
    import types
    rows = [{'symbol': 'BTCUSD', 'asset_class': 'crypto'}, {'symbol': 'AAPL', 'asset_class': 'us_equity'}]
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout=json.dumps(rows), stderr=''))
    assert chk._crypto_position_count() == 1


def test_registered():
    from src.system_checks.registry import all_checks
    import src.system_checks.checks  # noqa: F401
    meta = all_checks()['crypto_breaker_evaluating']
    assert meta['tags'] == ['broker']
