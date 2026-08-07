"""Backtest-sourced strategy similarity (spec 2026-08-05 §3) — unit tests.

Pins the three load-bearing behaviours of the new source:
  1. overlap_similarity_restricted computes Jaccard on the INTERSECTION of the
     two strategies' traded universes (§3.4(1) — different simulated symbol sets
     must not fake orthogonality), and reports intersection sizes.
  2. similarity_for_regime_backtest replaces ONE leg, not both (§3.1): the
     PnL-correlation leg comes from backtest returns, while the co-firing
     overlap leg stays LIVE for pairs observed live on both sides and falls
     back to backtest overlap otherwise.
  3. resolve_source: explicit arg > OPENCLAW_SIMILARITY_SOURCE > 'live';
     invalid value raises instead of silently building the wrong matrix.
  4. The live returns reader filters strategy_daily_returns on source='live'
     so a future backtest-row writer cannot silently blend into the live leg.

All pure-function tests — no DB, no network.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import execution.strategy_similarity as ss  # noqa: E402


# ---------------------------------------------------------------------------
# (1) overlap_similarity_restricted
# ---------------------------------------------------------------------------

def test_restricted_identical_sets_full_similarity():
    sets = {'A': {('26W01', 'X', 1), ('26W02', 'Y', -1)},
            'B': {('26W01', 'X', 1), ('26W02', 'Y', -1)}}
    unis = {'A': {'X', 'Y'}, 'B': {'X', 'Y'}}
    m, inter = ss.overlap_similarity_restricted(sets, unis)
    assert m['A']['B'] == 1.0 and m['B']['A'] == 1.0
    assert m['A']['A'] == 1.0
    assert inter[('A', 'B')] == 2


def test_restricted_disjoint_universes_zero_with_zero_intersection():
    sets = {'A': {('26W01', 'X', 1)}, 'B': {('26W01', 'Z', 1)}}
    unis = {'A': {'X'}, 'B': {'Z'}}
    m, inter = ss.overlap_similarity_restricted(sets, unis)
    assert m['A']['B'] == 0.0
    assert inter[('A', 'B')] == 0, \
        'zero-intersection must be recorded so "never comparable" is visible'


def test_restricted_excludes_out_of_intersection_tuples():
    # A trades X and Y; B's universe is only X. A's Y-emissions must NOT dilute
    # the Jaccard — on the common universe {X} the two agree perfectly.
    sets = {'A': {('26W01', 'X', 1), ('26W01', 'Y', 1), ('26W02', 'Y', -1)},
            'B': {('26W01', 'X', 1)}}
    unis = {'A': {'X', 'Y'}, 'B': {'X'}}
    m, inter = ss.overlap_similarity_restricted(sets, unis)
    assert m['A']['B'] == 1.0, \
        'restriction must drop A\'s out-of-universe tuples before Jaccard'
    assert inter[('A', 'B')] == 1

    # Unrestricted Jaccard over the raw sets would be 1/3 — pin the difference
    # so a refactor cannot silently fall back to the unrestricted form.
    raw = ss.overlap_similarity({'A': sets['A'], 'B': sets['B']})
    assert raw['A']['B'] == pytest.approx(1 / 3)


# ---------------------------------------------------------------------------
# (2) similarity_for_regime_backtest — one leg replaced, not both
# ---------------------------------------------------------------------------

def _bt_returns_corr(n=60, rho_pairs=('A', 'B', 'C')):
    """Perfectly co-moving 60-day return dicts → pairwise Pearson 1.0 (clipped
    to MAX_OFF_DIAGONAL=0.95), n_obs=60 → adaptive alpha at its 0.6 ceiling."""
    dates = [f'2025-01-{i+1:02d}' for i in range(n)]
    base = [0.01 * ((i % 7) - 3) for i in range(n)]
    return {s: {d: r for d, r in zip(dates, base)} for s in rho_pairs}


def test_backtest_source_prefers_live_overlap_when_both_observed():
    # Live: A and B co-fire identically → live overlap 1.0.
    live = {'A': {('26W01', 'X', 1)}, 'B': {('26W01', 'X', 1)}}
    # Backtest: A and B disjoint → backtest overlap would be 0.0.
    bt_sets = {'A': {('20W01', 'P', 1)}, 'B': {('20W01', 'Q', 1)}, 'C': {('20W01', 'P', 1)}}
    bt_unis = {'A': {'P'}, 'B': {'Q'}, 'C': {'P'}}
    sim = ss.similarity_for_regime_backtest(
        'LOW_VOL', live_cofiring=live, bt_cofiring=bt_sets,
        bt_universes=bt_unis, bt_returns=_bt_returns_corr())
    # alpha = 0.6 (60 obs), retcorr = 0.95 (clipped). Overlap leg must be the
    # LIVE 1.0, not the backtest 0.0: 0.4*1.0 + 0.6*0.95 = 0.97.
    assert sim['A']['B'] == pytest.approx(0.4 * 1.0 + 0.6 * 0.95)


def test_backtest_source_falls_back_to_backtest_overlap_without_live():
    # C has no live emissions → (A, C) must use the backtest restricted overlap
    # (identical on common universe {P} → 1.0), not live (absent) and not 0.05.
    live = {'A': {('26W01', 'X', 1)}, 'B': {('26W01', 'X', 1)}}
    bt_sets = {'A': {('20W01', 'P', 1)}, 'B': {('20W01', 'Q', 1)}, 'C': {('20W01', 'P', 1)}}
    bt_unis = {'A': {'P'}, 'B': {'Q'}, 'C': {'P'}}
    sim = ss.similarity_for_regime_backtest(
        'LOW_VOL', live_cofiring=live, bt_cofiring=bt_sets,
        bt_universes=bt_unis, bt_returns=_bt_returns_corr())
    assert sim['A']['C'] == pytest.approx(0.4 * 1.0 + 0.6 * 0.95)
    # And (B, C): backtest overlap on disjoint universes = 0.0 → pure ret-corr.
    assert sim['B']['C'] == pytest.approx(0.4 * 0.0 + 0.6 * 0.95)


def test_backtest_source_diagonal_and_key_union():
    live = {'A': {('26W01', 'X', 1)}}
    bt_sets = {'B': {('20W01', 'P', 1)}}
    bt_unis = {'B': {'P'}}
    rets = _bt_returns_corr(rho_pairs=('C',))
    sim = ss.similarity_for_regime_backtest(
        'LOW_VOL', live_cofiring=live, bt_cofiring=bt_sets,
        bt_universes=bt_unis, bt_returns=rets)
    assert set(sim) == {'A', 'B', 'C'}, 'matrix keys = union of all three sources'
    for s in ('A', 'B', 'C'):
        assert sim[s][s] == 1.0


# ---------------------------------------------------------------------------
# (3) resolve_source
# ---------------------------------------------------------------------------

def test_resolve_source_default_and_env(monkeypatch):
    monkeypatch.delenv('OPENCLAW_SIMILARITY_SOURCE', raising=False)
    assert ss.resolve_source() == 'live'
    monkeypatch.setenv('OPENCLAW_SIMILARITY_SOURCE', 'backtest')
    assert ss.resolve_source() == 'backtest'
    assert ss.resolve_source('live') == 'live', 'explicit arg beats env'
    monkeypatch.setenv('OPENCLAW_SIMILARITY_SOURCE', 'bogus')
    with pytest.raises(ValueError):
        ss.resolve_source()


# ---------------------------------------------------------------------------
# (4) the live returns reader must filter on source='live'
# ---------------------------------------------------------------------------

def test_live_returns_reader_filters_source_live():
    import inspect
    src = inspect.getsource(ss._returns_by_regime)
    assert "source = 'live'" in src, (
        "strategy_daily_returns carries a source column; the live leg must pin "
        "source='live' so future backtest-derived rows cannot silently blend in")
