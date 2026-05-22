"""Unit tests for scripts/build_backfill_universe.py.

All tests use monkeypatch + tmp_path; no live DB or master parquet touched.
Pattern mirrors tests/test_backfill_regime_backtests.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import build_backfill_universe as bbu


def _write_prices_parquet(path: Path, rows: list[dict]) -> None:
    """Write a small parquet matching master/prices.parquet's schema (subset)."""
    df = pd.DataFrame(rows)
    # Match the master schema: ticker/date as strings; volume int64; close double.
    table = pa.Table.from_pandas(df[['ticker', 'date', 'close', 'volume']],
                                 preserve_index=False)
    pq.write_table(table, path)


def _fake_pg(active_tickers: list[str]):
    """Build a MagicMock conn whose cursor().fetchall returns single-col rows."""
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = [(t,) for t in active_tickers]
    conn.cursor.return_value.__enter__.return_value = cur
    return conn


@pytest.fixture
def fixture_prices(tmp_path: Path) -> Path:
    """Five tickers spanning the last ~100 days. ADV ranking:
        AAA (highest dollar vol) > BBB > CCC > DDD > EEE.
    """
    rows = []
    base_date = pd.Timestamp('2026-05-01')
    # Rank by dollar vol: AAA highest, EEE lowest.
    spec = [
        ('AAA', 200.0, 5_000_000),
        ('BBB', 100.0, 4_000_000),
        ('CCC', 50.0, 3_000_000),
        ('DDD', 25.0, 2_000_000),
        ('EEE', 10.0, 1_000_000),
    ]
    # Spread over 100 calendar days so the 90-day cutoff includes most rows.
    for ticker, close, volume in spec:
        for offset in range(100):
            d = (base_date - pd.Timedelta(days=offset)).strftime('%Y-%m-%d')
            rows.append({'ticker': ticker, 'date': d, 'close': close, 'volume': volume})
    path = tmp_path / 'prices.parquet'
    _write_prices_parquet(path, rows)
    return path


def test_writes_ranked_top_n(monkeypatch, tmp_path, fixture_prices):
    out_path = tmp_path / '.backfill_universe_v1.txt'
    monkeypatch.setattr(bbu, 'MASTER_PRICES', fixture_prices)
    monkeypatch.setattr(bbu, 'OUT', out_path)
    monkeypatch.setenv('POSTGRES_URI', 'postgres://fake/test')
    # All 5 fixture tickers are active+tradable.
    monkeypatch.setattr(bbu.psycopg2, 'connect',
                        lambda *a, **kw: _fake_pg(['AAA', 'BBB', 'CCC', 'DDD', 'EEE']))

    rc = bbu.main(top_n=5)
    assert rc == 0
    assert out_path.exists()
    lines = out_path.read_text().strip().split('\n')
    assert lines == ['AAA', 'BBB', 'CCC', 'DDD', 'EEE'], (
        f'expected ADV-ranked descending order, got {lines}'
    )


def test_intersection_drops_inactive_tickers(monkeypatch, tmp_path, fixture_prices):
    """Tickers ranked highest by ADV but absent from active set must be dropped."""
    out_path = tmp_path / '.backfill_universe_v1.txt'
    monkeypatch.setattr(bbu, 'MASTER_PRICES', fixture_prices)
    monkeypatch.setattr(bbu, 'OUT', out_path)
    monkeypatch.setenv('POSTGRES_URI', 'postgres://fake/test')
    # AAA + BBB (the top two by ADV) are NOT in the active set.
    monkeypatch.setattr(bbu.psycopg2, 'connect',
                        lambda *a, **kw: _fake_pg(['CCC', 'DDD', 'EEE']))

    rc = bbu.main(top_n=5)
    assert rc == 0
    lines = out_path.read_text().strip().split('\n')
    assert lines == ['CCC', 'DDD', 'EEE'], (
        f'AAA and BBB must be dropped (not active+tradable); got {lines}'
    )


