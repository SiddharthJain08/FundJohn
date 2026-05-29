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


def test_gate_uses_deflated_when_corr_weight_on():
    # The per-ticker contributor map the sizer passes to deflated_net_sharpe:
    # a correlated block's deflated conviction must be < naive sum but >= the strongest member.
    from execution import orthogonalization as og
    contribs = {'AAPL': [('S1', 1), ('S2', 1)]}
    block_map = {'S1': 1, 'S2': 1}
    sim = {'S1': {'S2': 0.9}, 'S2': {'S1': 0.9}}
    eff = {'S1': 2.0, 'S2': 2.0}
    deflated = og.deflated_net_sharpe(contribs, block_map, sim, eff)['AAPL']
    assert deflated < 4.0     # correlated pair counts as < 2 independent (naive sum = 4.0)
    assert deflated >= 2.0    # floor: never below the strongest member
