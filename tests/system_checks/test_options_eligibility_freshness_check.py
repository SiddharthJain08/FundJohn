import json
from src.system_checks.checks import options_eligibility_freshness as chk
from src.system_checks.types import Status


def test_warn_when_cache_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(chk, '_CACHE', tmp_path / 'nope.json')
    status, _ = chk._options_eligibility_freshness()
    assert status is Status.WARN


def test_warn_when_below_floor(tmp_path, monkeypatch):
    p = tmp_path / 'cache.json'
    p.write_text(json.dumps({'AAPL': True}))
    monkeypatch.setattr(chk, '_CACHE', p)
    monkeypatch.setattr(chk, '_MIN_ELIGIBLE', 1000)
    status, detail = chk._options_eligibility_freshness()
    assert status is Status.WARN and 'eligible' in detail


def test_pass_when_fresh_and_populated(tmp_path, monkeypatch):
    p = tmp_path / 'cache.json'
    p.write_text(json.dumps({f'S{i}': True for i in range(1500)}))
    monkeypatch.setattr(chk, '_CACHE', p)
    monkeypatch.setattr(chk, '_MIN_ELIGIBLE', 1000)
    status, _ = chk._options_eligibility_freshness()
    assert status is Status.PASS
