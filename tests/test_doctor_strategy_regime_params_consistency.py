"""Tests for doctor.check_strategy_regime_params_consistency."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import doctor as doc  # noqa: E402


def _stub(monkeypatch, registry_ids, param_rows, raise_=None):
    """registry_ids: set of strategy_ids in strategy_registry.
    param_rows: list of (strategy_id, regime_state) tuples in strategy_regime_params.
    """
    def fake_query(sql, params=()):
        if raise_:
            raise raise_
        if 'strategy_registry' in sql:
            return [(sid,) for sid in registry_ids]
        if 'strategy_regime_params' in sql:
            return list(param_rows)
        return []
    monkeypatch.setattr(doc, '_query_consistency', fake_query)


REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')


def test_full_grid_returns_pass(monkeypatch):
    sids = {'s1', 's2'}
    rows = [(s, r) for s in sids for r in REGIMES]
    _stub(monkeypatch, sids, rows)
    r = doc.check_strategy_regime_params_consistency()
    assert r['severity'] == doc.PASS


def test_few_missing_returns_warn(monkeypatch):
    sids = {'s1', 's2'}
    rows = [(s, r) for s in sids for r in REGIMES if not (s == 's1' and r == 'CRISIS')]
    _stub(monkeypatch, sids, rows)
    r = doc.check_strategy_regime_params_consistency()
    assert r['severity'] == doc.WARN
    assert 'missing' in r['detail'].lower() or 's1' in r['detail']


def test_many_missing_returns_fail(monkeypatch):
    sids = {'s1', 's2', 's3', 's4'}
    rows = [('s1', 'LOW_VOL')]
    _stub(monkeypatch, sids, rows)
    r = doc.check_strategy_regime_params_consistency()
    assert r['severity'] == doc.FAIL


def test_orphan_params_row_returns_warn(monkeypatch):
    sids = {'s1'}
    rows = [(s, r) for s in sids for r in REGIMES] + [('orphan', 'LOW_VOL')]
    _stub(monkeypatch, sids, rows)
    r = doc.check_strategy_regime_params_consistency()
    assert r['severity'] == doc.WARN
    assert 'orphan' in r['detail'].lower()


def test_db_error_returns_warn(monkeypatch):
    _stub(monkeypatch, set(), [], raise_=RuntimeError('db down'))
    r = doc.check_strategy_regime_params_consistency()
    assert r['severity'] == doc.WARN
