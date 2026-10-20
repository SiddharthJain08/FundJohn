"""Parity of the three `_cached` measurement twins vs their parents (six-repo D2d).

The twins are never registered (coordinator ruling 2); they exist so the
indicator cache can be measured on real strategies.  Acceptance is byte-for-byte
signal identity on every bar from min_lookback + 5, for two seeded panels.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT / 'src'), str(ROOT), str(ROOT / 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.measure_indicator_cache import make_panel, _sig_record  # noqa: E402

PAIRS = {
    'S_ast_trend_following_effect_in_stocks': 'AstTrendFollowingEffectInStocks',
    'S_tr_02_hurst_regime_flip': 'HurstRegimeFlip',
    'S_two_day_pullback_atr_uptrend': 'TwoDayPullbackAtrUptrend',
}
SIDS = list(PAIRS)
IMPL = ROOT / 'src' / 'strategies' / 'implementations'
MARK = "measurement twin — never register; emits the parent's signals"


def _mods(sid):
    parent = importlib.import_module(f'strategies.implementations.{sid}')
    twin = importlib.import_module(f'strategies.implementations.{sid}_cached')
    return parent, twin


def _universe(sid, panel):
    return [c for c in panel.columns if c not in ('SPY', 'QQQ')]


def _gen(inst, frame, regime, universe):
    return inst.generate_signals(frame, regime, universe, aux_data={'options': {}})


def _records(inst, panel, regime, universe, start_i, source=None):
    if source is not None:
        inst._cache_source = source
    out = []
    for i in range(start_i, len(panel)):
        frame = panel.iloc[:i + 1]
        for s in _gen(inst, frame, regime, universe) or []:
            out.append(_sig_record(frame.index[-1], s))
    return out


@pytest.mark.parametrize('seed', [0, 1])
@pytest.mark.parametrize('sid', SIDS)
def test_twin_signals_identical_to_parent(sid, seed):
    parent_m, twin_m = _mods(sid)
    pcls, tcls = getattr(parent_m, PAIRS[sid]), getattr(twin_m, PAIRS[sid])
    panel, _bars, _reg = make_panel(days=330, tickers=30, seed=seed)
    regime = {'state': pcls.active_in_regimes[0]}
    uni = _universe(sid, panel)
    start_i = pcls.min_lookback + 5
    with open(os.devnull, 'w') as dn, patch.object(sys, 'stderr', dn):
        ref = _records(pcls(), panel, regime, uni, start_i)
        cached = _records(tcls(), panel, regime, uni, start_i, source=panel)
        plain = _records(tcls(), panel, regime, uni, start_i)
    assert ref, 'parent emitted no signals: test would be vacuous'
    assert cached == ref
    assert plain == ref


@pytest.mark.parametrize('sid', SIDS)
def test_twin_validates_with_parity_flag(sid):
    import strategies.validate_strategy as vs
    with patch.dict(os.environ, {'OPENCLAW_BT_CACHE_PARITY': '1'}):
        res = vs.validate(str(IMPL / f'{sid}_cached.py'))
    assert res['ok'] is True, res
    blob = json.dumps(res, default=str)
    assert 'cache_parity_vacuous' not in blob


@pytest.mark.parametrize('sid', SIDS)
def test_twin_class_is_the_one_validate_picks(sid):
    # validate_strategy takes the first BaseStrategy subclass among the module's
    # members (alphabetical); the parent is imported by module alias, so the twin
    # class must be the only candidate.
    import inspect
    from strategies.base import BaseStrategy
    _p, twin_m = _mods(sid)
    cands = [o for _, o in inspect.getmembers(twin_m, inspect.isclass)
             if o is not BaseStrategy and issubclass(o, BaseStrategy)]
    assert len(cands) == 1, cands
    assert cands[0].id == f'{sid}_cached'


def test_twin_cache_hits_dominate():
    for sid in SIDS:
        parent_m, twin_m = _mods(sid)
        tcls = getattr(twin_m, PAIRS[sid])
        panel, _b, _r = make_panel(days=330, tickers=30, seed=0)
        regime = {'state': tcls.active_in_regimes[0]}
        inst = tcls()
        with open(os.devnull, 'w') as dn, patch.object(sys, 'stderr', dn):
            _records(inst, panel, regime, _universe(sid, panel),
                     tcls.min_lookback + 5, source=panel)
        st = inst._bt_cache_stats
        assert st['hit'] > st['miss'], (sid, st)


def test_twins_never_registered():
    import strategies.registry as reg
    with open(ROOT / 'src' / 'strategies' / 'manifest.json') as fh:
        manifest = json.load(fh)['strategies']
    for sid in SIDS:
        tid = f'{sid}_cached'
        assert tid not in reg._IMPL_MAP
        assert tid not in manifest
        assert not (IMPL / f'{tid}.requirements.json').exists()


@pytest.mark.parametrize('sid', SIDS)
def test_twin_docstring_marks_measurement_only(sid):
    _p, twin_m = _mods(sid)
    assert MARK in (twin_m.__doc__ or '')


def _gate_panel(start_i):
    """Tiny panel hitting the parents' length/min-bars gates and NaN handling."""
    panel, _b, _r = make_panel(days=330, tickers=12, seed=3)
    tick = [c for c in panel.columns if c not in ('SPY', 'QQQ')]
    panel = panel.copy()
    panel.loc[panel.index[:start_i - 3], tick[0]] = float('nan')   # lists 3 bars before start
    panel.loc[panel.index[start_i + 20], :] = float('nan')         # fully-NaN row mid-run
    panel.loc[panel.index[start_i + 40], ['SPY', 'QQQ']] = float('nan')
    return panel


@pytest.mark.parametrize('sid', SIDS)
def test_twin_parity_on_gate_coverage_panel(sid):
    parent_m, twin_m = _mods(sid)
    pcls, tcls = getattr(parent_m, PAIRS[sid]), getattr(twin_m, PAIRS[sid])
    start_i = pcls.min_lookback + 5
    panel = _gate_panel(start_i)
    regime = {'state': pcls.active_in_regimes[0]}
    uni = _universe(sid, panel)
    with open(os.devnull, 'w') as dn, patch.object(sys, 'stderr', dn):
        ref = _records(pcls(), panel, regime, uni, start_i)
        cached = _records(tcls(), panel, regime, uni, start_i, source=panel)
        plain = _records(tcls(), panel, regime, uni, start_i)
    assert ref, 'parent emitted no signals: test would be vacuous'
    assert cached == ref
    assert plain == ref


def _twin_stub_dir(tmp_path):
    d = tmp_path / 'implementations'
    d.mkdir()
    twin = next(IMPL.glob('S_*_cached.py'))
    (d / twin.name).write_text(twin.read_text())
    (d / 'S_normal_stub.py').write_text('"""normal"""\nx = 1\n')
    return d, twin.stem


def test_generate_sidecars_skips_twin(tmp_path, monkeypatch):
    import strategies.generate_sidecars as gs
    d, tid = _twin_stub_dir(tmp_path)
    monkeypatch.setattr(gs, 'IMPL_DIR', d)
    gs.main()
    assert not (d / f'{tid}.requirements.json').exists()
    assert (d / 'S_normal_stub.requirements.json').exists()


def test_generate_missing_requirements_skips_twin(tmp_path, monkeypatch):
    import scripts.generate_missing_requirements as gm
    d, tid = _twin_stub_dir(tmp_path)
    monkeypatch.setattr(gm, 'IMPL_DIR', d)
    gm.process(dry_run=False)
    assert not (d / f'{tid}.requirements.json').exists()
    assert (d / 'S_normal_stub.requirements.json').exists()
