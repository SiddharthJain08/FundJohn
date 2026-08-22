"""Per-regime acting-strategy gate — resolver + counting helper (2026-08-22).

The conviction gate is the MINIMUM NUMBER OF DISTINCT STRATEGIES acting on a
ticker in its NET direction (regime_sizer_params.min_acting_strategies,
migration 147, bound [1, 10]). 1 = every ticker with a contributor passes
(the pre-2026-08-22 behaviour with the S_adj floor at 0.0).
"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import pytest  # noqa: E402
from execution import regime_blended_sizer as rbs  # noqa: E402


# ── resolver ────────────────────────────────────────────────────────────────

def test_uses_per_regime_value_in_bounds():
    assert rbs._resolve_min_acting_strategies({'min_acting_strategies': 3}) == 3
    assert rbs._resolve_min_acting_strategies({'min_acting_strategies': '4'}) == 4
    assert rbs._resolve_min_acting_strategies({'min_acting_strategies': 2.9}) == 2   # int(), not round()


def test_clamps_to_bounds():
    assert rbs._resolve_min_acting_strategies({'min_acting_strategies': 0}) == 1
    assert rbs._resolve_min_acting_strategies({'min_acting_strategies': -5}) == 1
    assert rbs._resolve_min_acting_strategies({'min_acting_strategies': 99}) == 10


def test_missing_param_falls_back_to_default_when_no_db(monkeypatch):
    monkeypatch.delenv('POSTGRES_URI', raising=False)
    assert rbs._resolve_min_acting_strategies({}) == 1
    assert rbs._resolve_min_acting_strategies(None) == 1
    assert rbs._resolve_min_acting_strategies({'min_acting_strategies': 'junk'}) == 1


def test_retired_sadj_floor_resolver_is_gone():
    # The S_adj floor gate was REPLACED (operator directive 2026-08-22): nothing
    # may read regime_sizer_params.min_corr_cum_sharpe in the sizer any more.
    assert not hasattr(rbs, '_resolve_min_corr_cum_sharpe')


# ── counting helper ─────────────────────────────────────────────────────────

def _meta(pairs):
    return {'strategies': [s for s, _ in pairs], 'directions': [d for _, d in pairs]}


def test_counts_distinct_strategies_in_net_direction_only():
    meta = {'AAA': _meta([('S1', 1), ('S2', 1), ('S3', -1)])}
    assert rbs._acting_counts(meta, {'AAA': 1}) == {'AAA': 2}
    assert rbs._acting_counts(meta, {'AAA': -1}) == {'AAA': 1}


def test_duplicate_contributions_from_one_strategy_count_once():
    # Cadence-window aggregation can carry several rows of the same strategy.
    meta = {'AAA': _meta([('S1', 1), ('S1', 1), ('S1', 1)])}
    assert rbs._acting_counts(meta, {'AAA': 1}) == {'AAA': 1}


def test_zero_net_sign_counts_zero_and_synthetic_markers_ignored():
    meta = {'AAA': _meta([('S1', 1), ('S2', -1)]),
            'BBB': _meta([('__close_orphan__', 0)])}
    assert rbs._acting_counts(meta, {'AAA': 0, 'BBB': 0}) == {'AAA': 0, 'BBB': 0}


def test_net_signs_prefer_sadj_then_fall_back_to_naive_weight():
    signs = rbs._net_signs({'AAA': 2.5, 'BBB': 0.0, 'CCC': -0.1},
                           {'AAA': -9.0, 'BBB': -3.0, 'DDD': 1.0})
    assert signs == {'AAA': 1, 'BBB': -1, 'CCC': -1, 'DDD': 1}
