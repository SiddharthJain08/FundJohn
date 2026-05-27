"""SP-4: lifecycle.register() AST-detects a module-level INSTRUMENT_CLASS
from the impl file, gated by OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT.
Run: pytest tests/test_lifecycle_instrument_class_detect.py -v
"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import strategies.lifecycle as lc  # noqa: E402
from strategies.lifecycle import LifecycleStateMachine, StrategyState  # noqa: E402


def _write_impl(tmp_path, body):
    p = tmp_path / 'S_probe.py'
    p.write_text(body, encoding='utf-8')
    return p


def test_detect_reads_module_constant(tmp_path):
    p = _write_impl(tmp_path, 'INSTRUMENT_CLASS = "option"\n\nclass X: pass\n')
    assert lc._detect_module_instrument_class(p) == 'option'


def test_detect_none_when_absent(tmp_path):
    p = _write_impl(tmp_path, 'class X: pass\n')
    assert lc._detect_module_instrument_class(p) is None


def test_detect_ignores_unknown_value(tmp_path):
    p = _write_impl(tmp_path, 'INSTRUMENT_CLASS = "banana"\n')
    assert lc._detect_module_instrument_class(p) is None


def test_register_sets_instrument_class_when_gate_on(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT', '1')
    monkeypatch.setattr(lc, '_IMPLEMENTATIONS_DIR', tmp_path)
    _write_impl(tmp_path, 'INSTRUMENT_CLASS = "crypto"\nclass X: pass\n')
    sm = LifecycleStateMachine.new_empty()
    rec = sm.register('S_probe', initial_state=StrategyState.CANDIDATE,
                      metadata={'canonical_file': 'S_probe.py'})
    assert rec.instrument_class == 'crypto'


def test_register_defaults_equity_when_gate_off(tmp_path, monkeypatch):
    monkeypatch.delenv('OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT', raising=False)
    monkeypatch.setattr(lc, '_IMPLEMENTATIONS_DIR', tmp_path)
    _write_impl(tmp_path, 'INSTRUMENT_CLASS = "crypto"\nclass X: pass\n')
    sm = LifecycleStateMachine.new_empty()
    rec = sm.register('S_probe', initial_state=StrategyState.CANDIDATE,
                      metadata={'canonical_file': 'S_probe.py'})
    assert rec.instrument_class == 'equity'