def test_top_n_truncates(monkeypatch, tmp_path, fixture_prices):
    out_path = tmp_path / '.backfill_universe_v1.txt'
    monkeypatch.setattr(bbu, 'MASTER_PRICES', fixture_prices)
    monkeypatch.setattr(bbu, 'OUT', out_path)
    monkeypatch.setenv('POSTGRES_URI', 'postgres://fake/test')
    monkeypatch.setattr(bbu.psycopg2, 'connect',
                        lambda *a, **kw: _fake_pg(['AAA', 'BBB', 'CCC', 'DDD', 'EEE']))

    rc = bbu.main(top_n=2)
    assert rc == 0
    lines = out_path.read_text().strip().split('\n')
    assert lines == ['AAA', 'BBB'], f'top_n=2 should truncate to top 2; got {lines}'


def test_missing_prices_parquet_raises_informative_error(monkeypatch, tmp_path):
    """Operator-friendly: missing master parquet must raise (not silently
    write an empty file)."""
    missing = tmp_path / 'does_not_exist.parquet'
    out_path = tmp_path / '.backfill_universe_v1.txt'
    monkeypatch.setattr(bbu, 'MASTER_PRICES', missing)
    monkeypatch.setattr(bbu, 'OUT', out_path)
    monkeypatch.setenv('POSTGRES_URI', 'postgres://fake/test')
    monkeypatch.setattr(bbu.psycopg2, 'connect',
                        lambda *a, **kw: _fake_pg(['AAA']))

    with pytest.raises(FileNotFoundError) as exc_info:
        bbu.main(top_n=10)
    assert 'master prices parquet missing' in str(exc_info.value)
    assert str(missing) in str(exc_info.value)
    assert not out_path.exists(), 'no output file should be written on error'


def test_empty_active_universe_returns_nonzero(monkeypatch, tmp_path, fixture_prices, capsys):
    """Empty alpaca_tradable_universe -> nonzero exit, no output, no crash."""
    out_path = tmp_path / '.backfill_universe_v1.txt'
    monkeypatch.setattr(bbu, 'MASTER_PRICES', fixture_prices)
    monkeypatch.setattr(bbu, 'OUT', out_path)
    monkeypatch.setenv('POSTGRES_URI', 'postgres://fake/test')
    monkeypatch.setattr(bbu.psycopg2, 'connect', lambda *a, **kw: _fake_pg([]))

    rc = bbu.main(top_n=10)
    assert rc != 0
    err = capsys.readouterr().err
    assert '0 active+tradable' in err
    assert not out_path.exists()


def test_missing_postgres_uri_returns_nonzero(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv('POSTGRES_URI', raising=False)
    monkeypatch.setattr(bbu, 'OUT', tmp_path / '.backfill_universe_v1.txt')
    rc = bbu.main(top_n=10)
    assert rc == 2
    err = capsys.readouterr().err
    assert 'POSTGRES_URI' in err


def test_zero_volume_rows_handled(monkeypatch, tmp_path):
    """All-zero-volume tickers should rank last (mean dollar_vol == 0) but the
    script must not crash or write garbage."""
    rows = []
    base_date = pd.Timestamp('2026-05-01')
    for ticker, close, volume in [
        ('REAL', 100.0, 1_000_000),
        ('ZERO', 50.0, 0),
    ]:
        for offset in range(100):
            d = (base_date - pd.Timedelta(days=offset)).strftime('%Y-%m-%d')
            rows.append({'ticker': ticker, 'date': d, 'close': close, 'volume': volume})
    parquet_path = tmp_path / 'prices.parquet'
    _write_prices_parquet(parquet_path, rows)

    out_path = tmp_path / '.backfill_universe_v1.txt'
    monkeypatch.setattr(bbu, 'MASTER_PRICES', parquet_path)
    monkeypatch.setattr(bbu, 'OUT', out_path)
    monkeypatch.setenv('POSTGRES_URI', 'postgres://fake/test')
    monkeypatch.setattr(bbu.psycopg2, 'connect',
                        lambda *a, **kw: _fake_pg(['REAL', 'ZERO']))

    rc = bbu.main(top_n=5)
    assert rc == 0
    lines = out_path.read_text().strip().split('\n')
    # REAL > ZERO since ZERO has dollar_vol = 0 everywhere.
    assert lines == ['REAL', 'ZERO']
