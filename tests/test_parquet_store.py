"""Roundtrip + concurrency tests for parquet_store.py."""

import multiprocessing
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.data import parquet_store as ps


@pytest.fixture
def tmp_parquet(tmp_path):
    return tmp_path / 'test.parquet'


def _df(rows):
    return pd.DataFrame(rows)


def test_append_dedup_replace_empty_file(tmp_parquet):
    rows = [{'ticker': 'AAPL', 'date': '2026-04-21', 'close': 200.0}]
    count = ps.append_dedup(tmp_parquet, _df(rows), ['ticker', 'date'])
    assert count == 1
    df = pd.read_parquet(tmp_parquet)
    assert len(df) == 1
    assert df.iloc[0]['close'] == 200.0


def test_append_dedup_replace_on_conflict(tmp_parquet):
    rows1 = [
        {'ticker': 'AAPL', 'date': '2026-04-20', 'close': 199.0},
        {'ticker': 'AAPL', 'date': '2026-04-21', 'close': 200.0},
    ]
    rows2 = [
        {'ticker': 'AAPL', 'date': '2026-04-21', 'close': 201.0},   # conflict → wins
        {'ticker': 'AAPL', 'date': '2026-04-22', 'close': 202.0},   # new
    ]
    ps.append_dedup(tmp_parquet, _df(rows1), ['ticker', 'date'])
    final = ps.append_dedup(tmp_parquet, _df(rows2), ['ticker', 'date'])
    assert final == 3

    df = pd.read_parquet(tmp_parquet).sort_values('date').reset_index(drop=True)
    assert df.iloc[0]['close'] == 199.0
    assert df.iloc[1]['close'] == 201.0   # replaced
    assert df.iloc[2]['close'] == 202.0


def test_append_insert_only_preserves_existing(tmp_parquet):
    rows1 = [{'ticker': 'AAPL', 'date': '2026-04-21', 'close': 200.0}]
    rows2 = [{'ticker': 'AAPL', 'date': '2026-04-21', 'close': 999.0}]  # would-be update
    ps.append_insert_only(tmp_parquet, _df(rows1), ['ticker', 'date'])
    final = ps.append_insert_only(tmp_parquet, _df(rows2), ['ticker', 'date'])
    assert final == 1
    df = pd.read_parquet(tmp_parquet)
    assert df.iloc[0]['close'] == 200.0


def test_column_migration_adds_nulls(tmp_parquet):
    """Writing rows with a new column shouldn't drop existing rows' other data."""
    rows1 = [{'ticker': 'AAPL', 'date': '2026-04-21', 'close': 200.0}]
    rows2 = [{'ticker': 'MSFT', 'date': '2026-04-21', 'close': 420.0, 'ev_revenue': 12.3}]
    ps.append_dedup(tmp_parquet, _df(rows1), ['ticker', 'date'])
    ps.append_dedup(tmp_parquet, _df(rows2), ['ticker', 'date'])
    df = pd.read_parquet(tmp_parquet)
    assert set(df.columns) == {'ticker', 'date', 'close', 'ev_revenue'}
    aapl = df[df['ticker'] == 'AAPL'].iloc[0]
    assert pd.isna(aapl['ev_revenue'])
    msft = df[df['ticker'] == 'MSFT'].iloc[0]
    assert msft['ev_revenue'] == 12.3


def test_read_latest_per_ticker(tmp_parquet):
    rows = [
        {'ticker': 'AAPL', 'date': '2026-04-20', 'close': 199.0},
        {'ticker': 'AAPL', 'date': '2026-04-21', 'close': 200.0},
        {'ticker': 'MSFT', 'date': '2026-04-19', 'close': 419.0},
        {'ticker': 'MSFT', 'date': '2026-04-21', 'close': 420.0},
    ]
    ps.append_dedup(tmp_parquet, _df(rows), ['ticker', 'date'])
    latest = ps.read_latest_per_ticker(tmp_parquet, ticker_col='ticker', date_col='date')
    assert len(latest) == 2
    latest = latest.set_index('ticker')
    assert latest.loc['AAPL', 'close'] == 200.0
    assert latest.loc['MSFT', 'close'] == 420.0


def test_read_filtered(tmp_parquet):
    rows = [
        {'ticker': 'AAPL', 'date': '2026-04-20', 'close': 199.0},
        {'ticker': 'AAPL', 'date': '2026-04-21', 'close': 200.0},
        {'ticker': 'MSFT', 'date': '2026-04-21', 'close': 420.0},
    ]
    ps.append_dedup(tmp_parquet, _df(rows), ['ticker', 'date'])
    df = ps.read_filtered(tmp_parquet, where="ticker='AAPL'", order_by='date DESC', limit=10)
    assert len(df) == 2
    assert df.iloc[0]['close'] == 200.0


