"""W6: manifest backtest_universe_cap → PrecomputedResolver bounding."""
from __future__ import annotations
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import pandas as pd

from backtest.unified_backtest import _bounded_resolver


def _manifest(tmp_path, cap):
    meta = {'backtest_universe_cap': cap} if cap else {}
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps(
        {'strategies': {'S_x': {'state': 'live', 'metadata': meta}}}))
    return p


def _artifact(tmp_path):
    d = tmp_path / 'data'
    d.mkdir()
    pd.DataFrame([
        {'run_id': 'shrink-t', 'tier': 'tier_liquid',
         'snapshot_date': '2024-01-31', 'symbols': ['AAA', 'BBB']},
        {'run_id': 'shrink-t', 'tier': 'sp500',
         'snapshot_date': '2024-01-31', 'symbols': ['AAA']},
    ]).to_parquet(d / 'universe_tier_membership_shrink-20240201.parquet',
                  index=False)
    return d


def test_no_cap_returns_none(tmp_path):
    assert _bounded_resolver(
        'S_x', manifest_path=_manifest(tmp_path, None),
        data_dir=_artifact(tmp_path)) is None


def test_cap_bounds_universe(tmp_path):
    r = _bounded_resolver('S_x',
                          manifest_path=_manifest(tmp_path, 'tier_liquid'),
                          data_dir=_artifact(tmp_path))
    assert r is not None
    assert sorted(r.resolve('S_x', date(2024, 2, 15))) == ['AAA', 'BBB']


def test_cap_without_artifact_falls_back_to_none(tmp_path):
    empty = tmp_path / 'nodata'
    empty.mkdir()
    assert _bounded_resolver(
        'S_x', manifest_path=_manifest(tmp_path, 'tier_liquid'),
        data_dir=empty) is None


def test_missing_strategy_returns_none(tmp_path):
    assert _bounded_resolver(
        'S_other', manifest_path=_manifest(tmp_path, 'tier_liquid'),
        data_dir=_artifact(tmp_path)) is None


# ── Sparse-CCA liquidity gate (2026-09-25-sparse-cca-zero-signals, task 2):
# universe_filter_ref fallback, gated behind OPENCLAW_BT_UNIVERSE_FILTER_REF
# (default OFF — must not silently re-epoch the ~95 live strategies that
# carry a universe_filter_ref but no explicit backtest_universe_cap). ─────

def _manifest_with_ref(tmp_path, ref, cap=None):
    meta = {}
    if ref is not None:
        meta['universe_filter_ref'] = ref
    if cap is not None:
        meta['backtest_universe_cap'] = cap
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps(
        {'strategies': {'S_x': {'state': 'live', 'metadata': meta}}}))
    return p


def test_ref_fallback_off_by_default_is_byte_identical(tmp_path, monkeypatch):
    """Flag unset (production default today): a manifest universe_filter_ref
    with NO backtest_universe_cap must resolve to None exactly like before
    this fallback existed, even though a matching artifact entry exists."""
    monkeypatch.delenv('OPENCLAW_BT_UNIVERSE_FILTER_REF', raising=False)
    ref = 'src.strategies.universe_default:tier_liquid'
    assert _bounded_resolver(
        'S_x', manifest_path=_manifest_with_ref(tmp_path, ref),
        data_dir=_artifact(tmp_path)) is None


def test_ref_fallback_explicitly_off_is_byte_identical(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENCLAW_BT_UNIVERSE_FILTER_REF', '0')
    ref = 'src.strategies.universe_default:tier_liquid'
    assert _bounded_resolver(
        'S_x', manifest_path=_manifest_with_ref(tmp_path, ref),
        data_dir=_artifact(tmp_path)) is None


def test_ref_fallback_bounds_universe_when_flag_on(tmp_path, monkeypatch):
    """The seam this task adds: manifest universe_filter_ref='...:tier_liquid'
    (the S_sparse_cca_mean_revert shape — no backtest_universe_cap) resolves
    the SAME tier name PrecomputedResolver already knows how to bound to,
    once the operator opts in."""
    monkeypatch.setenv('OPENCLAW_BT_UNIVERSE_FILTER_REF', '1')
    ref = 'src.strategies.universe_default:tier_liquid'
    r = _bounded_resolver(
        'S_x', manifest_path=_manifest_with_ref(tmp_path, ref),
        data_dir=_artifact(tmp_path))
    assert r is not None
    assert sorted(r.resolve('S_x', date(2024, 2, 15))) == ['AAA', 'BBB']


def test_explicit_backtest_universe_cap_wins_over_ref_fallback(tmp_path, monkeypatch):
    """When BOTH fields are present, the pre-existing backtest_universe_cap
    field wins outright — the ref fallback is only ever consulted when
    backtest_universe_cap is absent, flag or no flag."""
    monkeypatch.setenv('OPENCLAW_BT_UNIVERSE_FILTER_REF', '1')
    ref = 'src.strategies.universe_default:sp500'
    r = _bounded_resolver(
        'S_x', manifest_path=_manifest_with_ref(tmp_path, ref, cap='tier_liquid'),
        data_dir=_artifact(tmp_path))
    assert r is not None
    assert sorted(r.resolve('S_x', date(2024, 2, 15))) == ['AAA', 'BBB']  # tier_liquid, not sp500's ['AAA']


def test_cap_override_wins_over_ref_fallback_regardless_of_flag(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENCLAW_BT_UNIVERSE_FILTER_REF', '1')
    ref = 'src.strategies.universe_default:sp500'
    r = _bounded_resolver(
        'S_x', manifest_path=_manifest_with_ref(tmp_path, ref),
        data_dir=_artifact(tmp_path), cap_override='tier_liquid')
    assert r is not None
    assert sorted(r.resolve('S_x', date(2024, 2, 15))) == ['AAA', 'BBB']


def test_ref_fallback_unknown_tier_fails_open_to_none(tmp_path, monkeypatch):
    """A universe_filter_ref predicate name absent from the frozen artifact
    (e.g. no_otc, which is not in the ladder-tier artifact) must fail OPEN
    (unbounded static universe) rather than raise PrecomputedResolver's
    ValueError — unlike the pre-existing explicit-cap path, which still
    raises loudly on a genuinely misconfigured operator-set cap."""
    monkeypatch.setenv('OPENCLAW_BT_UNIVERSE_FILTER_REF', '1')
    ref = 'src.strategies.universe_default:no_otc'
    assert _bounded_resolver(
        'S_x', manifest_path=_manifest_with_ref(tmp_path, ref),
        data_dir=_artifact(tmp_path)) is None


def test_ref_fallback_no_ref_no_cap_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENCLAW_BT_UNIVERSE_FILTER_REF', '1')
    assert _bounded_resolver(
        'S_x', manifest_path=_manifest_with_ref(tmp_path, None),
        data_dir=_artifact(tmp_path)) is None
