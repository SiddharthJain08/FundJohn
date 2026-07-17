"""Tests for SP-2 Phase B Task 7 — --target prices implementation.

Splits into two layers:
  1. Pure unit tests on universe_prices.validate / sha256 — no IO at all.
  2. Integration tests on scripts/backfill_universe_5y.py::_run_prices —
     monkeypatches fetch_ticker_year + MASTER_PRICES + redis to a tmp_path
     fakedb. NEVER touches data/master/prices.parquet.

Integration tests skip cleanly if POSTGRES_URI or REDIS_URL are unset.
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path
from unittest import mock

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT))

from src.pipeline.backfillers import universe_prices as up
import backfill_universe_5y as bf  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────
def _make_valid_full_year_df(symbol: str = 'TEST', year: int = 2024, n: int = 252) -> pd.DataFrame:
    """Build a synthetic full-year price chunk."""
    dates = pd.bdate_range(f'{year}-01-02', f'{year}-12-30')[:n]
    return pd.DataFrame({
        'ticker': [symbol] * n,
        'date': [d.strftime('%Y-%m-%d') for d in dates],
        'open': [100.0 + i * 0.1 for i in range(n)],
        'high': [101.0 + i * 0.1 for i in range(n)],
        'low': [99.0 + i * 0.1 for i in range(n)],
        'close': [100.5 + i * 0.1 for i in range(n)],
        'volume': [1_000_000 + i * 1000 for i in range(n)],
        'vwap': [100.2 + i * 0.1 for i in range(n)],
        'transactions': [50_000 + i for i in range(n)],
        'source': [None] * n,
    })


def _make_valid_partial_year_df(symbol: str = 'IPO', year: int = 2024, n: int = 60) -> pd.DataFrame:
    """Listed mid-year (Q3 onwards)."""
    dates = pd.bdate_range(f'{year}-09-15', f'{year}-12-30')[:n]
    return pd.DataFrame({
        'ticker': [symbol] * n,
        'date': [d.strftime('%Y-%m-%d') for d in dates],
        'open': [50.0] * n,
        'high': [51.0] * n,
        'low': [49.0] * n,
        'close': [50.5] * n,
        'volume': [500_000] * n,
        'vwap': [50.2] * n,
        'transactions': [10_000] * n,
        'source': [None] * n,
    })


# ── Pure validate() tests ────────────────────────────────────────────────────
class TestValidate:
    def test_accepts_full_year_252(self):
        df = _make_valid_full_year_df()
        ok, err = up.validate(df, 'TEST', 2024)
        assert ok is True, err
        assert err is None

    def test_accepts_partial_year_60(self):
        df = _make_valid_partial_year_df()
        ok, err = up.validate(df, 'IPO', 2024)
        assert ok is True, err

    def test_rejects_empty(self):
        ok, err = up.validate(pd.DataFrame(), 'TEST', 2024)
        assert ok is False
        assert 'empty' in err.lower()

    def test_rejects_missing_column(self):
        df = _make_valid_full_year_df()
        df = df.drop(columns=['vwap'])
        ok, err = up.validate(df, 'TEST', 2024)
        assert ok is False
        assert 'missing columns' in err
        assert 'vwap' in err

    def test_rejects_null_close(self):
        df = _make_valid_full_year_df()
        df.loc[5, 'close'] = None
        ok, err = up.validate(df, 'TEST', 2024)
        assert ok is False
        assert 'close' in err

    def test_rejects_null_ticker(self):
        df = _make_valid_full_year_df()
        df.loc[5, 'ticker'] = None
        ok, err = up.validate(df, 'TEST', 2024)
        assert ok is False
        assert 'ticker' in err

    def test_rejects_date_out_of_range(self):
        df = _make_valid_full_year_df()
        df.loc[0, 'date'] = '2023-12-30'
        ok, err = up.validate(df, 'TEST', 2024)
        assert ok is False
        assert 'out of' in err

    def test_rejects_low_row_count_full_year(self):
        # Hand-build 50 rows spread across the year so partial-year detection
        # (min > Mar 1 OR max < Oct 1) doesn't fire. With min_d=2024-01-02 and
        # max_d=2024-12-30, this is correctly classified as full-year.
        dates = pd.bdate_range('2024-01-02', '2024-12-30')
        sampled = dates[:: len(dates) // 50][:50]
        df = pd.DataFrame({
            'ticker': ['TEST'] * len(sampled),
            'date': [d.strftime('%Y-%m-%d') for d in sampled],
            'open': [100.0] * len(sampled),
            'high': [101.0] * len(sampled),
            'low': [99.0] * len(sampled),
            'close': [100.5] * len(sampled),
            'volume': [1_000_000] * len(sampled),
            'vwap': [100.2] * len(sampled),
            'transactions': [50_000] * len(sampled),
            'source': [None] * len(sampled),
        })
        ok, err = up.validate(df, 'TEST', 2024)
        assert ok is False
        assert 'row count' in err

    def test_rejects_low_row_count_partial_year(self):
        df = _make_valid_partial_year_df(n=20)
        ok, err = up.validate(df, 'IPO', 2024)
        assert ok is False
        assert 'row count' in err


# ── Pure sha256() tests ──────────────────────────────────────────────────────
class TestSha256:
    def test_deterministic_across_reads(self):
        df1 = _make_valid_full_year_df()
        df2 = _make_valid_full_year_df()
        assert up.sha256(df1) == up.sha256(df2)

    def test_row_order_independent(self):
        df1 = _make_valid_full_year_df()
        df2 = df1.sample(frac=1, random_state=42).reset_index(drop=True)
        assert up.sha256(df1) == up.sha256(df2)

    def test_different_content_different_hash(self):
        df1 = _make_valid_full_year_df()
        df2 = _make_valid_full_year_df()
        df2.loc[10, 'close'] = 999.99
        assert up.sha256(df1) != up.sha256(df2)

    def test_empty_dataframe_hash_stable(self):
        h1 = up.sha256(pd.DataFrame())
        h2 = up.sha256(pd.DataFrame())
        assert h1 == h2
        assert len(h1) == 64


# ── Integration tests (require live PG + Redis) ──────────────────────────────
@pytest.fixture
def pg_conn():
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        pytest.skip('POSTGRES_URI not set')
    import psycopg2
    conn = psycopg2.connect(uri)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def redis_clean():
    """Yields a redis client and cleans up any backfill:5y:prices:TEST* keys after the test."""
    if not os.environ.get('REDIS_URL'):
        # Try default localhost
        os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6379/0')
    try:
        client = bf._redis()
    except Exception as e:
        pytest.skip(f'redis unavailable: {e}')
    # Uppercase tid because _load_universe() upper-cases all tickers; we want
    # the test's ticker string to match what lands in audit / data_quarantine.
    test_id = uuid.uuid4().hex[:8].upper()
    try:
        yield client, test_id
    finally:
        for prefix in ('T', 'Q', 'O'):
            for key in client.scan_iter(
                f'backfill:5y:prices:{prefix}{test_id}*'
            ):
                client.delete(key)


def _cleanup_audit_pg(conn, ticker_prefix: str):
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM backfill_audit WHERE chunk_key LIKE %s",
            (f'{ticker_prefix}%',),
        )
        cur.execute(
            "DELETE FROM data_quarantine WHERE symbol LIKE %s",
            (f'{ticker_prefix}%',),
        )
    conn.commit()


def _build_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        target='prices',
        resume=False,
        dry_run=False,
        tickers=None,
        years=None,
        source_tag=bf.CANONICAL_SOURCE_TAG,
        supersede_quarantine=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_dry_run_writes_audit_no_master(pg_conn, redis_clean, tmp_path, monkeypatch):
    client, tid = redis_clean
    ticker = f'T{tid}'
    fake_master = tmp_path / 'prices.parquet'
    monkeypatch.setattr(bf, 'MASTER_PRICES', fake_master)
    monkeypatch.setattr(
        'src.pipeline.backfillers.universe_prices.fetch_ticker_year',
        lambda symbol, year: _make_valid_full_year_df(symbol=symbol, year=year),
    )

    args = _build_args(tickers=ticker, years='2024', dry_run=True)
    try:
        bf._run_prices(args, pg_conn)

        # Master parquet must NOT exist
        assert not fake_master.exists()

        # Audit row should exist with status='validated'
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT status, rows_written FROM backfill_audit "
                "WHERE chunk_key = %s ORDER BY id DESC LIMIT 1",
                (f'{ticker}:2024',),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 'validated'
        assert row[1] == 252

        # Redis should NOT be set (dry-run doesn't promote)
        assert client.get(f'backfill:5y:prices:{ticker}:2024') is None
    finally:
        _cleanup_audit_pg(pg_conn, ticker)


def test_promote_then_idempotent(pg_conn, redis_clean, tmp_path, monkeypatch):
    client, tid = redis_clean
    ticker = f'T{tid}'
    fake_master = tmp_path / 'prices.parquet'
    monkeypatch.setattr(bf, 'MASTER_PRICES', fake_master)
    monkeypatch.setattr(
        'src.pipeline.backfillers.universe_prices.fetch_ticker_year',
        lambda symbol, year: _make_valid_full_year_df(symbol=symbol, year=year),
    )

    args = _build_args(tickers=ticker, years='2024', dry_run=False)
    try:
        # First run: promote
        bf._run_prices(args, pg_conn)
        assert fake_master.exists()
        df1 = pd.read_parquet(fake_master)
        assert len(df1) == 252
        assert (df1['source'] == bf.CANONICAL_SOURCE_TAG).all()
        assert client.get(f'backfill:5y:prices:{ticker}:2024') == 'promoted'

        # Second run with --resume: skip via Redis (still no double-write)
        args_resume = _build_args(tickers=ticker, years='2024', resume=True)
        bf._run_prices(args_resume, pg_conn)
        df2 = pd.read_parquet(fake_master)
        assert len(df2) == 252  # no new rows

        # Third run without --resume: hits overlap check, but all rows in master,
        # so df_new is empty → marked promoted, zero rows written.
        bf._run_prices(args, pg_conn)
        df3 = pd.read_parquet(fake_master)
        assert len(df3) == 252  # still 252, no double-write
    finally:
        _cleanup_audit_pg(pg_conn, ticker)


def test_bad_data_quarantines(pg_conn, redis_clean, tmp_path, monkeypatch):
    client, tid = redis_clean
    ticker = f'Q{tid}'
    fake_master = tmp_path / 'prices.parquet'
    monkeypatch.setattr(bf, 'MASTER_PRICES', fake_master)

    # Fake fetch returns a DataFrame with null closes (validation fails)
    def bad_fetch(symbol, year):
        df = _make_valid_full_year_df(symbol=symbol, year=year)
        df.loc[10:20, 'close'] = None
        return df

    monkeypatch.setattr(
        'src.pipeline.backfillers.universe_prices.fetch_ticker_year',
        bad_fetch,
    )

    args = _build_args(tickers=ticker, years='2024', dry_run=False)
    try:
        bf._run_prices(args, pg_conn)

        # Master parquet must NOT exist
        assert not fake_master.exists()

        # Audit row should be quarantined
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT status, error_text FROM backfill_audit "
                "WHERE chunk_key = %s ORDER BY id DESC LIMIT 1",
                (f'{ticker}:2024',),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 'quarantined'
        assert 'close' in (row[1] or '')

        # data_quarantine row
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, reason FROM data_quarantine "
                "WHERE symbol = %s ORDER BY id DESC LIMIT 1",
                (ticker,),
            )
            qrow = cur.fetchone()
        assert qrow is not None
        assert qrow[0] == ticker

        # Redis status
        assert client.get(f'backfill:5y:prices:{ticker}:2024') == 'quarantined'
    finally:
        _cleanup_audit_pg(pg_conn, ticker)


def test_overlap_refused_v1(pg_conn, redis_clean, tmp_path, monkeypatch):
    client, tid = redis_clean
    ticker = f'O{tid}'
    fake_master = tmp_path / 'prices.parquet'
    # Pre-seed master with 5 rows that overlap our 2024 staged data
    seed = _make_valid_full_year_df(symbol=ticker, year=2024).iloc[:5].copy()
    seed['source'] = 'pre_existing'
    fake_master.parent.mkdir(parents=True, exist_ok=True)
    seed.to_parquet(fake_master, index=False)

    monkeypatch.setattr(bf, 'MASTER_PRICES', fake_master)
    monkeypatch.setattr(
        'src.pipeline.backfillers.universe_prices.fetch_ticker_year',
        lambda symbol, year: _make_valid_full_year_df(symbol=symbol, year=year),
    )

    args = _build_args(tickers=ticker, years='2024', dry_run=False)
    try:
        # Ensure env gate is OFF
        monkeypatch.delenv('OPENCLAW_BACKFILL_ALLOW_OVERWRITE', raising=False)
        bf._run_prices(args, pg_conn)

        # Master parquet should be unchanged (still 5 pre-existing rows)
        df = pd.read_parquet(fake_master)
        assert len(df) == 5
        assert (df['source'] == 'pre_existing').all()

        # Audit row quarantined
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM backfill_audit "
                "WHERE chunk_key = %s ORDER BY id DESC LIMIT 1",
                (f'{ticker}:2024',),
            )
            row = cur.fetchone()
        assert row[0] == 'quarantined'
        assert client.get(f'backfill:5y:prices:{ticker}:2024') == 'quarantined'

        # Now run with v2 source_tag + env gate → succeeds, overwrites
        monkeypatch.setenv('OPENCLAW_BACKFILL_ALLOW_OVERWRITE', '1')
        args_v2 = _build_args(
            tickers=ticker, years='2024',
            source_tag='backfill_5y_v2',
            supersede_quarantine=True,
        )
        bf._run_prices(args_v2, pg_conn)

        df_after = pd.read_parquet(fake_master)
        # All 252 rows present (5 overwritten + 247 new)
        assert len(df_after) == 252
        # The 5 overlapping rows should now have source=v2
        first5 = df_after.sort_values('date').head(5)
        assert (first5['source'] == 'backfill_5y_v2').all()
        assert client.get(f'backfill:5y:prices:{ticker}:2024') == 'promoted'
    finally:
        _cleanup_audit_pg(pg_conn, ticker)


class TestFetchEndCappedAtYesterday:
    """SP-7 hardening: daytime runs must never ingest today's in-progress bar."""

    def _captured_end(self, monkeypatch, year):
        captured = {}

        def fake_run(args, **kw):
            captured['end'] = args[args.index('--end') + 1]
            return mock.Mock(returncode=0, stdout='{"bars": []}', stderr='')

        monkeypatch.setattr(up.subprocess, 'run', fake_run)
        up.fetch_ticker_year('TEST', year)
        return captured.get('end')

    def test_current_year_end_is_yesterday(self, monkeypatch):
        from datetime import date, timedelta
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        assert self._captured_end(monkeypatch, date.today().year) == yesterday

    def test_past_year_end_unchanged(self, monkeypatch):
        assert self._captured_end(monkeypatch, 2021) == '2021-12-31'

    def test_future_chunk_returns_empty_without_fetch(self, monkeypatch):
        from datetime import date
        called = []
        monkeypatch.setattr(up.subprocess, 'run',
                            lambda *a, **k: called.append(1))
        df = up.fetch_ticker_year('TEST', date.today().year + 1)
        assert df.empty and not called


