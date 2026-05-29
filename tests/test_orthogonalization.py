"""Tests for orthogonalization — Tier-1 fold + Tier-2 k_eff (pure)."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import orthogonalization as og  # noqa: E402


def _sig(sid, ticker, direction):
    return {'strategy_id': sid, 'ticker': ticker, 'direction': direction}


def test_fold_collapses_same_group_same_dir_to_representative():
    active = [_sig('S1', 'AAPL', 'LONG'), _sig('S2', 'AAPL', 'LONG'), _sig('S3', 'MSFT', 'LONG')]
    fold_map = {'S1': 1, 'S2': 1}          # S1,S2 same fold-group; S3 singleton (absent)
    rep_map = {1: 'S1'}                      # S1 is representative
    eff = {'S1': 2.0, 'S2': 1.0, 'S3': 1.5}
    out = og.fold_active_contributions(active, fold_map, rep_map, eff)
    sids = sorted(s['strategy_id'] for s in out)
    assert sids == ['S1', 'S3']              # S2 dropped (duplicate of S1 on AAPL/LONG)


def test_fold_fallback_to_highest_sharpe_when_representative_absent():
    active = [_sig('S2', 'AAPL', 'LONG'), _sig('S3', 'AAPL', 'LONG')]
    fold_map = {'S1': 1, 'S2': 1, 'S3': 1}   # all one group; rep S1 didn't fire
    rep_map = {1: 'S1'}
    eff = {'S1': 5.0, 'S2': 2.0, 'S3': 3.0}
    out = og.fold_active_contributions(active, fold_map, rep_map, eff)
    assert [s['strategy_id'] for s in out] == ['S3']   # highest-eff firing member


def test_fold_keeps_opposite_directions_in_same_group():
    active = [_sig('S1', 'AAPL', 'LONG'), _sig('S2', 'AAPL', 'SHORT')]
    fold_map = {'S1': 1, 'S2': 1}
    rep_map = {1: 'S1'}
    eff = {'S1': 2.0, 'S2': 1.0}
    out = og.fold_active_contributions(active, fold_map, rep_map, eff)
    assert len(out) == 2   # opposite directions are NOT duplicates — both kept


def test_fold_passes_through_ungrouped_strategies():
    active = [_sig('X', 'AAPL', 'LONG'), _sig('Y', 'AAPL', 'LONG')]
    out = og.fold_active_contributions(active, {}, {}, {'X': 1.0, 'Y': 1.0})
    assert len(out) == 2   # no fold-groups -> unchanged


def test_dir_to_int_known_directions():
    assert og._dir_to_int('LONG') == 1
    assert og._dir_to_int('BUY') == 1
    assert og._dir_to_int('BUY_VOL') == 1     # regression: was 0 under prefix matching
    assert og._dir_to_int('SHORT') == -1
    assert og._dir_to_int('SELL') == -1
    assert og._dir_to_int('SELL_VOL') == -1
    assert og._dir_to_int('FLAT') == 0
    assert og._dir_to_int(None) == 0
    assert og._dir_to_int('') == 0


def test_k_eff_endpoints():
    assert abs(og.k_eff(5, 0.0) - 5.0) < 1e-9     # uncorrelated -> full count
    assert abs(og.k_eff(5, 1.0) - 1.0) < 1e-9     # identical -> one bet
    assert abs(og.k_eff(2, 0.9) - (2 / 1.9)) < 1e-9
    assert og.k_eff(1, 0.5) == 1.0                 # single member guard


def test_block_conviction_floor_never_below_max_member():
    # strong 3.5 + correlated weak 1.0 at rho 0.5: must stay ABOVE 3.5 (the strong standalone)
    conv = og.block_conviction([3.5, 1.0], 0.5)
    assert conv > 3.5
    assert abs(conv - 3.8333333) < 1e-4            # 3.5 + (4.5-3.5)*(k_eff-1)/(k-1)


def test_block_conviction_endpoints():
    assert abs(og.block_conviction([1.0, 1.0, 1.0], 0.0) - 3.0) < 1e-9   # rho 0 -> sum
    assert abs(og.block_conviction([2.0, 1.0], 1.0) - 2.0) < 1e-9        # rho 1 -> max
    assert og.block_conviction([4.0], 0.9) == 4.0                         # singleton -> itself


def test_deflated_net_sharpe_gate_value():
    # AAPL: block B1 = {S1,S2} LONG (rho 0.5, sharpes 3.5 & 1.0); block B2 = {S3} LONG sharpe 2.0
    contribs = {'AAPL': [('S1', 1), ('S2', 1), ('S3', 1)]}
    block_map = {'S1': 10, 'S2': 10}              # S3 ungrouped -> its own pseudo-block
    sim = {'S1': {'S2': 0.5}, 'S2': {'S1': 0.5}}
    eff = {'S1': 3.5, 'S2': 1.0, 'S3': 2.0}
    out = og.deflated_net_sharpe(contribs, block_map, sim, eff)
    # B1 conviction ~3.833 + B2 (singleton) 2.0 = ~5.833 (vs naive 6.5)
    assert abs(out['AAPL'] - 5.8333333) < 1e-3


def test_deflated_net_sharpe_cross_block_full_credit_and_signs():
    # Two uncorrelated single-strategy blocks, opposite directions -> signed sum
    contribs = {'XYZ': [('A', 1), ('B', -1)]}
    out = og.deflated_net_sharpe(contribs, {}, {}, {'A': 4.0, 'B': 1.0})
    assert abs(out['XYZ'] - 3.0) < 1e-9            # +4 -1
