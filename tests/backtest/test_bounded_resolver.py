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
