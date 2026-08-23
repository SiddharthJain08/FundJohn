import json
import os
import time

from src.system_checks.checks import fmp_profile_freshness as chk
from src.system_checks.types import Status


def _write(path, n_with_sector, n_tombstones=0, mtime_days_ago=0):
    data = {f'S{i}': {'sector': 'Tech', '_fetched_at': 'x'} for i in range(n_with_sector)}
    data.update({f'T{i}': {'_empty': True, '_fetched_at': 'x'} for i in range(n_tombstones)})
    path.write_text(json.dumps(data))
    ts = time.time() - mtime_days_ago * 86400
    os.utime(path, (ts, ts))


def test_warn_when_cache_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(chk, '_CACHE', tmp_path / 'nope.json')
    status, detail = chk._fmp_profile_freshness()
    assert status is Status.WARN and 'missing' in detail


def test_warn_when_too_few_sectors(tmp_path, monkeypatch):
    p = tmp_path / 'fmp_profile.json'
    _write(p, n_with_sector=20, n_tombstones=5)
    monkeypatch.setattr(chk, '_CACHE', p)
    monkeypatch.setattr(chk, '_MIN_WITH_SECTOR', 1000)
    status, detail = chk._fmp_profile_freshness()
    assert status is Status.WARN and 'sector' in detail


def test_warn_when_stale(tmp_path, monkeypatch):
    p = tmp_path / 'fmp_profile.json'
    _write(p, n_with_sector=1500, mtime_days_ago=20)
    monkeypatch.setattr(chk, '_CACHE', p)
    monkeypatch.setattr(chk, '_MIN_WITH_SECTOR', 1000)
    status, detail = chk._fmp_profile_freshness()
    assert status is Status.WARN and 'stale' in detail


def test_pass_when_fresh_and_populated(tmp_path, monkeypatch):
    p = tmp_path / 'fmp_profile.json'
    _write(p, n_with_sector=1500, n_tombstones=10, mtime_days_ago=1)
    monkeypatch.setattr(chk, '_CACHE', p)
    monkeypatch.setattr(chk, '_MIN_WITH_SECTOR', 1000)
    status, detail = chk._fmp_profile_freshness()
    assert status is Status.PASS and '1500' in detail
