import pytest
from src.maintenance.doctor import _check_metadata_snapshot_freshness, _check_union_universe_size

def test_metadata_freshness_passes_when_fresh(monkeypatch):
    monkeypatch.setattr("src.maintenance.doctor._latest_snapshot_age_days", lambda: 1)
    code, msg = _check_metadata_snapshot_freshness()
    assert code == 0

def test_metadata_freshness_warns_at_2d(monkeypatch):
    monkeypatch.setattr("src.maintenance.doctor._latest_snapshot_age_days", lambda: 2)
    code, msg = _check_metadata_snapshot_freshness()
    assert code == 1

def test_metadata_freshness_fails_at_4d(monkeypatch):
    monkeypatch.setattr("src.maintenance.doctor._latest_snapshot_age_days", lambda: 4)
    code, msg = _check_metadata_snapshot_freshness()
    assert code == 2

def test_union_size_warn_below_floor(monkeypatch):
    monkeypatch.setattr("src.maintenance.doctor._union_universe_size", lambda: (150, None))
    code, msg = _check_union_universe_size()
    assert code == 1

def test_union_size_fail_below_50(monkeypatch):
    monkeypatch.setattr("src.maintenance.doctor._union_universe_size", lambda: (30, None))
    code, msg = _check_union_universe_size()
    assert code == 2

def test_union_size_passes_at_floor(monkeypatch):
    monkeypatch.setattr("src.maintenance.doctor._union_universe_size", lambda: (200, None))
    code, msg = _check_union_universe_size()
    assert code == 0

def test_union_size_fails_on_resolver_error(monkeypatch):
    monkeypatch.setattr("src.maintenance.doctor._union_universe_size", lambda: (0, "resolver exit=1"))
    code, msg = _check_union_universe_size()
    assert code == 2
    assert "resolver exit=1" in msg
