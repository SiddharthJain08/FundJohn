"""SP-4: unified_backtest CLI resolves instrument_class for dispatch.
Run: pytest tests/test_backtest_instrument_class_dispatch.py -v
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub  # noqa: E402
from backtest import options_backtest  # noqa: E402


def _fake_root(tmp_path, strategies: dict):
    """Build a tmp tree with src/strategies/manifest.json and point ub.ROOT at it."""
    d = tmp_path / 'src' / 'strategies'
    d.mkdir(parents=True)
    (d / 'manifest.json').write_text(json.dumps({'strategies': strategies}))
    return tmp_path


def test_manifest_option_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(ub, 'ROOT', _fake_root(tmp_path, {'S_x': {'instrument_class': 'option'}}))
    assert ub._resolve_instrument_class('S_x') == 'option'


def test_manifest_crypto_returned_verbatim(tmp_path, monkeypatch):
    monkeypatch.setattr(ub, 'ROOT', _fake_root(tmp_path, {'S_x': {'instrument_class': 'crypto'}}))
    assert ub._resolve_instrument_class('S_x') == 'crypto'


def test_invalid_manifest_value_falls_back_to_equity(tmp_path, monkeypatch):
    monkeypatch.setattr(ub, 'ROOT', _fake_root(tmp_path, {'S_x': {'instrument_class': 'banana'}}))
    assert ub._resolve_instrument_class('S_x') == 'equity'


def test_module_const_fallback_when_absent_from_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(ub, 'ROOT', _fake_root(tmp_path, {}))   # S_x not in manifest
    impl = tmp_path / 'S_x.py'
    impl.write_text("INSTRUMENT_CLASS = 'option'\n\nclass X: pass\n")
    assert ub._resolve_instrument_class('S_x', filepath=str(impl)) == 'option'


def test_default_equity_when_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(ub, 'ROOT', _fake_root(tmp_path, {}))
    assert ub._resolve_instrument_class('S_x') == 'equity'


# NOTE: these used to assert against the live manifest via S_short_straddle_vrp
# (the SP-4 reference option strategy). The candidate reaper removed it
# (commit 1c2a17f), which is a legitimate lifecycle event — so the tests now
# pin the resolution path with a fake manifest instead of depending on any
# specific live strategy surviving.

def test_manifest_reference_option_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr(ub, 'ROOT', _fake_root(
        tmp_path, {'S_opt_ref': {'instrument_class': 'option'}}))
    assert ub._resolve_instrument_class('S_opt_ref') == 'option'


def test_main_strategy_id_threads_instrument_class(tmp_path, monkeypatch):
    monkeypatch.setattr(ub, 'ROOT', _fake_root(
        tmp_path, {'S_opt_ref': {'instrument_class': 'option'}}))
    captured = {}
    monkeypatch.setattr(ub, 'run_backtest',
                        lambda sid, **kw: captured.update({'sid': sid, **kw}) or 'run-id')
    monkeypatch.setattr(sys, 'argv', ['prog', '--strategy-id', 'S_opt_ref'])
    assert ub.main() == 0
    assert captured['sid'] == 'S_opt_ref'
    assert captured['instrument_class'] == 'option'   # from the (fake) manifest


def test_main_strategy_file_uses_module_const(tmp_path, monkeypatch):
    # A file whose stem is NOT in the manifest -> resolver falls back to the const.
    impl = tmp_path / 'S_probe_opt.py'
    impl.write_text("INSTRUMENT_CLASS = 'option'\n\nclass X: pass\n")
    captured = {}
    monkeypatch.setattr(ub, 'run_backtest',
                        lambda sid, **kw: captured.update({'sid': sid, **kw}) or 'run-id')
    monkeypatch.setattr(sys, 'argv', ['prog', '--strategy-file', str(impl)])
    assert ub.main() == 0
    assert captured['instrument_class'] == 'option'


def test_main_all_live_threads_per_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr(ub, 'ROOT', _fake_root(
        tmp_path, {'S_opt_ref': {'instrument_class': 'option'}}))
    monkeypatch.setattr(ub, '_all_live_strategies', lambda: ['S_opt_ref'])
    seen = []
    monkeypatch.setattr(ub, 'run_backtest',
                        lambda sid, **kw: seen.append((sid, kw.get('instrument_class'))) or 'run-id')
    monkeypatch.setattr(sys, 'argv', ['prog', '--all-live'])
    assert ub.main() == 0
    assert ('S_opt_ref', 'option') in seen


def test_resolution_composes_with_dispatch(tmp_path, monkeypatch):
    # End-to-end link without running a backtest: resolved class -> correct sim fn.
    monkeypatch.setattr(ub, 'ROOT', _fake_root(
        tmp_path, {'S_opt_ref': {'instrument_class': 'option'}}))
    assert ub._simulate_for(ub._resolve_instrument_class('S_opt_ref')) is options_backtest.simulate


def test_equity_strategy_resolves_equity_and_dispatches_per_bar():
    # Byte-identity guard: a normal equity strategy still resolves to 'equity'
    # and dispatches to _per_bar_simulate (the change is confined to 'option').
    strategies = json.load(open(ROOT / 'src' / 'strategies' / 'manifest.json'))['strategies']
    equity_sid = next(sid for sid, e in strategies.items()
                      if e.get('instrument_class', 'equity') == 'equity')
    assert ub._resolve_instrument_class(equity_sid) == 'equity'
    assert ub._simulate_for(ub._resolve_instrument_class(equity_sid)).__name__ == '_per_bar_simulate'
