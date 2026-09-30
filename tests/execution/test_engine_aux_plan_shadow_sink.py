"""Signals memory task 4: the M3 aux plan/drift lines and the M1 last_price
line are also appended to logs/aux_plan_shadow.log (lib.shadow_log), in every
OPENCLAW_AUX_LAZY mode, fail-open. No masters, no DB."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import engine  # noqa: E402


def _lines(tmp_path):
    p = tmp_path / 'aux_plan_shadow.log'
    return p.read_text().splitlines() if p.exists() else []


def _env(monkeypatch, tmp_path, mode=None, compute=False):
    monkeypatch.setenv('OPENCLAW_SHADOW_LOG_DIR', str(tmp_path))
    if mode is None:
        monkeypatch.delenv('OPENCLAW_AUX_LAZY', raising=False)
    else:
        monkeypatch.setenv('OPENCLAW_AUX_LAZY', mode)
    if compute:
        monkeypatch.setenv('OPENCLAW_SIGNALS_SKIP_SHADOW_OPTIONS', '1')
    else:
        monkeypatch.delenv('OPENCLAW_SIGNALS_SKIP_SHADOW_OPTIONS', raising=False)


def _stub_plan(monkeypatch):
    monkeypatch.setattr(engine, '_running_strategy_ids', lambda s, r: {'a', 'b'})
    monkeypatch.setattr(engine, '_needed_aux_kinds', lambda s, ids: set())


def test_plan_line_in_every_mode_with_tags(monkeypatch, tmp_path, caplog):
    _stub_plan(monkeypatch)
    for mode, tag, reason in (('shadow', 'shadow', 'other'), ('1', '1', 'compute'), (None, 'off', 'other')):
        for f in tmp_path.glob('*.log'):
            f.unlink()
        _env(monkeypatch, tmp_path, mode, compute=(reason == 'compute'))
        with caplog.at_level(logging.INFO):
            engine._resolve_aux_load_plan_safe([], {})
        ls = _lines(tmp_path)
        assert len(ls) == 1, (mode, ls)
        assert '[aux_shadow] kind=plan' in ls[0]
        assert f'mode={tag} ' in ls[0] and f'reason={reason} ' in ls[0]
        assert ' date=20' in ls[0]
    assert any('[engine] aux plan:' in r.message for r in caplog.records)  # logger line kept


def test_plan_line_carries_the_logger_text(monkeypatch, tmp_path):
    _stub_plan(monkeypatch)
    _env(monkeypatch, tmp_path, 'shadow')
    engine._resolve_aux_load_plan([], {})
    (line,) = _lines(tmp_path)
    assert '| [engine] aux plan: run=2 strategies; load={' in line


def test_plan_failure_line_logged_and_safe(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, 'shadow')
    monkeypatch.setattr(engine, '_running_strategy_ids', lambda s, r: (_ for _ in ()).throw(RuntimeError('boom')))
    assert engine._resolve_aux_load_plan_safe([], {}) == (None, None, None)
    (line,) = _lines(tmp_path)
    assert 'kind=plan' in line and 'aux plan failed (boom)' in line


def test_drift_line_only_when_raised(monkeypatch, tmp_path, caplog):
    _env(monkeypatch, tmp_path, 'shadow')
    assert engine._log_aux_plan_drift({'a': 1}, {'a'}) == set()
    assert engine._log_aux_plan_drift({'a': 1}, None) == set()
    assert _lines(tmp_path) == []
    with caplog.at_level(logging.WARNING):
        assert engine._log_aux_plan_drift({'a': 1, 'z': 2}, {'a'}) == {'z'}
    (line,) = _lines(tmp_path)
    assert 'kind=drift' in line and "['z']" in line
    assert any('aux plan drift' in r.message for r in caplog.records)


def test_sink_exception_swallowed(monkeypatch, tmp_path, caplog):
    _stub_plan(monkeypatch)
    _env(monkeypatch, tmp_path, 'shadow')
    from lib import shadow_log
    monkeypatch.setattr(shadow_log, 'record', lambda *a, **k: (_ for _ in ()).throw(OSError('disk')))
    with caplog.at_level(logging.WARNING):
        out = engine._resolve_aux_load_plan_safe([], {})
        assert engine._log_aux_plan_drift({'z': 1}, set()) == {'z'}
    assert out == (None, {'a', 'b'}, set())  # plan result unaffected
    assert any('aux shadow sink failed' in r.message for r in caplog.records)


def test_last_price_emit_sites_in_source():
    """load_aux_data needs the masters, so pin the wiring structurally (same
    approach as test_engine_last_price_window.py)."""
    src = Path(engine.__file__).read_text()
    assert src.count("_aux_shadow_sink('last_price'") == 2
    assert "'late_drift'" in src
