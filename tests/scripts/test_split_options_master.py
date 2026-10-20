"""A6: split_options_master.py on a tiny synthetic source (never the real master)."""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    'split_options_master', ROOT / 'scripts' / 'split_options_master.py')
sp = importlib.util.module_from_spec(_SPEC)
sys.modules['split_options_master'] = sp
_SPEC.loader.exec_module(sp)


def _df(dates):
    return pd.DataFrame([{'ticker': t, 'date': d, 'contract_symbol': f'{t}{d}', 'v': float(i)}
                         for i, (t, d) in enumerate((t, d) for d in dates for t in ('ZZTEST1', 'ZZTEST2'))])


@pytest.fixture
def env(tmp_path):
    src = tmp_path / 'options_eod.parquet'
    _df(['2026-08-31', '2026-09-15', '2026-09-30', '2026-10-01']).to_parquet(src, index=False)
    return src, tmp_path / 'out', tmp_path / 'spill'


def _kw(spill, **k):
    return dict(months=None, apply=True, spill_dir=spill, **k)


def test_split_roundtrip_counts(env):
    src, out, spill = env
    r = sp.split(src, out, **_kw(spill))
    assert r['mismatches'] == [] and r['applied'] is True
    names = sorted(f.name for f in out.glob('*.parquet'))
    assert names == ['2026-08.parquet', '2026-09.parquet', '2026-10.parquet']
    assert [pq.read_metadata(out / n).num_rows for n in names] == [2, 4, 2]
    union = pd.concat([pd.read_parquet(out / n) for n in names])
    key = ['ticker', 'date', 'contract_symbol', 'v']
    a = union.sort_values(key).reset_index(drop=True)[key]
    b = pd.read_parquet(src).sort_values(key).reset_index(drop=True)[key]
    pd.testing.assert_frame_equal(a, b)
    assert not list(out.glob('*.tmp'))


def test_split_month_refresh(env):
    src, out, spill = env
    sp.split(src, out, **_kw(spill))
    before = {f.name: f.stat().st_mtime_ns for f in out.glob('*.parquet')}
    time.sleep(0.05)
    pd.concat([pd.read_parquet(src), _df(['2026-10-02'])]).to_parquet(src, index=False)
    r = sp.split(src, out, months=['2026-10'], apply=True, spill_dir=spill)
    assert r['mismatches'] == []
    after = {f.name: f.stat().st_mtime_ns for f in out.glob('*.parquet')}
    assert after['2026-08.parquet'] == before['2026-08.parquet']
    assert after['2026-09.parquet'] == before['2026-09.parquet']
    assert after['2026-10.parquet'] != before['2026-10.parquet']
    assert pq.read_metadata(out / '2026-10.parquet').num_rows == 4


def test_verify_detects_mismatch(env):
    src, out, spill = env
    sp.split(src, out, **_kw(spill))
    assert sp.main(['--verify', '--src', str(src), '--out-dir', str(out),
                    '--spill-dir', str(spill)]) == 0
    f = out / '2026-09.parquet'
    pd.read_parquet(f).iloc[1:].to_parquet(f, index=False)
    with pytest.raises(SystemExit) as e:
        sp.main(['--verify', '--src', str(src), '--out-dir', str(out),
                 '--spill-dir', str(spill)])
    assert e.value.code == 2
