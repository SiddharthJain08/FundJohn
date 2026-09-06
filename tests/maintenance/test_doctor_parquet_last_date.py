"""doctor._parquet_last_date must read parquet ROW-GROUP STATISTICS, never the
date column itself (ERR-20260906-001: the column read of the 62 M-row
options_eod.parquet peaked at 3.4 GB inside johnbot's ExecStartPre and timed
the boot out while a fleet child held the rest of the box)."""
from __future__ import annotations
import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from maintenance import doctor  # noqa: E402


def _write(path, dates, arrow_type, row_group_size=2):
    if arrow_type == 'string':
        col = pa.array([d.isoformat() for d in dates], pa.string())
    elif arrow_type == 'date32':
        col = pa.array(dates, pa.date32())
    else:
        col = pa.array([pd.Timestamp(d) for d in dates], pa.timestamp('ns'))
    tbl = pa.table({'date': col, 'x': pa.array(range(len(dates)))})
    pq.write_table(tbl, path, row_group_size=row_group_size)


DATES = [dt.date(2026, 9, 1), dt.date(2026, 9, 4), dt.date(2026, 8, 30), dt.date(2026, 9, 3), dt.date(2026, 9, 2)]


@pytest.mark.parametrize('arrow_type', ['string', 'date32', 'timestamp'])
def test_last_date_from_row_group_statistics(tmp_path, arrow_type):
    p = tmp_path / f'{arrow_type}.parquet'
    _write(p, DATES, arrow_type)                     # 3 row groups; the max sits in the FIRST one
    assert pq.ParquetFile(p).metadata.num_row_groups == 3
    assert doctor._parquet_last_date(str(p)) == dt.date(2026, 9, 4)


def test_never_reads_the_date_column(tmp_path, monkeypatch):
    p = tmp_path / 's.parquet'
    _write(p, DATES, 'string')

    def boom(*a, **k):
        raise AssertionError('full column read — the whole point of the fix is to avoid this')
    monkeypatch.setattr(pd, 'read_parquet', boom)
    monkeypatch.setattr(pq, 'read_table', boom)
    assert doctor._parquet_last_date(str(p)) == dt.date(2026, 9, 4)


def test_row_group_without_statistics_falls_back_to_that_row_group_only(tmp_path, monkeypatch):
    p = tmp_path / 'nostats.parquet'
    tbl = pa.table({'date': pa.array([d.isoformat() for d in DATES], pa.string()), 'x': pa.array(range(5))})
    pq.write_table(tbl, p, row_group_size=2, write_statistics=False)
    monkeypatch.setattr(pd, 'read_parquet', lambda *a, **k: (_ for _ in ()).throw(AssertionError('full read')))
    assert doctor._parquet_last_date(str(p)) == dt.date(2026, 9, 4)


def test_missing_and_empty_files(tmp_path):
    assert doctor._parquet_last_date(str(tmp_path / 'nope.parquet')) is None
    p = tmp_path / 'empty.parquet'
    pq.write_table(pa.table({'date': pa.array([], pa.string())}), p)
    assert doctor._parquet_last_date(str(p)) is None
