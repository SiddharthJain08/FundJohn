"""Sizer-integration tests for orthogonalization gates (default-OFF byte-identical)."""
from __future__ import annotations
import os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import orthogonalization as og  # noqa: E402


def test_fold_noop_when_no_groups():
    active = [{'strategy_id': 'A', 'ticker': 'AAPL', 'direction': 'LONG'},
              {'strategy_id': 'B', 'ticker': 'AAPL', 'direction': 'LONG'}]
    # empty fold_map (gates off => load_groups not called => no folding)
    assert og.fold_active_contributions(active, {}, {}, {}) == active


def test_gate_env_default_off(monkeypatch):
    monkeypatch.delenv('OPENCLAW_STRATEGY_FOLD', raising=False)
    monkeypatch.delenv('OPENCLAW_STRATEGY_CORR_WEIGHT', raising=False)
    from execution import regime_blended_sizer as rbs
    assert rbs._ortho_enabled('OPENCLAW_STRATEGY_FOLD') is False
    assert rbs._ortho_enabled('OPENCLAW_STRATEGY_CORR_WEIGHT') is False
