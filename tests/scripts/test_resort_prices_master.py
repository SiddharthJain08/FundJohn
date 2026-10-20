from __future__ import annotations
import hashlib, importlib.util, math, os
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[2]


def _mod():
    spec = importlib.util.spec_from_file_location('resort_prices_master', ROOT / 'scripts' / 'resort_prices_master.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _fixture(tmp_path, n_days=400, rg=100, n_tickers=3) -> Path:
    """Ticker-contiguous (the bad layout): n_tickers x n_days."""
    days = pd.bdate_range('2024-01-01', periods=n_days)
    frames = []
    for i, t in enumerate(f'ZZTEST{k}' for k in range(1, n_tickers + 1)):
        frames.append(pd.DataFrame({'ticker': t, 'date': days, 'close': [100.0 + i + k * 0.1 for k in range(n_days)],
                                    'volume': list(range(n_days))}))
    df = pd.concat(frames, ignore_index=True)
    p = tmp_path / 'prices.parquet'
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), p, row_group_size=rg)
    return p


def _call(m, p, tmp_path, **kw):
    kw.setdefault('row_group_size', 256)
    return m.resort(p, spill_dir=tmp_path / 'spill', **kw)


def test_resort_dry_run_leaves_master_untouched(tmp_path):
    # DuckDB COPY cuts row groups in 2048-row vectors, so tightness needs many tickers per day
    m = _mod(); p = _fixture(tmp_path, n_tickers=60); before = _sha(p)
    r = _call(m, p, tmp_path, apply=False, row_group_size=2048)  # ~34 bdays/group -> tight
    assert _sha(p) == before
    assert not list(tmp_path.glob('*.resort.tmp'))
    assert r['rows_old'] == r['rows_new'] == 24000
    assert r['colhash_equal'] and r['rowhash_equal'] and r['schema_equal']
    assert r['verdict_new'] == 'PASS' and r['applied'] is False and r['backup'] is None


def test_resort_apply_sorts_and_keeps_backup(tmp_path):
    from src.data.parquet_layout import rowgroup_span_report
    m = _mod(); p = _fixture(tmp_path)
    old_sha, old_ino = _sha(p), p.stat().st_ino
    old_rows = sorted(pd.read_parquet(p)[['ticker', 'date', 'close']].itertuples(index=False, name=None))
    r = _call(m, p, tmp_path, apply=True, today=date(2026, 10, 20))
    bk = tmp_path / 'prices.parquet.pre-resort-20261020'
    assert r['applied'] and bk.exists() and _sha(bk) == old_sha and bk.stat().st_ino == old_ino
    assert rowgroup_span_report(p)['monotone'] is True
    new_rows = sorted(pd.read_parquet(p)[['ticker', 'date', 'close']].itertuples(index=False, name=None))
    assert new_rows == old_rows


def test_resort_output_is_sorted_and_tight(tmp_path):
    from src.data.parquet_layout import rowgroup_span_report
    m = _mod(); p = _fixture(tmp_path, n_days=100, n_tickers=60)
    _call(m, p, tmp_path, apply=True, today=date(2026, 10, 20), row_group_size=2048)
    r = rowgroup_span_report(p)
    assert r['monotone'] is True and r['frac_tight'] == 1.0
    assert pq.ParquetFile(p).metadata.num_row_groups == math.ceil(6000 / 2048)


def test_resort_refuses_on_hash_mismatch(monkeypatch, tmp_path):
    m = _mod(); p = _fixture(tmp_path); before = _sha(p)
    real = m._hashes

    def fake(con, src):
        h = real(con, src)
        if str(src).endswith('.resort.tmp'):
            h = dict(h); h['rowhash'] = h['rowhash'] + 1
        return h
    monkeypatch.setattr(m, '_hashes', fake)
    with pytest.raises(SystemExit) as e:
        m.main(['--path', str(p), '--spill-dir', str(tmp_path / 'spill'), '--row-group-size', '256', '--apply'])
    assert e.value.code == 2
    assert _sha(p) == before
    assert not list(tmp_path.glob('*pre-resort*')) and not list(tmp_path.glob('*.resort.tmp'))


def test_resort_takes_master_lock(tmp_path):
    from src.data import parquet_store
    m = _mod(); p = _fixture(tmp_path)
    with parquet_store._file_lock(p):
        with pytest.raises(TimeoutError):
            _call(m, p, tmp_path, apply=False, lock_timeout=0.2)
