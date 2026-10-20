"""scripts/register_dsl_pilots.py against a tmp_path manifest only."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location('register_dsl_pilots', ROOT / 'scripts' / 'register_dsl_pilots.py')
reg = importlib.util.module_from_spec(_SPEC)
sys.modules['register_dsl_pilots'] = reg
_SPEC.loader.exec_module(reg)

BASE = {'schema_version': 1, 'strategies': {
    'S_other': {'state': 'approved', 'state_since': 't', 'metadata': {'x': 1}, 'history': [], 'instrument_class': 'equity'},
}}


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    monkeypatch.delenv('POSTGRES_URI', raising=False)
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps(BASE, indent=2))
    return p


def test_dry_run_writes_nothing(manifest, capsys):
    before = manifest.read_bytes()
    assert reg.main(['--manifest', str(manifest)]) == 0
    assert manifest.read_bytes() == before
    assert 'S_amihud_illiquidity_premium_dsl' in capsys.readouterr().out


def test_apply_inserts_three_candidates_only(manifest):
    assert reg.main(['--manifest', str(manifest), '--apply']) == 0
    got = json.loads(manifest.read_text())['strategies']
    assert got['S_other'] == BASE['strategies']['S_other']
    assert set(got) - {'S_other'} == set(reg.IDS) and len(reg.IDS) == 3
    tl = 'src.strategies.universe_default:tier_liquid'
    want = {'S_golden_cross_trend_signal_tv2_dsl': tl,
            'S_amihud_illiquidity_premium_dsl': 'src.strategies.universe_default:stocks_liquid',
            'oxf_bollinger_momentum_dsl': tl}
    for sid, ref in want.items():
        e = got[sid]
        assert e['state'] == 'candidate' and 'eligible_regimes' not in e and 'eligible_regimes' not in e['metadata']
        assert e['metadata']['universe_filter_ref'] == ref
    am = got['S_amihud_illiquidity_premium_dsl']['metadata']
    assert am['backtest_universe_cap'] == 'tier_liquid' and am['backtest_universe_cap_reason']


def test_apply_is_idempotent(manifest):
    assert reg.main(['--manifest', str(manifest), '--apply']) == 0
    first = manifest.read_bytes()
    assert reg.main(['--manifest', str(manifest), '--apply']) == 0
    assert manifest.read_bytes() == first
