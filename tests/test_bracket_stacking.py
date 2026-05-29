# tests/test_bracket_stacking.py
import math
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from execution import bracket_stacking as bs


def _b(sid, direction, weight, entry, stop, t1, t2=None):
    return {'sid': sid, 'direction': direction, 'weight': weight,
            'entry': entry, 'stop': stop, 't1': t1, 't2': t2}


def test_single_block_returns_top_sharpe_rep_not_max_weight():
    # Two strategies, SAME block, opposite (weight, sharpe) ranking.
    # A: high weight, low sharpe, tight stop / 5% target
    # B: low weight, high sharpe, wide stop / 8% target
    cands = [
        _b('A', 1, 9.0, 100.0, 98.0, 105.0),    # stop 2%, tp 5%
        _b('B', 1, 1.0, 100.0, 94.0, 108.0),    # stop 6%, tp 8%
    ]
    out = bs.stacked_bracket(cands, 1, block_map={'A': 7, 'B': 7},
                             eff_sharpe={'A': 0.5, 'B': 3.0})
    # One block -> rep is B (max sharpe). tp_total = 8%, stop = 6%.
    assert out['n_blocks'] == 1
    assert math.isclose(out['t1'], 108.0, rel_tol=1e-9)
    assert math.isclose(out['stop'], 94.0, rel_tol=1e-9)


def test_two_uncorrelated_blocks_stack_tp_keep_tightest_stop():
    cands = [
        _b('A', 1, 5.0, 100.0, 98.0, 105.0),    # block 1: stop 2%, tp 5%
        _b('B', 1, 5.0, 100.0, 96.0, 105.0),    # block 2: stop 4%, tp 5%
    ]
    out = bs.stacked_bracket(cands, 1, block_map={'A': 1, 'B': 2},
                             eff_sharpe={'A': 2.0, 'B': 1.0})
    assert out['n_blocks'] == 2
    # tp stacks: 5% + 5% = 10% (under 3x cap). stop = min(2%,4%) = 2%.
    assert math.isclose(out['t1'], 110.0, rel_tol=1e-9)
    assert math.isclose(out['stop'], 98.0, rel_tol=1e-9)


def test_tp_cap_is_three_times_largest_single_block():
    # Four blocks each tp 5% -> sum 20%, cap = 3 * 5% = 15%.
    cands = [_b(s, 1, 1.0, 100.0, 99.0, 105.0) for s in ('A', 'B', 'C', 'D')]
    out = bs.stacked_bracket(cands, 1,
                             block_map={'A': 1, 'B': 2, 'C': 3, 'D': 4},
                             eff_sharpe={s: 1.0 for s in ('A', 'B', 'C', 'D')})
    assert out['n_blocks'] == 4
    assert math.isclose(out['t1'], 115.0, rel_tol=1e-9)   # capped at +15%
    assert math.isclose(out['stop'], 99.0, rel_tol=1e-9)  # tightest 1%


def test_short_side_mirrors_long():
    cands = [
        _b('A', -1, 5.0, 100.0, 102.0, 95.0),   # block1: stop 2%, tp 5%
        _b('B', -1, 5.0, 100.0, 103.0, 95.0),   # block2: stop 3%, tp 5%
    ]
    out = bs.stacked_bracket(cands, -1, block_map={'A': 1, 'B': 2},
                             eff_sharpe={'A': 2.0, 'B': 1.0})
    # tp stacks to 10% -> t1 = 90; stop = min(2%,3%) -> 102.
    assert math.isclose(out['t1'], 90.0, rel_tol=1e-9)
    assert math.isclose(out['stop'], 102.0, rel_tol=1e-9)


def test_ungrouped_strategies_are_singleton_blocks():
    # No block_map entries -> each is its own block -> tp stacks.
    cands = [
        _b('A', 1, 1.0, 100.0, 98.0, 105.0),
        _b('B', 1, 1.0, 100.0, 97.0, 105.0),
    ]
    out = bs.stacked_bracket(cands, 1, block_map={}, eff_sharpe={'A': 1.0, 'B': 1.0})
    assert out['n_blocks'] == 2
    assert math.isclose(out['t1'], 110.0, rel_tol=1e-9)
    assert math.isclose(out['stop'], 98.0, rel_tol=1e-9)  # tightest of 98(2%)/97(3%)


def test_wrong_direction_and_nonfinite_rejected():
    cands = [
        _b('A', -1, 9.0, 100.0, 98.0, 105.0),               # wrong direction
        _b('B', 1, 1.0, 100.0, float('nan'), 105.0),        # nonfinite stop
        _b('C', 1, 1.0, 100.0, 102.0, 105.0),               # inverted (stop>entry long)
        _b('D', 1, 1.0, 100.0, 98.0, 106.0),                # the only usable long
    ]
    out = bs.stacked_bracket(cands, 1, block_map={}, eff_sharpe={})
    assert out['n_blocks'] == 1
    assert out['why'].startswith('stacked')
    assert math.isclose(out['t1'], 106.0, rel_tol=1e-9)


def test_no_usable_bracket_returns_empty():
    cands = [_b('A', -1, 1.0, 100.0, 98.0, 105.0)]          # only wrong-direction
    assert bs.stacked_bracket(cands, 1, block_map={}, eff_sharpe={}) == {}
    assert bs.stacked_bracket([], 1, block_map={}, eff_sharpe={}) == {}


def test_rep_tiebreak_is_deterministic_smallest_sid():
    # Equal sharpe in one block -> smallest sid wins (matches representatives()).
    cands = [
        _b('Zzz', 1, 1.0, 100.0, 98.0, 105.0),
        _b('Aaa', 1, 1.0, 100.0, 95.0, 109.0),
    ]
    out = bs.stacked_bracket(cands, 1, block_map={'Zzz': 1, 'Aaa': 1},
                             eff_sharpe={'Zzz': 2.0, 'Aaa': 2.0})
    # Tie on sharpe -> 'Aaa' chosen -> stop 5%, tp 9%.
    assert math.isclose(out['stop'], 95.0, rel_tol=1e-9)
    assert math.isclose(out['t1'], 109.0, rel_tol=1e-9)


def test_t2_never_inverts_past_stacked_t1():
    # Long: anchor t2 (108) is below the stacked t1 (110) -> clamp up to t1.
    cands = [
        _b('A', 1, 5.0, 100.0, 98.0, 105.0, t2=108.0),   # block1, sharpe high (anchor)
        _b('B', 1, 5.0, 100.0, 96.0, 105.0, t2=109.0),   # block2
    ]
    out = bs.stacked_bracket(cands, 1, block_map={'A': 1, 'B': 2},
                             eff_sharpe={'A': 2.0, 'B': 1.0})
    assert math.isclose(out['t1'], 110.0, rel_tol=1e-9)
    assert out['t2'] >= out['t1']                         # no inversion

    # Single block long with a finite t2 above t1 is preserved unchanged.
    out1 = bs.stacked_bracket([_b('A', 1, 1.0, 100.0, 98.0, 105.0, t2=112.0)], 1,
                              block_map={'A': 1}, eff_sharpe={'A': 1.0})
    assert math.isclose(out1['t1'], 105.0, rel_tol=1e-9)
    assert math.isclose(out1['t2'], 112.0, rel_tol=1e-9)

    # None t2 stays None.
    out2 = bs.stacked_bracket([_b('A', 1, 1.0, 100.0, 98.0, 105.0, t2=None)], 1,
                              block_map={'A': 1}, eff_sharpe={'A': 1.0})
    assert out2['t2'] is None
