"""Phase 3 (2026-10-06 universe parity epoch): _bounded_resolver_info records HOW the
universe was bound so config_json can carry it; fixture-artifact tests, no DB."""
from __future__ import annotations
import inspect
import json
import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import pandas as pd
import pytest

from backtest import unified_backtest as ub
from backtest.unified_backtest import _bounded_resolver, _bounded_resolver_info

REF = 'src.strategies.universe_default:{}'


def _manifest(tmp_path, ref=None, cap=None):
    meta = {}
    if ref:
        meta['universe_filter_ref'] = REF.format(ref)
    if cap:
        meta['backtest_universe_cap'] = cap
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps({'strategies': {'S_x': {'state': 'live', 'metadata': meta}}}))
    return p


def _artifact(tmp_path, tiers=('tier_liquid', 'stocks_liquid'), name='universe_tier_membership_shrink-20261006.parquet'):
    d = tmp_path / 'data'
    d.mkdir(exist_ok=True)
    pd.DataFrame([{'run_id': 'r', 'tier': t, 'snapshot_date': '2026-09-30',
                   'symbols': ['AAA', 'BBB'] if t != 'stocks_liquid' else ['AAA']}
                  for t in tiers]).to_parquet(d / name, index=False)
    return d


@pytest.fixture(autouse=True)
def _flag(monkeypatch):
    monkeypatch.setenv('OPENCLAW_BT_UNIVERSE_FILTER_REF', '1')


def test_filter_ref_records_tier_and_source(tmp_path):
    r, info = _bounded_resolver_info('S_x', manifest_path=_manifest(tmp_path, 'tier_liquid'),
                                     data_dir=_artifact(tmp_path))
    assert r is not None
    assert info == {'universe_bound_source': 'filter_ref', 'universe_filter_ref_tier': 'tier_liquid',
                    'universe_bound_tier': 'tier_liquid', 'universe_filter_ref_unresolved': None}


def test_stocks_tier_resolves_through_precomputed_resolver(tmp_path):
    r, info = _bounded_resolver_info('S_x', manifest_path=_manifest(tmp_path, 'stocks_liquid'),
                                     data_dir=_artifact(tmp_path))
    assert info['universe_bound_source'] == 'filter_ref'
    assert info['universe_filter_ref_tier'] == 'stocks_liquid'
    assert r.resolve('S_x', date(2026, 10, 2)) == ['AAA']


def test_stale_artifact_without_stocks_tier_fails_open_with_warning(tmp_path, monkeypatch):
    logs = []
    monkeypatch.setattr(ub, '_log', logs.append)
    r, info = _bounded_resolver_info('S_x', manifest_path=_manifest(tmp_path, 'stocks_liquid'),
                                     data_dir=_artifact(tmp_path, tiers=('tier_liquid', 'sp500')))
    assert r is None
    assert info['universe_bound_source'] == 'none'
    assert info['universe_filter_ref_tier'] is None
    assert info['universe_filter_ref_unresolved'] == 'stocks_liquid'
    assert any('WARNING' in m and "'stocks_liquid'" in m for m in logs)


def test_missing_artifact_fails_open(tmp_path, monkeypatch):
    logs = []
    monkeypatch.setattr(ub, '_log', logs.append)
    empty = tmp_path / 'nodata'
    empty.mkdir()
    r, info = _bounded_resolver_info('S_x', manifest_path=_manifest(tmp_path, 'tier_liquid'), data_dir=empty)
    assert r is None and info['universe_bound_source'] == 'none'
    assert info['universe_filter_ref_unresolved'] == 'tier_liquid'
    assert any('WARNING' in m for m in logs)


def test_explicit_manifest_cap_is_source_cap(tmp_path):
    r, info = _bounded_resolver_info('S_x', manifest_path=_manifest(tmp_path, 'sp500', cap='tier_liquid'),
                                     data_dir=_artifact(tmp_path))
    assert r is not None
    assert info['universe_bound_source'] == 'cap'
    assert info['universe_bound_tier'] == 'tier_liquid'
    assert info['universe_filter_ref_tier'] is None


def test_cap_override_is_source_cap(tmp_path):
    _, info = _bounded_resolver_info('S_x', manifest_path=_manifest(tmp_path),
                                     data_dir=_artifact(tmp_path), cap_override='tier_liquid')
    assert info['universe_bound_source'] == 'cap'


def test_no_ref_no_cap_is_none(tmp_path):
    r, info = _bounded_resolver_info('S_x', manifest_path=_manifest(tmp_path), data_dir=_artifact(tmp_path))
    assert r is None and info['universe_bound_source'] == 'none'
    assert info['universe_filter_ref_unresolved'] is None


def test_flag_off_ref_is_none(tmp_path, monkeypatch):
    monkeypatch.delenv('OPENCLAW_BT_UNIVERSE_FILTER_REF')
    r, info = _bounded_resolver_info('S_x', manifest_path=_manifest(tmp_path, 'tier_liquid'),
                                     data_dir=_artifact(tmp_path))
    assert r is None and info['universe_bound_source'] == 'none'


def test_newest_shrink_artifact_preferred(tmp_path):
    d = _artifact(tmp_path, tiers=('tier_liquid',), name='universe_tier_membership_shrink-20260101.parquet')
    _artifact(tmp_path, tiers=('tier_liquid', 'stocks_liquid'))
    r, info = _bounded_resolver_info('S_x', manifest_path=_manifest(tmp_path, 'stocks_liquid'), data_dir=d)
    assert info['universe_bound_source'] == 'filter_ref'


def test_legacy_wrapper_unchanged(tmp_path):
    r = _bounded_resolver('S_x', manifest_path=_manifest(tmp_path, 'tier_liquid'), data_dir=_artifact(tmp_path))
    assert sorted(r.resolve('S_x', date(2026, 10, 2))) == ['AAA', 'BBB']


def test_config_json_writes_bound_info_keys():
    """The persisted config_json spreads the bound-info dict in the same function that resolves it."""
    src = inspect.getsource(ub)
    assert "resolver, _bound_info = _bounded_resolver_info(" in src
    assert "**_bound_info," in src
