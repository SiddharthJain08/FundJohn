"""SP-1: engine.py drops zero-greek rows at options_eod.parquet read boundary."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import pandas as pd
import pytest

from execution.engine import _drop_zero_greeks


def test_drops_zero_greek_rows():
    df = pd.DataFrame([
        {'contract_symbol': 'OK',   'delta': 0.5,  'gamma': 0.01, 'theta': -0.2, 'vega': 0.8},
        {'contract_symbol': 'ZERO', 'delta': 0,    'gamma': 0,    'theta':  0,   'vega': 0},
        {'contract_symbol': 'OK2',  'delta': -0.4, 'gamma': 0.02, 'theta': -0.1, 'vega': 0.5},
    ])
    out = _drop_zero_greeks(df)
    assert len(out) == 2
    assert set(out['contract_symbol']) == {'OK', 'OK2'}


def test_handles_empty_df():
    out = _drop_zero_greeks(pd.DataFrame())
    assert out.empty


def test_handles_missing_columns():
    """No greek columns → no rows filtered (nothing to evaluate)."""
    df = pd.DataFrame([{'contract_symbol': 'X', 'volume': 100}])
    out = _drop_zero_greeks(df)
    # No greek columns present → filter skips entirely → all rows kept.
    assert len(out) == 1


def test_partial_greek_columns_all_zero():
    """Only some greek columns present; if ALL present ones are zero → drop."""
    df = pd.DataFrame([
        {'contract_symbol': 'PARTIAL_ZERO', 'delta': 0, 'gamma': 0},
        {'contract_symbol': 'PARTIAL_OK',   'delta': 0.3, 'gamma': 0},
    ])
    out = _drop_zero_greeks(df)
    # PARTIAL_ZERO: delta=0 AND gamma=0 → drop
    # PARTIAL_OK:   delta=0.3 AND gamma=0 → NOT all zero → keep
    assert len(out) == 1
    assert out.iloc[0]['contract_symbol'] == 'PARTIAL_OK'


def test_nan_treated_as_zero():
    """NaN greeks are treated as zero — a NaN-only row is degenerate and dropped."""
    import numpy as np
    df = pd.DataFrame([
        {'contract_symbol': 'NAN_ROW', 'delta': np.nan, 'gamma': np.nan,
         'theta': np.nan, 'vega': np.nan},
        {'contract_symbol': 'GOOD',    'delta': 0.5,    'gamma': 0.01,
         'theta': -0.1,   'vega': 0.4},
    ])
    out = _drop_zero_greeks(df)
    assert len(out) == 1
    assert out.iloc[0]['contract_symbol'] == 'GOOD'


def test_preserves_non_greek_columns():
    """Non-greek columns are preserved in output rows."""
    df = pd.DataFrame([
        {'contract_symbol': 'KEEP', 'delta': 0.3, 'gamma': 0.01,
         'theta': -0.05, 'vega': 0.2, 'open_interest': 500, 'volume': 100},
        {'contract_symbol': 'DROP', 'delta': 0,   'gamma': 0,
         'theta':  0,    'vega': 0,   'open_interest': 0,   'volume': 0},
    ])
    out = _drop_zero_greeks(df)
    assert len(out) == 1
    assert out.iloc[0]['open_interest'] == 500
