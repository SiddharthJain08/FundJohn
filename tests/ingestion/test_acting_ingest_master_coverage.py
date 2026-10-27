"""_master_coverage must probe ticker coverage with a dictionary read per file —
never `to_pylist()` of the whole ticker column (2026-10-27: 6.5 GB, global OOM,
johnbot scheduler lost the 15:00 ET compute tick)."""
import importlib.util
import pathlib

import pandas as pd
import pyarrow.parquet as pq
import pytest

from src.data import parquet_store as ps

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        'run_acting_ingest', ROOT / 'scripts' / 'run_acting_ingest.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def store(tmp_path, monkeypatch):
    d = tmp_path / 'options_eod'
    d.mkdir()
    for m, tickers in (('2026-09', ['ZZA', 'ZZB']), ('2026-10', ['ZZB', 'ZZC'])):
        pd.DataFrame({'ticker': tickers * 3, 'date': [f'{m}-05'] * 6,
                      'close': [1.0] * 6}).to_parquet(d / f'{m}.parquet', index=False)
    monkeypatch.setattr(ps, 'OPTIONS_DIR', d)
    monkeypatch.setattr(ps, 'OPTIONS_PATH', tmp_path / 'options_eod.parquet')
    monkeypatch.setenv('OPENCLAW_OPTIONS_MONTHLY', '1')
    return d


def test_options_coverage_monthly_dir_counts_union(store, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, 'ROOT', store.parent)
    calls = []
    real = pq.read_table

    def spy(path, **kw):
        calls.append((pathlib.Path(path).name, kw.get('columns'), kw.get('read_dictionary')))
        return real(path, **kw)
    monkeypatch.setattr(pq, 'read_table', spy)
    out = mod._master_coverage('options_eod', ['ZZA', 'ZZB', 'ZZC', 'ZZD'])
    assert out == {'in_master': 3, 'wanted': 4, 'frac': 0.75}
    assert sorted(c[0] for c in calls) == ['2026-09.parquet', '2026-10.parquet']
    assert all(c[1] == ['ticker'] and c[2] == ['ticker'] for c in calls), calls


def test_options_coverage_flag_off_single_file(store, tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.delenv('OPENCLAW_OPTIONS_MONTHLY', raising=False)
    pd.DataFrame({'ticker': ['ZZA', 'ZZQ'], 'date': ['2026-10-05'] * 2,
                  'close': [1.0, 1.0]}).to_parquet(ps.OPTIONS_PATH, index=False)
    out = mod._master_coverage('options_eod', ['ZZA', 'ZZQ', 'ZZZ'])
    assert out == {'in_master': 2, 'wanted': 3, 'frac': 0.6667}
