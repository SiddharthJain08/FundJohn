from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import seed_strategy_regime_params as seed  # noqa: E402


def test_compute_rows_eligible_field_present():
    manifest = {'strategies': {
        's1': {'eligible_regimes': ['LOW_VOL', 'TRANSITIONING']},
    }}
    rows = seed.compute_rows(manifest)
    by_regime = {r['regime_state']: r for r in rows if r['strategy_id'] == 's1'}
    assert by_regime['LOW_VOL']['eligible'] is True
    assert by_regime['TRANSITIONING']['eligible'] is True
    assert by_regime['HIGH_VOL']['eligible'] is False
    assert by_regime['CRISIS']['eligible'] is False


def test_compute_rows_no_field_means_all_eligible():
    manifest = {'strategies': {'legacy': {}}}
    rows = seed.compute_rows(manifest)
    for r in rows:
        assert r['eligible'] is True


def test_compute_rows_empty_list_means_all_eligible():
    """Backward-compat: empty list under Phase 1 gate semantics returned True.
    Migration preserves that interpretation."""
    manifest = {'strategies': {'edge': {'eligible_regimes': []}}}
    rows = seed.compute_rows(manifest)
    for r in rows:
        assert r['eligible'] is True


def test_compute_rows_produces_four_per_strategy():
    manifest = {'strategies': {
        's1': {'eligible_regimes': ['LOW_VOL']},
        's2': {'eligible_regimes': None},
    }}
    rows = seed.compute_rows(manifest)
    assert len(rows) == 8
    assert {r['regime_state'] for r in rows} == {
        'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}


def test_compute_rows_set_by_tagged_as_migration():
    manifest = {'strategies': {'s1': {'eligible_regimes': ['LOW_VOL']}}}
    rows = seed.compute_rows(manifest)
    for r in rows:
        assert r['set_by'].startswith('migration:')