def test_max_date(tmp_parquet):
    rows = [
        {'ticker': 'AAPL', 'date': '2026-04-20', 'close': 199.0},
        {'ticker': 'AAPL', 'date': '2026-04-21', 'close': 200.0},
    ]
    ps.append_dedup(tmp_parquet, _df(rows), ['ticker', 'date'])
    md = ps.max_date(tmp_parquet)
    assert md == '2026-04-21'


def test_empty_df_is_noop(tmp_parquet):
    rows = [{'ticker': 'AAPL', 'date': '2026-04-21', 'close': 200.0}]
    ps.append_dedup(tmp_parquet, _df(rows), ['ticker', 'date'])
    ps.append_dedup(tmp_parquet, _df([]), ['ticker', 'date'])   # noop
    assert ps.row_count(tmp_parquet) == 1


def _writer_worker(path_str, ticker):
    """Worker used by test_concurrent_writers to test flock serialization."""
    from src.data import parquet_store as ps_inner
    for i in range(20):
        ps_inner.append_dedup(
            path_str,
            pd.DataFrame([{'ticker': ticker, 'date': f'2026-04-{i:02d}', 'close': 100.0 + i}]),
            ['ticker', 'date'],
        )


def test_concurrent_writers_serialize(tmp_parquet):
    """Two concurrent processes writing different tickers shouldn't lose rows."""
    procs = []
    for t in ['AAAA', 'BBBB']:
        p = multiprocessing.Process(target=_writer_worker, args=(str(tmp_parquet), t))
        p.start()
        procs.append(p)
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0

    df = pd.read_parquet(tmp_parquet)
    assert len(df[df['ticker'] == 'AAAA']) == 20
    assert len(df[df['ticker'] == 'BBBB']) == 20


# ── Memory-bounded merge (DuckDB streaming) ──────────────────────────────────
#
# The OOM root cause (2026-06-22/23 EOD collect, rc=137): append_dedup did
# `pd.read_parquet(path)` of the entire master file (5.9M-row options_eod.parquet)
# into pandas to concat+dedup+rewrite — ~4GB peak that the kernel OOM-killed.
# The writer must merge WITHOUT loading the whole existing file into pandas, while
# preserving byte-for-byte the existing dedup semantics.


def _oracle_append_dedup(path, new_df, key_cols, mode='replace'):
    """The ORIGINAL pandas read-concat-dedup logic, kept here as the parity oracle.

    The DuckDB implementation must produce identical results to this."""
    path = Path(path)
    if new_df.empty:
        return pd.read_parquet(path) if path.exists() else pd.DataFrame()
    if path.exists():
        existing = pd.read_parquet(path)
    else:
        existing = pd.DataFrame(columns=new_df.columns)
    all_cols = list(dict.fromkeys(list(existing.columns) + list(new_df.columns)))
    existing = existing.reindex(columns=all_cols)
    new_df = new_df.reindex(columns=all_cols)
    combined = pd.concat([existing, new_df], ignore_index=True)
    keep = 'last' if mode == 'replace' else 'first'
    deduped = combined.drop_duplicates(subset=list(key_cols), keep=keep)
    deduped.to_parquet(path, index=False)
    return deduped


def _normalize(df):
    """Order-independent, dtype-independent canonical form for frame comparison."""
    out = []
    for rec in df.to_dict('records'):
        norm = tuple(
            (k, None if (isinstance(rec[k], float) and pd.isna(rec[k])) else rec[k])
            for k in sorted(rec)
        )
        out.append(norm)
    return sorted(out, key=repr)


def test_append_dedup_does_not_load_full_file_into_pandas(tmp_parquet, monkeypatch):
    """Memory-bound contract: merging into an EXISTING file must not pd.read_parquet it.

    This is the OOM guard — loading the whole 5.9M-row master file into pandas is
    exactly what got OOM-killed. Seed the file, then forbid pd.read_parquet."""
    ps.append_dedup(tmp_parquet, _df([
        {'ticker': 'AAPL', 'date': '2026-04-20', 'close': 199.0},
        {'ticker': 'MSFT', 'date': '2026-04-20', 'close': 419.0},
    ]), ['ticker', 'date'])

    import pandas as _pd
    def _boom(*a, **k):
        raise AssertionError('append_dedup must not pd.read_parquet the existing master file')
    monkeypatch.setattr(_pd, 'read_parquet', _boom)

    final = ps.append_dedup(tmp_parquet, _df([
        {'ticker': 'AAPL', 'date': '2026-04-20', 'close': 200.0},   # replace
        {'ticker': 'NVDA', 'date': '2026-04-20', 'close': 900.0},   # new
    ]), ['ticker', 'date'])
    assert final == 3
    # read back WITHOUT the monkeypatch (restore happens at teardown; read via duckdb)
    got = ps.read_filtered(tmp_parquet)
    by = {r['ticker']: r['close'] for r in got.to_dict('records')}
    assert by == {'AAPL': 200.0, 'MSFT': 419.0, 'NVDA': 900.0}


