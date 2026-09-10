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
    assert math.isclose(out['t1'], 105.0, rel_tol=1e-9)   # both takes 5% -> weighted mean 5%
    # sharpe-wtd mean stop: ω=(2/3,1/3) -> 2%·⅔ + 4%·⅓ = 8/3 % -> 97.333…
    # (distinguishes from B-pick 96 AND from the old min-stop 98)
    assert math.isclose(out['stop'], 100.0 * (1.0 - 0.08 / 3.0), rel_tol=1e-9)


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


# ── Benchmark-sleeve bracket (2026-09-10) ────────────────────────────────────
# The beta sleeve (S_beta_spy) declares a deliberately unreachable bracket
# (stop −40 % / target +400 %) so buy-and-hold beta is never stopped out by a
# bracket. Before this change the sizer handed SPY whichever alpha strategy's
# 2×ATR bracket won the weight pick (~1 % on SPY), which ejected the sleeve
# pre-market on 2026-09-10. When a ticker carries a benchmark-sleeve
# contributor in the winning direction, the sleeve's own bracket wins.

def _sleeve_candidates():
    return _candidates() + [
        {'sid': 'S_beta_spy', 'direction': 1, 'weight': 0.8, 'entry': 100.0,
         'stop': 60.0, 't1': 500.0, 't2': 600.0},
    ]


def test_sleeve_bracket_wins_over_heavier_alpha_gate_off(monkeypatch):
    monkeypatch.delenv('OPENCLAW_STRATEGY_BRACKET_STACK', raising=False)
    monkeypatch.delenv('OPENCLAW_BENCH_SLEEVE_BRACKET', raising=False)
    out = rbs._choose_bracket(_sleeve_candidates(), 1, _GROUPS, _SHARPE,
                              bench_ids={'S_beta_spy'})
    assert out['sid'] == 'S_beta_spy'
    assert math.isclose(out['stop'], 60.0) and math.isclose(out['t1'], 500.0)


def test_sleeve_bracket_wins_over_stacking_gate_on(monkeypatch):
    monkeypatch.setenv('OPENCLAW_STRATEGY_BRACKET_STACK', '1')
    monkeypatch.delenv('OPENCLAW_BENCH_SLEEVE_BRACKET', raising=False)
    out = rbs._choose_bracket(_sleeve_candidates(), 1, _GROUPS, _SHARPE,
                              bench_ids={'S_beta_spy'})
    assert out['sid'] == 'S_beta_spy'
    assert math.isclose(out['stop'], 60.0)


def test_sleeve_bracket_requires_winning_direction(monkeypatch):
    # Sleeve contributor is SHORT while the ticker nets LONG -> legacy pick.
    monkeypatch.delenv('OPENCLAW_STRATEGY_BRACKET_STACK', raising=False)
    cands = _candidates() + [
        {'sid': 'S_beta_spy', 'direction': -1, 'weight': 0.8, 'entry': 100.0,
         'stop': 140.0, 't1': 20.0, 't2': None},
    ]
    out = rbs._choose_bracket(cands, 1, _GROUPS, _SHARPE, bench_ids={'S_beta_spy'})
    assert out['sid'] == 'B'


def test_sleeve_bracket_nonfinite_falls_back_to_legacy(monkeypatch):
    monkeypatch.delenv('OPENCLAW_STRATEGY_BRACKET_STACK', raising=False)
    cands = _candidates() + [
        {'sid': 'S_beta_spy', 'direction': 1, 'weight': 0.8, 'entry': 100.0,
         'stop': float('nan'), 't1': 500.0, 't2': None},
    ]
    out = rbs._choose_bracket(cands, 1, _GROUPS, _SHARPE, bench_ids={'S_beta_spy'})
    assert out['sid'] == 'B'


def test_sleeve_bracket_kill_switch_restores_legacy(monkeypatch):
    monkeypatch.delenv('OPENCLAW_STRATEGY_BRACKET_STACK', raising=False)
    monkeypatch.setenv('OPENCLAW_BENCH_SLEEVE_BRACKET', '0')
    out = rbs._choose_bracket(_sleeve_candidates(), 1, _GROUPS, _SHARPE,
                              bench_ids={'S_beta_spy'})
    assert out['sid'] == 'B'


def test_no_bench_ids_is_byte_identical_legacy(monkeypatch):
    monkeypatch.delenv('OPENCLAW_STRATEGY_BRACKET_STACK', raising=False)
    legacy = rbs._choose_bracket(_sleeve_candidates(), 1, _GROUPS, _SHARPE)
    # Without bench_ids the sleeve is just another (light) contributor: B wins.
    assert legacy['sid'] == 'B'
    assert rbs._choose_bracket(_sleeve_candidates(), 1, _GROUPS, _SHARPE,
                               bench_ids=None) == legacy
    assert rbs._choose_bracket(_sleeve_candidates(), 1, _GROUPS, _SHARPE,
                               bench_ids=set()) == legacy


def test_emit_orders_hands_bench_ticker_the_sleeve_bracket(monkeypatch):
    """End-to-end through _emit_orders_from_targets: a benchmark ticker's
    emitted order carries the sleeve's stop/t1, a non-benchmark ticker keeps
    the legacy pick."""
    monkeypatch.delenv('OPENCLAW_STRATEGY_BRACKET_STACK', raising=False)
    monkeypatch.delenv('OPENCLAW_BENCH_SLEEVE_BRACKET', raising=False)
    ticker_meta = {
        'SPY': {'strategies': ['A', 'S_beta_spy'], 'directions': [1, 1],
                'brackets': [
                    {'sid': 'A', 'direction': 1, 'weight': 5.0, 'entry': 760.0,
                     'stop': 752.4, 't1': 798.0, 't2': None},
                    {'sid': 'S_beta_spy', 'direction': 1, 'weight': 0.8, 'entry': 760.0,
                     'stop': 456.0, 't1': 3800.0, 't2': 4560.0},
                ]},
        'XYZ': {'strategies': ['A'], 'directions': [1],
                'brackets': [
                    {'sid': 'A', 'direction': 1, 'weight': 5.0, 'entry': 50.0,
                     'stop': 48.0, 't1': 52.5, 't2': None},
                ]},
    }
    target_usd = {'SPY': 60_000.0, 'XYZ': 5_000.0}
    orders = rbs._emit_orders_from_targets(
        target_usd, ticker_meta, nav=100_000.0, confirmer=None, _ortho_groups=None,
        sharpe_by_strat={'A': 2.0, 'S_beta_spy': 0.8},
        eff_weight_by_strat={'A': 5.0, 'S_beta_spy': 0.8}, opt_active=[],
        weight_by_strat={'A': 5.0, 'S_beta_spy': 0.8}, scale=1.0,
        account_state={'nav': 100_000.0}, broker={},
        bench_ids={'S_beta_spy'}, bench_tkrs={'SPY'})
    by_tkr = {o['ticker']: o for o in orders}
    assert math.isclose(by_tkr['SPY']['stop'], 456.0)
    assert math.isclose(by_tkr['SPY']['t1'], 3800.0)
    assert math.isclose(by_tkr['XYZ']['stop'], 48.0)
