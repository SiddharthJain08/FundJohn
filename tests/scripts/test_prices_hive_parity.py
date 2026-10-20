"""A7: build_prices_hive + prices_hive_parity (tmp_path only)."""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (ROOT, ROOT / 'scripts'):
    sys.path.insert(0, str(_p))

import build_prices_hive as bph
import prices_hive_parity as php


def _src(tmp_path):
    rows = []
    for d in pd.bdate_range('2024-12-30', '2026-01-10'):
        for t in ('ZZTEST1', 'ZZTEST2', 'ZZTEST3'):
            rows.append({'ticker': t, 'date': d.strftime('%Y-%m-%d'),
                         'close': float(len(rows))})
    p = tmp_path / 'prices.parquet'
    assert str(p).startswith(str(tmp_path))
    pd.DataFrame(rows).to_parquet(p, index=False)
    return p


def _build(tmp_path):
    src = _src(tmp_path)
    out = tmp_path / 'prices'
    r = bph.build(src, out, years=None, apply=True, spill_dir=tmp_path / 'spill')
    return src, out, r


def test_build_then_parity_pass(tmp_path):
    src, out, r = _build(tmp_path)
    assert r['rows_src'] == r['rows_hive'] and r['applied'] is True
    assert sorted(p.name for p in out.iterdir()) == ['year=2024', 'year=2025', 'year=2026']
    res = php.parity(src, out)
    assert res['verdict'] == 'PASS'
    assert res['dup_keys_hive'] == 0 and res['ticker_maxdate_mismatch'] == 0
    assert res['sample_mismatch'] == 0 and res['rows_file'] == res['rows_hive']


def test_dry_run_writes_nothing(tmp_path):
    src = _src(tmp_path)
    out = tmp_path / 'prices'
    r = bph.build(src, out, years=None, apply=False, spill_dir=tmp_path / 'spill')
    assert r['applied'] is False and not out.exists()


def test_parity_detects_drift(tmp_path, capsys):
    src, out, _ = _build(tmp_path)
    df = pd.read_parquet(src)
    extra = pd.DataFrame([{'ticker': 'ZZTEST1', 'date': '2026-01-12', 'close': 1.0}])
    pd.concat([df, extra]).to_parquet(src, index=False)
    res = php.parity(src, out)
    assert res['ticker_maxdate_mismatch'] > 0 and res['verdict'] == 'FAIL'
    rc = php.main(['--src', str(src), '--hive-dir', str(out)])
    assert rc == 1
    assert 'verdict=FAIL' in capsys.readouterr().out
