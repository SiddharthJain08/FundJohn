"""Tangency-Sharpe S_adj (operator directive 2026-07-27).

S_adj = optimal non-negative-weights combination Sharpe per side, sides
subtract. Replaces the fixed-proportional combination Sharpe whose fixed
allocations let a weak correlated confirmer LOWER a ticker's conviction
(BE vs WW, 2026-07-27). Real LOW_VOL numbers used where it matters."""
import importlib
import math

import pytest

og = importlib.import_module("execution.orthogonalization")

# live LOW_VOL trade-factored weights + similarity (2026-07-27)
W = {'mom': 1.430, 'vme': 1.271, 'ff': 0.781}
SIM = {
    'mom': {'vme': 0.29086, 'ff': 0.33220},
    'vme': {'mom': 0.29086, 'ff': 0.58238},
    'ff':  {'mom': 0.33220, 'vme': 0.58238},
}


def _tan(contribs, sim=SIM, w=W, shrink=0.10):
    out, _ = og.tangency_net_sharpe({'T': contribs}, sim, w, shrink=shrink)
    return out.get('T', 0.0)


# ── monotonicity: the motivating anomaly ─────────────────────────────────────

def test_confirming_contributor_never_lowers_conviction():
    ww = _tan([('mom', 1), ('vme', 1)])                 # the WW pair
    be = _tan([('mom', 1), ('vme', 1), ('ff', 1)])      # + ff (the BE set)
    assert be >= ww - 1e-12
    assert ww > max(W.values())                          # diversification credit


def test_tangency_never_below_best_solo():
    assert _tan([('mom', 1)]) == pytest.approx(1.430)
    assert _tan([('mom', 1), ('ff', 1)]) >= 1.430


def test_subset_monotone_chain():
    s1 = _tan([('vme', 1)])
    s2 = _tan([('vme', 1), ('ff', 1)])
    s3 = _tan([('vme', 1), ('ff', 1), ('mom', 1)])
    assert s2 >= s1 - 1e-12 and s3 >= s2 - 1e-12


# ── disagreement semantics ───────────────────────────────────────────────────

def test_opposing_sides_subtract():
    net = _tan([('mom', 1), ('vme', -1)])
    assert net == pytest.approx(1.430 - 1.271, abs=1e-9)


def test_near_cancellation_gates_toward_zero():
    w = {'a': 1.40, 'b': 1.40}
    sim = {'a': {'b': 0.3}, 'b': {'a': 0.3}}
    out, _ = og.tangency_net_sharpe({'T': [('a', 1), ('b', -1)]}, sim, w)
    assert abs(out['T']) < 1e-9


def test_disagreement_never_rewarded():
    # unconstrained R^-1 would score this ABOVE either solo (spread arb);
    # sides-subtract must keep it BELOW the stronger solo
    w = {'a': 1.40, 'b': 1.30}
    sim = {'a': {'b': 0.3}, 'b': {'a': 0.3}}
    out, _ = og.tangency_net_sharpe({'T': [('a', 1), ('b', -1)]}, sim, w)
    assert 0 < out['T'] < 1.40


# ── redundancy: why the fold is retired ──────────────────────────────────────

def test_near_clone_adds_almost_nothing():
    w = {'a': 1.30, 'a2': 1.28}
    sim = {'a': {'a2': 0.95}, 'a2': {'a': 0.95}}
    out, _ = og.tangency_net_sharpe({'T': [('a', 1), ('a2', 1)]}, sim, w)
    solo = 1.30
    naive = math.sqrt(1.30 ** 2 + 1.28 ** 2)            # independence double-count
    assert solo - 1e-9 <= out['T'] < solo * 1.10        # ~no credit, no dilution
    assert out['T'] < naive * 0.80


def test_independent_confirmers_stack():
    w = {'a': 1.0, 'b': 1.0}
    sim = {'a': {'b': 0.0}, 'b': {'a': 0.0}}
    out, _ = og.tangency_net_sharpe({'T': [('a', 1), ('b', 1)]}, sim, w, shrink=0.0)
    assert out['T'] == pytest.approx(math.sqrt(2.0), rel=1e-6)


# ── robustness ───────────────────────────────────────────────────────────────

def test_non_psd_similarity_survives():
    # wildly inconsistent rho triple (non-PSD raw matrix): result stays finite
    # and >= best solo (singletons always feasible)
    w = {'a': 1.2, 'b': 1.1, 'c': 1.0}
    sim = {'a': {'b': 0.9, 'c': -0.9}, 'b': {'a': 0.9, 'c': 0.9},
           'c': {'a': -0.9, 'b': 0.9}}
    out, _ = og.tangency_net_sharpe({'T': [('a', 1), ('b', 1), ('c', 1)]}, sim, w)
    assert math.isfinite(out['T']) and out['T'] >= 1.2 - 1e-9


def test_missing_pair_uses_sparse_default():
    w = {'a': 1.0, 'zz': 0.9}
    out, _ = og.tangency_net_sharpe({'T': [('a', 1), ('zz', 1)]}, {}, w)
    assert out['T'] > 1.0                                # near-independent credit


def test_unknown_strategy_and_flat_direction_skipped():
    out, _ = og.tangency_net_sharpe(
        {'T': [('mom', 1), ('ghost', 1), ('vme', 0)]}, SIM, W)
    assert out['T'] == pytest.approx(1.430)


def test_empty_ticker_omitted():
    out, _ = og.tangency_net_sharpe({'T': [('ghost', 1)]}, SIM, W)
    assert 'T' not in out


# ── sizer wiring ─────────────────────────────────────────────────────────────

def test_sizer_maps_use_tangency_by_default(monkeypatch):
    import execution.regime_blended_sizer as rbs
    monkeypatch.delenv('OPENCLAW_TANGENCY_SADJ', raising=False)
    meta = {'T': {'strategies': ['mom', 'vme', 'ff'], 'directions': [1, 1, 1]}}
    gate, size, _, _ = rbs._corr_adjusted_maps(meta, W, W, SIM)
    expect, _ = og.tangency_net_sharpe(
        {'T': [('mom', 1), ('vme', 1), ('ff', 1)]}, SIM, W)
    assert gate['T'] == pytest.approx(expect['T'])
    assert size['T'] == pytest.approx(expect['T'])


def test_sizer_maps_legacy_killswitch(monkeypatch):
    import execution.regime_blended_sizer as rbs
    monkeypatch.setenv('OPENCLAW_TANGENCY_SADJ', '0')
    meta = {'T': {'strategies': ['mom', 'vme', 'ff'], 'directions': [1, 1, 1]}}
    gate, _, _, _ = rbs._corr_adjusted_maps(meta, W, W, SIM)
    expect, _ = og.corr_adjusted_net_sharpe(
        {'T': [('mom', 1), ('vme', 1), ('ff', 1)]}, SIM, W)
    assert gate['T'] == pytest.approx(expect['T'])
