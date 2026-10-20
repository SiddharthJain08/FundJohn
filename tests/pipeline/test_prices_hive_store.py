"""A7: OPENCLAW_PRICES_HIVE dual-write in parquet_store.write_prices (tmp_path only)."""
import logging
import sys
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pads
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.data import parquet_store as ps


@pytest.fixture
def store(tmp_path, monkeypatch):
    single = tmp_path / 'prices.parquet'
    hive = tmp_path / 'prices'
    monkeypatch.setattr(ps, 'PRICES_PATH', single)
    monkeypatch.setattr(ps, 'PRICES_HIVE_DIR', hive)
    monkeypatch.setenv('OPENCLAW_PARQUET_SPILL_DIR', str(tmp_path / 'spill'))
    (tmp_path / 'spill').mkdir()
    for p in (single, hive):
        assert str(p).startswith(str(tmp_path))
    monkeypatch.delenv('OPENCLAW_PRICES_HIVE', raising=False)
    return single, hive


def _rows(dates, close=1.0):
    return [{'ticker': t, 'date': d, 'close': close}
            for t in ('ZZTEST1', 'ZZTEST2') for d in dates]


def test_flag_unset_no_hive_tree(store, monkeypatch):
    single, hive = store
    calls = []
    real = ps.append_dedup
    monkeypatch.setattr(ps, 'append_dedup', lambda *a, **k: (calls.append(a[0]), real(*a, **k))[1])
    n = ps.write_prices(_rows(['2026-01-02']))
    assert n == 2
    assert len(calls) == 1 and Path(calls[0]) == single
    assert not hive.exists()


def test_dual_write_splits_by_year_and_dedups(store, monkeypatch):
    single, hive = store
    monkeypatch.setenv('OPENCLAW_PRICES_HIVE', '1')
    ps.write_prices(_rows(['2025-12-31', '2026-01-02']))
    assert ps.prices_hive_part(2025).exists() and ps.prices_hive_part(2026).exists()
    assert ps.prices_hive_part(2026) == hive / 'year=2026' / 'part-0.parquet'
    ps.write_prices(_rows(['2026-01-02'], close=9.0))
    df = pd.read_parquet(ps.prices_hive_part(2026))
    assert len(df) == 2 and set(df['close']) == {9.0}
    assert pq.read_schema(ps.prices_hive_part(2026)).field('date_d').type == pa.date32()


def test_hive_part_file_has_no_year_column(store, monkeypatch):
    single, hive = store
    monkeypatch.setenv('OPENCLAW_PRICES_HIVE', '1')
    ps.write_prices(_rows(['2026-01-02']))
    ps.write_prices(_rows(['2026-01-05']))
    assert 'year' not in pq.read_schema(ps.prices_hive_part(2026)).names
    t = pads.dataset(hive, partitioning='hive').to_table()
    assert t.num_rows == 4


def test_hive_failure_never_breaks_authoritative_write(store, monkeypatch, caplog, capsys):
    single, hive = store
    monkeypatch.setenv('OPENCLAW_PRICES_HIVE', '1')
    real = ps.append_dedup

    def fake(path, *a, **k):
        if Path(path) != single:
            raise OSError('boom')
        return real(path, *a, **k)
    monkeypatch.setattr(ps, 'append_dedup', fake)
    caplog.set_level(logging.INFO)
    n = ps.write_prices(_rows(['2026-01-02']))
    assert n == 2 and len(pd.read_parquet(single)) == 2
    out = capsys.readouterr().err + caplog.text
    assert out.count('[prices_hive] status=error:OSError') == 1
