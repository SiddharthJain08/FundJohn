# tests/test_bracket_stacking_sizer.py
import sys, os, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))
from execution import regime_blended_sizer as rbs


def _candidates():
    # Two uncorrelated blocks, both long, both tp 5%, different stops.
    return [
        {'sid': 'A', 'direction': 1, 'weight': 5.0, 'entry': 100.0,
         'stop': 98.0, 't1': 105.0, 't2': None},
        {'sid': 'B', 'direction': 1, 'weight': 9.0, 'entry': 100.0,
         'stop': 96.0, 't1': 105.0, 't2': None},
    ]


_GROUPS = {'block_map': {'A': 1, 'B': 2}}
_SHARPE = {'A': 2.0, 'B': 1.0}


def test_choose_bracket_gate_off_picks_max_weight(monkeypatch):
    monkeypatch.delenv('OPENCLAW_STRATEGY_BRACKET_STACK', raising=False)
    out = rbs._choose_bracket(_candidates(), 1, _GROUPS, _SHARPE)
    # Legacy: max-weight pick (B), single bracket, no stacking.
    assert out['sid'] == 'B'
    assert math.isclose(out['t1'], 105.0, rel_tol=1e-9)


def test_choose_bracket_gate_on_stacks(monkeypatch):
    monkeypatch.setenv('OPENCLAW_STRATEGY_BRACKET_STACK', '1')
    out = rbs._choose_bracket(_candidates(), 1, _GROUPS, _SHARPE)
    assert out['n_blocks'] == 2
    assert math.isclose(out['t1'], 105.0, rel_tol=1e-9)   # both takes 5% -> max 5%
    # min-stop: min(2%, 4%) = 2% -> 98.0 (distinguishes from the legacy
    # max-weight B-pick, whose stop would be 96).
    assert math.isclose(out['stop'], 98.0, rel_tol=1e-9)


def test_choose_bracket_gate_on_no_substrate_falls_back(monkeypatch):
    monkeypatch.setenv('OPENCLAW_STRATEGY_BRACKET_STACK', '1')
    out = rbs._choose_bracket(_candidates(), 1, None, _SHARPE)
    assert out['sid'] == 'B'                              # ortho_groups None -> legacy


def test_choose_bracket_gate_on_empty_stack_falls_back(monkeypatch):
    monkeypatch.setenv('OPENCLAW_STRATEGY_BRACKET_STACK', '1')
    # All wrong-direction -> stacked_bracket {} AND _select_bracket {} -> {}.
    cands = [{'sid': 'A', 'direction': -1, 'weight': 1.0, 'entry': 100.0,
              'stop': 98.0, 't1': 105.0, 't2': None}]
    assert rbs._choose_bracket(cands, 1, {'block_map': {}}, {}) == {}
