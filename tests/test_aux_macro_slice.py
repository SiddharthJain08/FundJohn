"""aux_data_loader macro series — backtest-blind fix regression (2026-07-03).

Strategies reading aux_data['macro'][name] as a SERIES (S_tr_01 etc.) were
backtest-blind: the loader served only scalar vol_indices day-values. The
macro slice must mirror engine.py's live format and be point-in-time.
"""
import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import aux_data_loader as adl  # noqa: E402


@pytest.fixture()
def macro_fixture(monkeypatch, tmp_path):
    df = pd.DataFrame({
        'date':   ['2026-06-27', '2026-06-30', '2026-07-01'] * 2,
        'series': ['VIX'] * 3 + ['VVIX'] * 3,
        'value':  [16.5, 16.4, 16.6, 88.0, 87.0, 89.0],
        'source': ['cboe'] * 6,
    })
    p = tmp_path / 'macro.parquet'
    df.to_parquet(p, index=False)
    monkeypatch.setattr(adl, 'MACRO_PATH', p)
    monkeypatch.setattr(adl, '_MACRO_SERIES', None)  # bust module cache
    yield
    monkeypatch.setattr(adl, '_MACRO_SERIES', None)


def test_macro_slice_is_point_in_time(macro_fixture):
    m = adl._macro_slice('2026-06-30')
    assert set(m) == {'VIX', 'VVIX'}
    assert isinstance(m['VVIX'], pd.Series)
    assert m['VVIX'].index.max() == pd.Timestamp('2026-06-30'), 'no look-ahead'
    assert len(m['VVIX']) == 2
    assert float(m['VVIX'].iloc[-1]) == 87.0


def test_macro_slice_empty_before_history(macro_fixture):
    assert adl._macro_slice('2020-01-01') == {}


def test_macro_missing_file_fails_open(monkeypatch, tmp_path):
    monkeypatch.setattr(adl, 'MACRO_PATH', tmp_path / 'nope.parquet')
    monkeypatch.setattr(adl, '_MACRO_SERIES', None)
    assert adl._macro_slice('2026-06-30') == {}
    monkeypatch.setattr(adl, '_MACRO_SERIES', None)


def test_load_aux_data_carries_macro(macro_fixture, monkeypatch):
    # keep the other slices cheap/empty
    monkeypatch.setattr(adl, '_day_slice', lambda d: {})
    monkeypatch.setattr(adl, '_vol_indices_slice', lambda d: {})
    monkeypatch.setattr(adl, '_insider_slice', lambda d: {})
    monkeypatch.setattr(adl, '_insider_long_slice', lambda d: {})
    monkeypatch.setattr(adl, '_sentiment_day_slice', lambda d: {})
    aux = adl.load_aux_data('2026-07-01')
    assert 'macro' in aux
    assert set(aux['macro']) == {'VIX', 'VVIX'}
    assert float(aux['macro']['VIX'].loc['2026-07-01']) == 16.6
