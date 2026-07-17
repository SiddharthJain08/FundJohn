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

ROOT = Path(__file__).resolve().parents[2]
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


def test_nan_close_or_volume_handled(monkeypatch, tmp_path):
    """A NaN close or NaN volume on some bars must not crash the ranking AND
    must not silently lower a ticker's rank below its true ADV. Tickers whose
    *every* row is NaN should be excluded entirely (groupby.mean of an empty
    group is dropped)."""
    rows = []
    base_date = pd.Timestamp('2026-05-01')
    # GOOD: clean 100M-dollar-per-bar.
    for offset in range(100):
        d = (base_date - pd.Timedelta(days=offset)).strftime('%Y-%m-%d')
        rows.append({'ticker': 'GOOD', 'date': d, 'close': 100.0, 'volume': 1_000_000})

    # MIXED: half the bars NaN, half clean at $50 * 2M = $100M -- once NaN
    # rows are dropped, MIXED's mean dollar_vol equals GOOD's. (If we DIDN'T
    # drop NaN, groupby.mean would still drop them anyway, but downstream
    # multiplication would have raised or silently propagated NaN.)
    for offset in range(100):
        d = (base_date - pd.Timedelta(days=offset)).strftime('%Y-%m-%d')
        if offset % 2 == 0:
            rows.append({'ticker': 'MIXED', 'date': d, 'close': float('nan'),
                         'volume': 2_000_000})
        else:
            rows.append({'ticker': 'MIXED', 'date': d, 'close': 50.0,
                         'volume': 2_000_000})

    # ALLNAN: every bar has NaN volume — must be excluded entirely, not crash.
    for offset in range(100):
        d = (base_date - pd.Timedelta(days=offset)).strftime('%Y-%m-%d')
        rows.append({'ticker': 'ALLNAN', 'date': d, 'close': 99.0,
                     'volume': float('nan')})

    parquet_path = tmp_path / 'prices.parquet'
    _write_prices_parquet(parquet_path, rows)

    out_path = tmp_path / '.backfill_universe_v1.txt'
    monkeypatch.setattr(bbu, 'MASTER_PRICES', parquet_path)
    monkeypatch.setattr(bbu, 'OUT', out_path)
    monkeypatch.setenv('POSTGRES_URI', 'postgres://fake/test')
    monkeypatch.setattr(bbu.psycopg2, 'connect',
                        lambda *a, **kw: _fake_pg(['GOOD', 'MIXED', 'ALLNAN']))

    rc = bbu.main(top_n=5)
    assert rc == 0
    lines = out_path.read_text().strip().split('\n')
    # ALLNAN must NOT appear -- every row was NaN, so it has no ADV.
    assert 'ALLNAN' not in lines, (
        f'ticker with 100% NaN volume must be excluded; got {lines}'
    )
    # GOOD and MIXED both end up at $100M / bar mean dollar_vol (MIXED's NaN
    # rows dropped). Either order is acceptable; both must be present.
    assert set(lines) == {'GOOD', 'MIXED'}, (
        f'GOOD and MIXED should both appear; got {lines}'
    )


def test_realistic_magnitude_ranking(monkeypatch, tmp_path):
    """Production-scale dollar volumes (100M shares * $1000 close = $1e11/bar)
    must rank correctly without int overflow, float32 truncation, or
    precision loss. AAPL_LIKE > MSFT_LIKE > SMALL by a wide margin."""
    rows = []
    base_date = pd.Timestamp('2026-05-01')
    # AAPL_LIKE: $1000 close, 100M shares = $1e11/bar.
    # MSFT_LIKE: $500 close, 50M shares = $2.5e10/bar.
    # SMALL: $10 close, 100K shares = $1e6/bar.
    spec = [
        ('AAPL_LIKE', 1000.0, 100_000_000),
        ('MSFT_LIKE', 500.0, 50_000_000),
        ('SMALL', 10.0, 100_000),
    ]
    for ticker, close, volume in spec:
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
                        lambda *a, **kw: _fake_pg(['AAPL_LIKE', 'MSFT_LIKE', 'SMALL']))

    rc = bbu.main(top_n=10)
    assert rc == 0
    lines = out_path.read_text().strip().split('\n')
    assert lines == ['AAPL_LIKE', 'MSFT_LIKE', 'SMALL'], (
        f'production-magnitude ADV ranking must hold; got {lines}'
    )

    # Spot-check the underlying values are exactly representable in float64.
    adv = bbu._rank_by_adv(parquet_path)
    assert adv['AAPL_LIKE'] == pytest.approx(1e11), (
        f'AAPL_LIKE mean dollar_vol should be exactly 1e11; got {adv["AAPL_LIKE"]}'
    )
    assert adv['MSFT_LIKE'] == pytest.approx(2.5e10)
    assert adv['SMALL'] == pytest.approx(1e6)
    # Strict ordering preserved at production scale.
    assert adv['AAPL_LIKE'] > adv['MSFT_LIKE'] > adv['SMALL']


def test_idempotency_guard_refuses_overwrite(monkeypatch, tmp_path, fixture_prices, capsys):
    """If output already exists and --force not passed, exit 2 with REFUSED
    message and leave the existing file untouched."""
    out_path = tmp_path / '.backfill_universe_v1.txt'
    out_path.write_text('PRE_EXISTING\n')
    monkeypatch.setattr(bbu, 'MASTER_PRICES', fixture_prices)
    monkeypatch.setattr(bbu, 'OUT', out_path)
    monkeypatch.setenv('POSTGRES_URI', 'postgres://fake/test')
    monkeypatch.setattr(bbu.psycopg2, 'connect',
                        lambda *a, **kw: _fake_pg(['AAA', 'BBB']))

    rc = bbu.main(top_n=5)
    assert rc == 2, 'idempotency guard must return exit-code 2'
    err = capsys.readouterr().err
    assert 'REFUSED' in err
    assert '--force' in err
    # Existing content untouched.
    assert out_path.read_text() == 'PRE_EXISTING\n'

    # And --force overrides.
    rc2 = bbu.main(top_n=5, force=True)
    assert rc2 == 0
    assert out_path.read_text().strip().split('\n') == ['AAA', 'BBB']
