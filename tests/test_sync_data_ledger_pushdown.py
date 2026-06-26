"""Parity + edge-case regression for sync_data_ledger._stats column-pushdown.

_stats runs at every johnbot boot. It was rewritten 2026-06-26 to read ONLY the
date+ticker columns (row_count from metadata) instead of a full pd.read_parquet
of every master parquet — the boot peak on options_eod dropped ~2.9 GB -> ~0.9 GB
(prices ~1.5 GB -> ~1.1 GB). This test pins that the pushdown output is identical
to a full-read reference and that the historical edge cases still hold.
"""
from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.strategies.sync_data_ledger import _stats

KEYS = ('min_date', 'max_date', 'row_count', 'ticker_count')


def _ref_full(parquet_path) -> dict:
    """The pre-pushdown full-read logic — the parity oracle."""
    try:
        df = pd.read_parquet(parquet_path)
    except Exception as e:  # noqa: BLE001
        return {'min_date': None, 'max_date': None, 'row_count': 0, 'ticker_count': 0, 'error': str(e)}
    row_count = len(df)
    if row_count == 0:
        return {'min_date': None, 'max_date': None, 'row_count': 0, 'ticker_count': 0}
    date_col = next((c for c in ['date', 'Date', 'timestamp', 'Timestamp', 'ts_utc'] if c in df.columns), None)
    min_date = max_date = None
    if date_col:
        try:
            dates = pd.to_datetime(df[date_col], errors='coerce').dropna()
            if not dates.empty:
                today = pd.Timestamp.today().normalize()
                hist = dates[dates <= today]
                if not hist.empty:
                    min_date = hist.min().date().isoformat()
                    max_date = hist.max().date().isoformat()
        except Exception:  # noqa: BLE001
            pass
    ticker_col = next((c for c in ['ticker', 'Ticker', 'symbol', 'Symbol'] if c in df.columns), None)
    ticker_count = int(df[ticker_col].nunique()) if ticker_col else 0
    return {'min_date': min_date, 'max_date': max_date, 'row_count': row_count, 'ticker_count': ticker_count}


def _write(tmp_path, name: str, df: pd.DataFrame):
    p = tmp_path / f'{name}.parquet'
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), p)
    return p


def _eq(a: dict, b: dict) -> bool:
    return all(a.get(k) == b.get(k) for k in KEYS)


def test_standard_date_ticker(tmp_path):
    df = pd.DataFrame({
        'ticker': ['AAA', 'BBB', 'AAA', 'CCC'],
        'date': pd.to_datetime(['2020-01-01', '2020-01-02', '2021-06-01', '2019-12-31']),
        'close': [1.0, 2.0, 3.0, 4.0],
        'volume': [10, 20, 30, 40],
    })
    p = _write(tmp_path, 'prices', df)
    s = _stats(p)
    assert _eq(s, _ref_full(p))
    assert s == {'min_date': '2019-12-31', 'max_date': '2021-06-01', 'row_count': 4, 'ticker_count': 3}


def test_future_dates_excluded(tmp_path):
    """Future-dated rows count toward row_count/ticker_count but not min/max date."""
    future = pd.Timestamp.today().normalize() + pd.Timedelta(days=400)
    df = pd.DataFrame({
        'ticker': ['AAA', 'ZZZ'],
        'date': [pd.Timestamp('2020-01-01'), future],
    })
    p = _write(tmp_path, 'earnings_calendar', df)
    s = _stats(p)
    assert _eq(s, _ref_full(p))
    assert s['min_date'] == '2020-01-01' and s['max_date'] == '2020-01-01'
    assert s['row_count'] == 2 and s['ticker_count'] == 2


def test_all_future_dates(tmp_path):
    """earnings_calendar real case: every date is future -> None..None, tickers still counted."""
    f1 = pd.Timestamp.today().normalize() + pd.Timedelta(days=10)
    f2 = pd.Timestamp.today().normalize() + pd.Timedelta(days=20)
    df = pd.DataFrame({'symbol': ['AAA', 'BBB'], 'date': [f1, f2]})
    p = _write(tmp_path, 'earnings_calendar', df)
    s = _stats(p)
    assert _eq(s, _ref_full(p))
    assert s['min_date'] is None and s['max_date'] is None and s['ticker_count'] == 2


def test_ts_utc_and_no_ticker(tmp_path):
    """intraday_features: ts_utc date column, no ticker/symbol column -> ticker_count 0."""
    df = pd.DataFrame({
        'ts_utc': pd.to_datetime(['2024-04-18 09:30', '2024-04-18 10:00', '2024-04-19 09:30']),
        'feature_a': [0.1, 0.2, 0.3],
    })
    p = _write(tmp_path, 'intraday_features', df)
    s = _stats(p)
    assert _eq(s, _ref_full(p))
    assert s['ticker_count'] == 0 and s['min_date'] == '2024-04-18' and s['row_count'] == 3


def test_symbol_column_variant(tmp_path):
    df = pd.DataFrame({'symbol': ['X', 'Y', 'X'], 'timestamp': pd.to_datetime(['2022-01-01'] * 3)})
    p = _write(tmp_path, 'misc', df)
    s = _stats(p)
    assert _eq(s, _ref_full(p))
    assert s['ticker_count'] == 2


def test_null_dates_and_tickers(tmp_path):
    df = pd.DataFrame({
        'ticker': ['AAA', None, 'AAA', 'BBB'],
        'date': [pd.Timestamp('2020-01-01'), pd.NaT, pd.Timestamp('2020-03-01'), pd.NaT],
    })
    p = _write(tmp_path, 'sparse', df)
    s = _stats(p)
    assert _eq(s, _ref_full(p))  # nunique drops null ticker; dropna drops NaT
    assert s['ticker_count'] == 2 and s['min_date'] == '2020-01-01' and s['max_date'] == '2020-03-01'


def test_empty_parquet(tmp_path):
    df = pd.DataFrame({'ticker': pd.Series([], dtype='object'), 'date': pd.Series([], dtype='datetime64[ns]')})
    p = _write(tmp_path, 'empty', df)
    s = _stats(p)
    assert s == {'min_date': None, 'max_date': None, 'row_count': 0, 'ticker_count': 0}
    assert _eq(s, _ref_full(p))


def test_no_date_no_ticker(tmp_path):
    df = pd.DataFrame({'value': [1, 2, 3], 'other': [4, 5, 6]})
    p = _write(tmp_path, 'plain', df)
    s = _stats(p)
    assert _eq(s, _ref_full(p))
    assert s == {'min_date': None, 'max_date': None, 'row_count': 3, 'ticker_count': 0}


def test_string_dates(tmp_path):
    """date stored as strings (pd.to_datetime coercion path) must still parse identically."""
    df = pd.DataFrame({'ticker': ['A', 'B'], 'date': ['2020-05-01', '2020-05-02']})
    p = _write(tmp_path, 'strdate', df)
    s = _stats(p)
    assert _eq(s, _ref_full(p))
    assert s['min_date'] == '2020-05-01' and s['max_date'] == '2020-05-02'


def test_missing_file_errors_gracefully(tmp_path):
    s = _stats(tmp_path / 'does_not_exist.parquet')
    assert s['row_count'] == 0 and s['ticker_count'] == 0 and 'error' in s
