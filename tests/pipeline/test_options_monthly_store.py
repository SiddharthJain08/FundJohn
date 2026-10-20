"""A6: per-month options store behind OPENCLAW_OPTIONS_MONTHLY (tiny tmp parquet)."""
from __future__ import annotations

import hashlib
import logging

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.data import parquet_store as ps


def _rows(dates, tickers=('ZZTEST1', 'ZZTEST2'), price=1.0):
    out = []
    for d in dates:
        for t in tickers:
            out.append({'ticker': t, 'date': d, 'expiry': '2026-12-18', 'strike': 100.0,
                        'option_type': 'call', 'contract_symbol': f'{t}{d}C',
                        'market_price': price, 'open_interest': 5.0})
    return out


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, 'OPTIONS_PATH', tmp_path / 'options_eod.parquet')
    monkeypatch.setattr(ps, 'OPTIONS_DIR', tmp_path / 'options_eod')
    monkeypatch.delenv('OPENCLAW_OPTIONS_MONTHLY', raising=False)
    return tmp_path


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_flag_unset_writes_single_file_byte_identical(store, monkeypatch):
    calls = []
    real = ps.append_dedup
    monkeypatch.setattr(ps, 'append_dedup',
                        lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    n = ps.write_options(_rows(['2026-09-30', '2026-10-01']))
    assert n == 4
    assert len(calls) == 1 and calls[0][0] == ps.OPTIONS_PATH
    assert list(calls[0][2]) == ps.OPTIONS_KEYS
    assert not ps.OPTIONS_DIR.exists()
    assert ps.options_path() == ps.OPTIONS_PATH


def test_append_options_splits_batch_by_month(store, monkeypatch):
    monkeypatch.setenv('OPENCLAW_OPTIONS_MONTHLY', '1')
    assert ps.write_options(_rows(['2026-09-30', '2026-10-01'])) == 4
    files = ps.options_month_files()
    assert [f.name for f in files] == ['2026-09.parquet', '2026-10.parquet']
    for f in files:
        d = pd.read_parquet(f)['date'].astype(str).str[:7].unique()
        assert list(d) == [f.stem]
    sep = _sha(files[0])
    assert ps.write_options(_rows(['2026-10-01'], price=9.0)) == 4
    assert _sha(files[0]) == sep
    oct_ = pd.read_parquet(files[1])
    assert len(oct_) == 2 and (oct_['market_price'] == 9.0).all()


def test_dataset_unifies_null_typed_month(store, monkeypatch):
    monkeypatch.setenv('OPENCLAW_OPTIONS_MONTHLY', '1')
    ps.OPTIONS_DIR.mkdir()
    a = pd.DataFrame({'ticker': ['ZZTEST1'], 'open_interest': [None]})
    b = pd.DataFrame({'ticker': ['ZZTEST2'], 'open_interest': [3.0]})
    pq.write_table(pa.Table.from_pandas(a.assign(open_interest=pa.nulls(1))), ps.OPTIONS_DIR / '2026-08.parquet')
    pq.write_table(pa.Table.from_pandas(b), ps.OPTIONS_DIR / '2026-09.parquet')
    assert pq.read_schema(ps.OPTIONS_DIR / '2026-08.parquet').field('open_interest').type == pa.null()
    t = ps.options_read_table(['ticker', 'open_interest'])
    assert t.num_rows == 2 and t.schema.field('open_interest').type == pa.float64()
    t2 = ps.options_read_table(['ticker'], filters=[('ticker', '=', 'ZZTEST2')],
                               read_dictionary=['ticker'])
    assert t2.num_rows == 1
    assert pa.types.is_dictionary(t2.schema.field('ticker').type)


def test_options_dataset_empty_dir_raises(store, monkeypatch):
    monkeypatch.setenv('OPENCLAW_OPTIONS_MONTHLY', '1')
    with pytest.raises(FileNotFoundError):
        ps.options_dataset()


def test_read_filtered_max_date_row_count_dir_aware(store, monkeypatch):
    monkeypatch.setenv('OPENCLAW_OPTIONS_MONTHLY', '1')
    rows = _rows(['2026-08-31', '2026-09-30', '2026-10-01'])
    ps.write_options(rows)
    oracle = pd.DataFrame(rows)
    p = ps.options_path()
    got = ps.read_filtered(p, where="ticker='ZZTEST1'")
    assert len(got) == (oracle.ticker == 'ZZTEST1').sum()
    assert ps.max_date(p) == oracle['date'].max()
    assert ps.row_count(p) == len(oracle)
    assert len(ps.read_latest_per_ticker(p)) == 2


def test_alpaca_merge_write_routes_and_keeps_keys(store, monkeypatch):
    from src.pipeline.backfillers import alpaca_options as ao
    monkeypatch.setenv('OPENCLAW_OPTIONS_MONTHLY', '1')
    seen = {}
    monkeypatch.setattr(ps, 'append_options',
                        lambda df, key_cols=None, mode='replace': seen.update(k=list(key_cols), m=mode))
    df = pd.DataFrame(_rows(['2026-10-01']))
    ao._merge_write(df, parquet_path=ps.OPTIONS_PATH)
    assert seen == {'k': ['date', 'contract_symbol'], 'm': 'replace'}
    other = store / 'other.parquet'
    ao._merge_write(df, parquet_path=other)
    assert other.exists() and not ps.OPTIONS_DIR.exists()
    assert len(pd.read_parquet(other)) == 2


def test_alpaca_merge_write_flag_off_single_file(store):
    from src.pipeline.backfillers import alpaca_options as ao
    ao._merge_write(pd.DataFrame(_rows(['2026-10-01'])), parquet_path=ps.OPTIONS_PATH)
    assert ps.OPTIONS_PATH.exists() and not ps.OPTIONS_DIR.exists()


def test_master_readable_checks_month_files(store, monkeypatch):
    from src.pipeline.backfillers import alpaca_options as ao
    monkeypatch.setenv('OPENCLAW_OPTIONS_MONTHLY', '1')
    ps.write_options(_rows(['2026-10-01']))
    assert ao._master_readable(ps.OPTIONS_PATH)
    (ps.OPTIONS_DIR / '2026-10.parquet').write_bytes(b'garbage')
    assert not ao._master_readable(ps.OPTIONS_PATH)


def test_options_store_log_line(store, monkeypatch, capsys):
    monkeypatch.setenv('OPENCLAW_OPTIONS_MONTHLY', '1')
    ps.write_options(_rows(['2026-09-30', '2026-10-01']))
    lines = [l for l in capsys.readouterr().err.splitlines() if l.startswith('[options_store]')]
    assert lines == ['[options_store] mode=monthly months=2026-09,2026-10 rows_in=4 total=4']


def test_null_or_unparseable_date_raises(store, monkeypatch):
    monkeypatch.setenv('OPENCLAW_OPTIONS_MONTHLY', '1')
    rows = _rows(['2026-09-30', '2026-10-01'])
    rows[1]['date'] = None
    with pytest.raises(ValueError, match=r'1 rows with NULL/unparseable date'):
        ps.write_options(rows)
    assert not ps.OPTIONS_DIR.exists()


def test_mixed_date_formats_split_by_text_month(store, monkeypatch):
    monkeypatch.setenv('OPENCLAW_OPTIONS_MONTHLY', '1')
    ps.write_options(_rows(['2026-09-30']) + _rows(['2026-10-01T00:00:00']))
    assert [f.name for f in ps.options_month_files()] == ['2026-09.parquet', '2026-10.parquet']


def test_empty_dir_reads_with_flag_on(store, monkeypatch):
    monkeypatch.setenv('OPENCLAW_OPTIONS_MONTHLY', '1')
    ps.OPTIONS_DIR.mkdir()
    p = ps.options_path()
    assert ps.read_filtered(p, where="ticker='ZZTEST1'").empty
    assert ps.max_date(p) is None
    assert ps.row_count(p) == 0