@pytest.mark.parametrize('mode', ['replace', 'keep-first'])
def test_append_dedup_parity_with_pandas_oracle(tmp_path, mode):
    """DuckDB merge == original pandas logic across a multi-append sequence with
    overlaps, within-batch dups, and a column addition."""
    f_new = tmp_path / 'new.parquet'
    f_ora = tmp_path / 'oracle.parquet'
    batches = [
        [{'ticker': 'AAPL', 'date': '2026-04-20', 'close': 199.0},
         {'ticker': 'MSFT', 'date': '2026-04-20', 'close': 419.0}],
        # within-batch dup on AAPL/04-21 + cross-conflict on AAPL/04-20
        [{'ticker': 'AAPL', 'date': '2026-04-20', 'close': 200.0},
         {'ticker': 'AAPL', 'date': '2026-04-21', 'close': 201.0},
         {'ticker': 'AAPL', 'date': '2026-04-21', 'close': 202.0}],
        # new column appears mid-stream
        [{'ticker': 'NVDA', 'date': '2026-04-21', 'close': 900.0, 'pe': 38.0}],
    ]
    for b in batches:
        ps.append_dedup(f_new, _df(b), ['ticker', 'date'], mode=mode)
        _oracle_append_dedup(f_ora, _df(b), ['ticker', 'date'], mode=mode)
    assert _normalize(pd.read_parquet(f_new)) == _normalize(pd.read_parquet(f_ora))


def test_append_dedup_null_safe_keys(tmp_parquet):
    """A NULL key value dedups like pandas (NaN==NaN treated as duplicate)."""
    ps.append_dedup(tmp_parquet, _df([
        {'ticker': 'SPX', 'date': '2026-04-20', 'expiry': None, 'close': 1.0},
    ]), ['ticker', 'date', 'expiry'])
    final = ps.append_dedup(tmp_parquet, _df([
        {'ticker': 'SPX', 'date': '2026-04-20', 'expiry': None, 'close': 2.0},  # same NULL key → replace
    ]), ['ticker', 'date', 'expiry'])
    assert final == 1
    df = pd.read_parquet(tmp_parquet)
    assert df.iloc[0]['close'] == 2.0


def test_append_dedup_preserves_all_existing_rows_on_disjoint_append(tmp_parquet):
    """NEVER-DELETE invariant: appending disjoint keys never drops existing rows."""
    seed = [{'ticker': f'T{i}', 'date': '2026-04-20', 'close': float(i)} for i in range(50)]
    ps.append_dedup(tmp_parquet, _df(seed), ['ticker', 'date'])
    final = ps.append_dedup(tmp_parquet, _df([
        {'ticker': 'NEW', 'date': '2026-04-21', 'close': 1.0},
    ]), ['ticker', 'date'])
    assert final == 51
    tickers = set(pd.read_parquet(tmp_parquet)['ticker'])
    assert all(f'T{i}' in tickers for i in range(50)) and 'NEW' in tickers


def test_named_writers(tmp_path, monkeypatch):
    """write_prices / write_options / write_macro etc write to their canonical paths."""
    monkeypatch.setattr(ps, 'PRICES_PATH',       tmp_path / 'prices.parquet')
    monkeypatch.setattr(ps, 'OPTIONS_PATH',      tmp_path / 'options_eod.parquet')
    monkeypatch.setattr(ps, 'FUNDAMENTALS_PATH', tmp_path / 'financials.parquet')
    monkeypatch.setattr(ps, 'INSIDER_PATH',      tmp_path / 'insider.parquet')
    monkeypatch.setattr(ps, 'MACRO_PATH',        tmp_path / 'macro.parquet')

    assert ps.write_prices([{'ticker': 'AAPL', 'date': '2026-04-21', 'close': 200.0}]) == 1
    assert ps.write_options([{
        'ticker': 'AAPL', 'date': '2026-04-21',
        'expiry': '2026-04-25', 'strike': 200.0, 'option_type': 'CALL',
        'implied_volatility': 0.5,
    }]) == 1
    assert ps.write_fundamentals([{
        'ticker': 'AAPL', 'period': 'Q1-2026',
        'revenue': 100e9, 'ev_revenue': 3.5,
    }]) == 1
    assert ps.write_insider([{
        'ticker': 'AAPL', 'filing_date': '2026-04-21',
        'insider_name': 'Jane', 'transaction_type': 'P-Purchase', 'shares': 1000,
    }]) == 1
    assert ps.write_macro([{'date': '2026-04-21', 'series': 'VIX', 'value': 18.87}]) == 1