class TestApiSymbolDashToDot:
    """SP-7 hardening: dash-form class/preferred symbols must hit the API dot-form."""

    def test_dash_symbol_translated_for_api_but_ticker_kept(self, monkeypatch):
        captured = {}

        def fake_run(args, **kw):
            captured['symbol'] = args[args.index('--symbol') + 1]
            return mock.Mock(returncode=0, stdout=(
                '{"bars": [{"t": "2021-01-04T05:00:00Z", "o": 1, "h": 1,'
                ' "l": 1, "c": 1, "v": 10, "vw": 1, "n": 1}],'
                ' "next_page_token": null}'), stderr='')

        monkeypatch.setattr(up.subprocess, 'run', fake_run)
        df = up.fetch_ticker_year('AGM-PRI', 2021)
        assert captured['symbol'] == 'AGM.PRI'      # API gets dot form
        assert df.iloc[0]['ticker'] == 'AGM-PRI'    # parquet keeps dash form

    def test_plain_symbol_unchanged(self, monkeypatch):
        captured = {}

        def fake_run(args, **kw):
            captured['symbol'] = args[args.index('--symbol') + 1]
            return mock.Mock(returncode=0, stdout='{"bars": []}', stderr='')

        monkeypatch.setattr(up.subprocess, 'run', fake_run)
        up.fetch_ticker_year('DELL', 2021)
        assert captured['symbol'] == 'DELL'
