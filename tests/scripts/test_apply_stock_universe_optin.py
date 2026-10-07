"""scripts/apply_stock_universe_optin.py — Phase 2 stock-universe opt-in.
Temp manifests only; never the real src/strategies/manifest.json.

Run: PYTHONPATH=src python3 -m pytest tests/scripts/test_apply_stock_universe_optin.py -q
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    'apply_stock_universe_optin', ROOT / 'scripts' / 'apply_stock_universe_optin.py')
optin = importlib.util.module_from_spec(_SPEC)
sys.modules['apply_stock_universe_optin'] = optin
_SPEC.loader.exec_module(optin)

TARGET = 'src.strategies.universe_default:stocks_liquid'


def _entry(ref=None, extra_meta=None, state='live'):
    meta = {'canonical_file': 'x.py', 'class': 'X'}
    if ref is not None:
        meta['universe_filter_ref'] = ref
    meta.update(extra_meta or {})
    return {'state': state, 'state_since': '2026-07-18T19:17:15.485Z', 'metadata': meta,
            'history': [{'from_state': 'candidate', 'to_state': state,
                         'timestamp': '2026-07-18T19:17:15.485Z', 'actor': 'system',
                         'reason': 'r', 'metadata': {}}],
            'instrument_class': 'equity'}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv('POSTGRES_URI', raising=False)


@pytest.fixture
def world(tmp_path):
    manifest = {'schema_version': 1, 'decommissioned': ['old'], 'strategies': {
        'A': _entry('src.strategies.universe_default:tier_liquid'),
        'B': _entry(None, state='candidate'),
        'C': _entry(TARGET),
        'D': _entry('src.strategies.universe_default:sp500'),   # not listed
    }}
    mp = tmp_path / 'manifest.json'
    mp.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    lp = tmp_path / 'list.txt'
    lp.write_text(f"# comment\n\nA\t{TARGET}\nB\t{TARGET}\n  \nC\t{TARGET}\n", encoding='utf-8')
    return mp, lp, tmp_path / 'audit.json', manifest


def _run(world, apply, **kw):
    mp, lp, ao, _ = world
    return optin.run(kw.get('lp', lp), mp, ao, apply=apply)


def _forbid_lock_and_write(monkeypatch):
    def boom(*a, **k):
        raise AssertionError('lock/write used')
    monkeypatch.setattr(optin._ml, 'manifest_lock', boom)
    monkeypatch.setattr(optin._ml, 'write_atomic', boom)


def test_parse_list(world):
    _, lp, _, _ = world
    assert optin.parse_list(lp) == [('A', TARGET), ('B', TARGET), ('C', TARGET)]


def test_parse_list_bad_line(tmp_path):
    p = tmp_path / 'l.txt'
    p.write_text('A only-one-column\n')
    with pytest.raises(optin.RefusedError):
        optin.parse_list(p)


def test_dry_run_writes_nothing_no_lock(world, monkeypatch, capsys):
    mp, _, ao, _ = world
    before = mp.read_bytes()
    _forbid_lock_and_write(monkeypatch)
    assert _run(world, False) == 0
    out = capsys.readouterr().out
    assert mp.read_bytes() == before and not ao.exists()
    assert 'DRY-RUN' in out and 'byte-stable' in out and 'next signals run' in out
    assert 'C: no-op' in out and '--- ' in out
    assert not Path(str(mp) + '.lock').exists()


def test_apply_changes_exactly_four_metadata_keys(world, capsys):
    mp, _, ao, orig = world
    assert _run(world, True) == 0
    after = json.loads(mp.read_text())
    assert after['decommissioned'] == orig['decommissioned']
    assert after['strategies']['D'] == orig['strategies']['D']
    assert after['strategies']['C'] == orig['strategies']['C']      # already target
    for sid in 'AB':
        e, o = after['strategies'][sid], orig['strategies'][sid]
        assert e['metadata']['universe_filter_ref'] == TARGET
        assert e['history'] == o['history']
        assert e['metadata']['universe_filter_ref_changed_by'] == 'manual:operator'
        assert e['metadata']['universe_filter_ref_changed_at'].endswith('Z')
        e2, o2 = copy.deepcopy(e), copy.deepcopy(o)
        for x in (e2, o2):
            x['metadata'].pop('universe_filter_ref', None)
            for k in optin.PROVENANCE_KEYS:
                x['metadata'].pop(k, None)
        assert e2 == o2
    assert after['strategies']['A']['metadata']['universe_filter_ref_prior'] == \
        'src.strategies.universe_default:tier_liquid'
    assert after['strategies']['B']['metadata']['universe_filter_ref_prior'] == '<none>'
    assert 'next signals run' in capsys.readouterr().out
    assert not Path(str(mp) + '.lock').exists()


def test_history_unchanged_and_provenance_in_metadata(world):
    mp, _, _, orig = world
    _run(world, True)
    after = json.loads(mp.read_text())['strategies']
    for sid in 'ABCD':
        assert after[sid]['history'] == orig['strategies'][sid]['history']
    ma = after['A']['metadata']
    assert ma['universe_filter_ref_changed_by'] == 'manual:operator'
    ts = ma['universe_filter_ref_changed_at']
    assert ts.endswith('Z') and 'T' in ts and len(ts) == 24
    assert 'universe_filter_ref_changed_at' not in after['C']['metadata']   # no-op entry


def test_changed_at_by_overwritten_prior_kept(world):
    mp, *_ = world
    d = json.loads(mp.read_text())
    m = d['strategies']['A']['metadata']
    m.update(universe_filter_ref_prior='FIRST', universe_filter_ref_changed_at='old',
             universe_filter_ref_changed_by='old')
    mp.write_text(json.dumps(d, indent=2))
    _run(world, True)
    m = json.loads(mp.read_text())['strategies']['A']['metadata']
    assert m['universe_filter_ref_prior'] == 'FIRST'
    assert m['universe_filter_ref_changed_by'] == 'manual:operator'
    assert m['universe_filter_ref_changed_at'] != 'old'


def test_first_prior_kept(world):
    mp, lp, ao, _ = world
    d = json.loads(mp.read_text())
    d['strategies']['A']['metadata']['universe_filter_ref_prior'] = 'FIRST'
    mp.write_text(json.dumps(d, indent=2))
    _run(world, True)
    m = json.loads(mp.read_text())['strategies']['A']['metadata']
    assert m['universe_filter_ref_prior'] == 'FIRST' and m['universe_filter_ref'] == TARGET


def test_idempotent_second_apply(world, monkeypatch):
    mp, _, ao, _ = world
    assert _run(world, True) == 0
    b1, a1 = mp.read_bytes(), ao.read_bytes()
    calls = []
    monkeypatch.setattr(optin._ml, 'write_atomic', lambda *a, **k: calls.append(1))
    assert _run(world, True) == 0
    assert calls == [] and mp.read_bytes() == b1 and ao.read_bytes() == a1


def test_audit_content(world):
    _, _, ao, _ = world
    _run(world, True)
    assert json.loads(ao.read_text()) == {
        'A': {'prior': 'src.strategies.universe_default:tier_liquid', 'new': TARGET, 'changed': True},
        'B': {'prior': '<none>', 'new': TARGET, 'changed': True},
        'C': {'prior': None, 'new': TARGET, 'changed': False},
    }


@pytest.mark.parametrize('apply', [False, True])
def test_unknown_strategy_refuses_whole_run(world, tmp_path, apply, capsys):
    mp, lp, ao, _ = world
    lp.write_text(f"A\t{TARGET}\nNOPE\t{TARGET}\n")
    before = mp.read_bytes()
    assert _run(world, apply) == 1
    assert mp.read_bytes() == before and not ao.exists()
    assert 'NOPE' in capsys.readouterr().err


@pytest.mark.parametrize('ref', [
    'src.strategies.universe_default:not_a_predicate',   # not in the allowed sets
    'no_colon_here',
    'src.strategies.nonexistent_mod:stocks_liquid',      # unresolvable module
])
def test_bad_ref_refuses(world, ref):
    mp, lp, ao, _ = world
    lp.write_text(f"A\t{TARGET}\nB\t{ref}\n")
    before = mp.read_bytes()
    assert _run(world, True) == 1
    assert mp.read_bytes() == before and not ao.exists()


def test_allowed_name_but_unresolvable_module_refuses(world):
    mp, lp, ao, _ = world
    lp.write_text("A\tsrc.strategies.nonexistent_mod:stocks_liquid\n")
    before = mp.read_bytes()
    assert _run(world, True) == 1 and mp.read_bytes() == before


def test_duplicate_id_refuses(world):
    mp, lp, ao, _ = world
    lp.write_text(f"A\t{TARGET}\nA\t{TARGET}\n")
    before = mp.read_bytes()
    assert _run(world, True) == 1
    assert mp.read_bytes() == before and not ao.exists()


def test_main_default_is_dry_run(world, monkeypatch):
    mp, lp, ao, _ = world
    before = mp.read_bytes()
    assert optin.main(['--list', str(lp), '--manifest', str(mp), '--audit-out', str(ao)]) == 0
    assert mp.read_bytes() == before and not ao.exists()


def test_real_list_parses_and_validates():
    items = optin.parse_list(optin.DEFAULT_LIST)
    assert len(items) == 21
    strategies = {sid: {'state': 'live', 'metadata': {}, 'history': []} for sid, _ in items}
    optin.validate(items, strategies)
