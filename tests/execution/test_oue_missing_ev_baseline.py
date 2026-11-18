"""tests/execution/test_oue_missing_ev_baseline.py

After the 2026-11-18 EV-prefilter retirement the structured handoff no longer
carries ev_gbm. The OUE classifier must use an expectation baseline of 0.0
when hv21 is present (mirrors backtest_panel, which classifies with ev_gbm=0),
and return None only when hv21 itself is missing.
"""
from __future__ import annotations

import json

from src.execution import oue_classifier as oue


def test_load_signal_ev_defaults_missing_ev_to_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(oue, 'HANDOFF_DIR', tmp_path)
    (tmp_path / '2026-11-17_structured.json').write_text(json.dumps(
        {'signals': [{'ticker': 'AAA', 'strategy_id': 'S_x', 'hv21': 0.4}]}))
    assert oue._load_signal_ev('2026-11-17', 'AAA', 'S_x') == {'ev_gbm': 0.0, 'hv21': 0.4}
    (tmp_path / '2026-11-16_structured.json').write_text(json.dumps(
        {'signals': [{'ticker': 'AAA', 'strategy_id': 'S_x'}]}))
    assert oue._load_signal_ev('2026-11-16', 'AAA', 'S_x') is None


def test_load_signal_ev_keeps_legacy_ev(tmp_path, monkeypatch):
    monkeypatch.setattr(oue, 'HANDOFF_DIR', tmp_path)
    (tmp_path / '2026-11-10_structured.json').write_text(json.dumps(
        {'prefiltered': [{'ticker': 'AAA', 'strategy_id': 'S_x',
                          'ev_gbm': -0.01, 'hv21': 0.3}]}))
    assert oue._load_signal_ev('2026-11-10', 'AAA', 'S_x') == {'ev_gbm': -0.01, 'hv21': 0.3}
    assert oue._load_signal_ev('2026-11-09', 'AAA', 'S_x') is None   # no file
    assert oue._load_signal_ev('2026-11-10', 'BBB', 'S_x') is None   # no row
