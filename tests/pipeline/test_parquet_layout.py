"""A1: writer discipline (page index, row-group size) + footer-only layout report."""
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.data import parquet_layout as pl
from src.data import parquet_store as ps

TICKERS = ['ZZTEST1', 'ZZTEST2', 'ZZTEST3']


def _frame(n_dates, tickers=TICKERS, start='2024-01-01', close=1.0):
    dates = pd.bdate_range(start, periods=n_dates)
    return pd.DataFrame(
        [(d, t, close) for t in tickers for d in dates],
        columns=['date', 'ticker', 'close'],
    )


def test_atomic_write_page_index_and_rowgroup_size(tmp_path):
    p = tmp_path / 'a.parquet'
    df = pd.DataFrame({'date': pd.bdate_range('2024-01-01', periods=1000),
                       'x': range(1000)})
    ps._atomic_write(p, df, row_group_size=256)
    md = pq.ParquetFile(p).metadata
    assert md.num_row_groups == 4
    col = md.row_group(0).column(0)
    assert col.has_column_index and col.has_offset_index


def test_atomic_write_default_rowgroup_size(monkeypatch, tmp_path):
    calls = []
    real = pq.write_table

    def spy(*a, **kw):
        calls.append(kw)
        return real(*a, **kw)

    monkeypatch.setattr(ps.pq, 'write_table', spy)
    ps._atomic_write(tmp_path / 'b.parquet', _frame(5))
    assert calls[0]['row_group_size'] == 131072
    assert calls[0]['write_page_index'] is True
    assert calls[0]['compression'] == 'snappy'


def test_append_dedup_keeps_date_monotone_after_sort(tmp_path):
    p = tmp_path / 'c.parquet'
    seed = _frame(30).sort_values(['date', 'ticker'])
    ps._atomic_write(p, seed)
    new_date = pd.bdate_range('2024-01-01', periods=31)[-1]
    first = pd.DataFrame({'date': [new_date] * 3, 'ticker': TICKERS,
                          'close': [1.0, 1.0, 1.0]})
    ps.append_dedup(p, first, ['date', 'ticker'], mode='replace')
    second = first.assign(close=[7.0, 8.0, 9.0])
    ps.append_dedup(p, second, ['date', 'ticker'], mode='replace')
    out = pd.read_parquet(p)
    assert out['date'].is_monotonic_increasing
    day = out[out['date'] == new_date].sort_values('ticker')
    assert len(day) == 3
    assert list(day['close']) == [7.0, 8.0, 9.0]


def _duckdb_copy(src: Path, dst: Path):
    con = duckdb.connect()
    try:
        con.execute(f"COPY (SELECT * FROM read_parquet('{src}')) TO '{dst}' "
                    f"(FORMAT PARQUET, COMPRESSION 'snappy', ROW_GROUP_SIZE 100)")
    finally:
        con.close()


def test_span_report_ticker_contiguous_vs_date_sorted(tmp_path):
    df = _frame(400)  # ticker-contiguous
    p1 = tmp_path / 'contig.parquet'
    ps._atomic_write(p1, df, row_group_size=100)
    r1 = pl.rowgroup_span_report(p1)
    assert r1['frac_tight'] < 0.95
    assert r1['monotone'] is False
    assert pl.layout_verdict(r1)[0] == 'FAIL'

    srt = df.sort_values(['date', 'ticker'], kind='stable').reset_index(drop=True)
    p2 = tmp_path / 'sorted.parquet'
    ps._atomic_write(p2, srt, row_group_size=100)
    r2 = pl.rowgroup_span_report(p2)
    assert r2['frac_tight'] == 1.0
    assert r2['monotone'] is True
    assert r2['n_row_groups'] == 12
    assert pl.layout_verdict(r2)[0] == 'PASS'

    p3 = tmp_path / 'duck.parquet'
    _duckdb_copy(p2, p3)
    r3 = pl.rowgroup_span_report(p3)
    verdict, detail = pl.layout_verdict(r3)
    assert verdict == 'PASS'
    assert r3['page_index'] is False
    assert 'page_index=False' in detail
    assert len(detail) <= 200


def test_verdict_fails_on_missing_stats():
    rep = {'n_row_groups': 2, 'n_tight': 2, 'frac_tight': 1.0,
           'median_span_days': 1.0, 'max_span_days_seen': 1,
           'page_index': True, 'monotone': True, 'missing_stats': 1}
    verdict, detail = pl.layout_verdict(rep)
    assert verdict == 'FAIL'
    assert len(detail) <= 200


def test_span_report_reads_footer_only(monkeypatch, tmp_path):
    p = tmp_path / 'f.parquet'
    ps._atomic_write(p, _frame(50).sort_values(['date', 'ticker']),
                     row_group_size=40)

    def boom(*a, **k):
        raise AssertionError('data read')

    monkeypatch.setattr(pq, 'read_table', boom)
    monkeypatch.setattr(pd, 'read_parquet', boom)
    rep = pl.rowgroup_span_report(p)
    assert rep['n_row_groups'] >= 1
