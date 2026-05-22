"""Tests for SP-2 Phase B Task 9 — --target options driver mode.

Layers:
  1. Pure unit tests on universe_options.validate / enumerate / fetch helpers
     — no IO, monkeypatched alpaca CLI.
  2. Integration tests on scripts/backfill_universe_5y.py::_run_options —
     monkeypatches universe_options + MASTER_OPTIONS to a tmp_path fakedb.
     NEVER touches data/master/options_eod.parquet.
  3. Subprocess tests for the driver CLI (`--target options --dry-run`) and a
     CLI-surface guard for the cutover-gap one-shot.

The cutover-gap script (`scripts/backfill_options_eod_cutover_gap.py`) remains
self-contained (operator-preserved CLI). Tests just guard its surface against
drift; the shared logic lives in `universe_options.py` so future windows can
share it.

Integration tests skip cleanly if POSTGRES_URI or REDIS_URL are unset.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import uuid
from datetime import date as _date
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT))

from src.pipeline.backfillers import universe_options as uo
import backfill_universe_5y as bf  # noqa: E402

SCRIPT = ROOT / 'scripts' / 'backfill_universe_5y.py'
WRAPPER = ROOT / 'scripts' / 'backfill_options_eod_cutover_gap.py'


# ── Pure validate() tests ────────────────────────────────────────────────────
class TestValidate:
    def test_accepts_minimal_valid(self):
        d = _date(2026, 5, 12)
        df = pd.DataFrame([
            {'date': '2026-05-12', 'contract_symbol': 'SPY260619C00500000'},
            {'date': '2026-05-12', 'contract_symbol': 'SPY260619P00500000'},
        ])
        ok, err = uo.validate(df, d)
        assert ok is True, err
        assert err is None

    def test_rejects_none(self):
        ok, err = uo.validate(None, _date(2026, 5, 12))
        assert ok is False
        assert 'empty' in err.lower()

    def test_rejects_empty_df(self):
        ok, err = uo.validate(pd.DataFrame(), _date(2026, 5, 12))
        assert ok is False
        assert 'empty' in err.lower()

    def test_rejects_missing_contract_symbol(self):
        d = _date(2026, 5, 12)
        df = pd.DataFrame([{'date': '2026-05-12', 'close': 1.0}])
        ok, err = uo.validate(df, d)
        assert ok is False
        assert 'contract_symbol' in err

    def test_rejects_missing_date(self):
        d = _date(2026, 5, 12)
        df = pd.DataFrame([{'contract_symbol': 'SPY...', 'close': 1.0}])
        ok, err = uo.validate(df, d)
        assert ok is False
        assert 'date' in err

    def test_rejects_date_mismatch(self):
        d = _date(2026, 5, 12)
        df = pd.DataFrame([
            {'date': '2026-05-12', 'contract_symbol': 'A'},
            {'date': '2026-05-13', 'contract_symbol': 'B'},  # wrong date
        ])
        ok, err = uo.validate(df, d)
        assert ok is False
        assert 'date mismatch' in err


# ── enumerate_contracts_for_date — monkeypatched alpaca ──────────────────────
class TestEnumerate:
    def test_filters_to_expiry_geq_d(self, monkeypatch):
        fake_payload = {'snapshots': {
            # OCC: AAPL250101C00150000 → expiry 2025-01-01
            'AAPL250101C00150000': {},
            # AAPL260101C00150000 → expiry 2026-01-01
            'AAPL260101C00150000': {},
        }}
        monkeypatch.setattr(uo, '_alpaca', lambda args: fake_payload)
        d = _date(2025, 6, 1)
        out = uo.enumerate_contracts_for_date(d, ['AAPL'])
        # Only the 2026-01-01 contract (>=2025-06-01) survives.
        assert 'AAPL260101C00150000' in out
        assert 'AAPL250101C00150000' not in out

    def test_swallows_alpaca_errors(self, monkeypatch):
        def boom(args):
            raise RuntimeError('alpaca down')
        monkeypatch.setattr(uo, '_alpaca', boom)
        out = uo.enumerate_contracts_for_date(_date(2026, 5, 12), ['SPY', 'AAPL'])
        assert out == []


# ── fetch_eod_for_contracts — monkeypatched alpaca ───────────────────────────
class TestFetch:
    def test_batches_and_flattens(self, monkeypatch):
        calls = []

        def fake_alpaca(args):
            calls.append(args)
            # Return one bar per requested symbol
            symbols = args[args.index('--symbols') + 1].split(',')
            return {'bars': {
                s: [{'o': 1.0, 'h': 2.0, 'l': 0.5, 'c': 1.5,
                     'v': 100, 'vw': 1.25, 'n': 10}]
                for s in symbols
            }}

        monkeypatch.setattr(uo, '_alpaca', fake_alpaca)
        # 150 contracts → two batches (100 + 50)
        contracts = [f'SPY26061{i:02d}C00500000' for i in range(150)]
        d = _date(2026, 5, 12)
        df = uo.fetch_eod_for_contracts(contracts, d)
        assert len(df) == 150
        assert (df['date'] == '2026-05-12').all()
        assert (df['data_source'] == 'alpaca_aat_plus_backfill').all()
        assert len(calls) == 2

    def test_empty_returns_empty_df_with_required_cols(self, monkeypatch):
        monkeypatch.setattr(uo, '_alpaca', lambda args: {'bars': {}})
        df = uo.fetch_eod_for_contracts(['X'], _date(2026, 5, 12))
        assert df.empty
        for col in uo.REQUIRED_COLUMNS:
            assert col in df.columns


# ── Subprocess-level driver tests ────────────────────────────────────────────
def _run(*args, env_extra=None, script=SCRIPT, timeout=60):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def test_run_options_no_longer_notimpl():
    """Static check: _run_options must not be the Task-9 stub anymore."""
    import inspect
    src = inspect.getsource(bf._run_options)
    assert 'NotImplementedError' not in src
    # The implementation must reference universe_options.
    assert 'universe_options' in src


def test_cutover_wrapper_help_still_works():
    """--help must still display the original surface (--from-date / --to-date).

    The cutover-gap script remains self-contained (intentional — operator CLI
    already validated in production). This test guards against accidental CLI
    surface drift.
    """
    r = _run('--help', script=WRAPPER, timeout=10)
    assert r.returncode == 0
    assert '--from-date' in r.stdout
    assert '--to-date' in r.stdout


# ── Integration: _run_options against live PG + Redis ────────────────────────
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
    if not os.environ.get('REDIS_URL'):
        os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6379/0')
    try:
        client = bf._redis()
    except Exception as e:
        pytest.skip(f'redis unavailable: {e}')
    test_id = uuid.uuid4().hex[:8].upper()
    try:
        yield client, test_id
    finally:
        for key in client.scan_iter(f'backfill:5y:options:*{test_id}*'):
            client.delete(key)


def _cleanup_audit_pg(conn, chunk_substr: str):
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM backfill_audit WHERE chunk_key LIKE %s",
            (f'%{chunk_substr}%',),
        )
    conn.commit()


def _build_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        target='options',
        resume=False,
        dry_run=False,
        tickers=None,
        years=None,
        source_tag=bf.CANONICAL_SOURCE_TAG,
        supersede_quarantine=False,
        start_date=None,
        end_date=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_dry_run_writes_audit_no_master(pg_conn, redis_clean, tmp_path, monkeypatch):
    """Audit row written on dry-run; master parquet not touched."""
    client, tid = redis_clean
    ticker = f'T{tid}'
    fake_master = tmp_path / 'options_eod.parquet'
    monkeypatch.setattr(bf, 'MASTER_OPTIONS', fake_master)

    # Stub the universe_options helpers so we don't hit Alpaca.
    monkeypatch.setattr(
        'src.pipeline.backfillers.universe_options.enumerate_contracts_for_date',
        lambda d, universe: [f'{universe[0]}260619C00100000'],
    )
    monkeypatch.setattr(
        'src.pipeline.backfillers.universe_options.fetch_eod_for_contracts',
        lambda contracts, d: pd.DataFrame([
            {'date': d.isoformat(), 'contract_symbol': c, 'close': 1.0}
            for c in contracts
        ]),
    )

    args = _build_args(
        tickers=ticker,
        start_date='2026-05-12',
        end_date='2026-05-12',
        dry_run=True,
    )
    try:
        bf._run_options(args, pg_conn)

        # Master parquet must NOT exist
        assert not fake_master.exists()

        # Audit row: chunk_key = '2026-05-12:<ticker>', status validated
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT status, rows_written FROM backfill_audit "
                "WHERE chunk_key = %s ORDER BY id DESC LIMIT 1",
                (f'2026-05-12:{ticker}',),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 'validated'
        assert row[1] == 1

        # Redis NOT set on dry-run
        assert client.get(f'backfill:5y:options:2026-05-12:{ticker}') is None
    finally:
        _cleanup_audit_pg(pg_conn, ticker)


def test_validate_rejects_bad_schema(pg_conn, redis_clean, tmp_path, monkeypatch):
    """A chunk with missing required columns must be quarantined, not promoted."""
    client, tid = redis_clean
    ticker = f'Q{tid}'
    fake_master = tmp_path / 'options_eod.parquet'
    monkeypatch.setattr(bf, 'MASTER_OPTIONS', fake_master)

    monkeypatch.setattr(
        'src.pipeline.backfillers.universe_options.enumerate_contracts_for_date',
        lambda d, universe: ['BADC260619C00100000'],
    )
    # Return a DataFrame missing contract_symbol
    monkeypatch.setattr(
        'src.pipeline.backfillers.universe_options.fetch_eod_for_contracts',
        lambda contracts, d: pd.DataFrame([
            {'date': d.isoformat(), 'close': 1.0},
        ]),
    )

    args = _build_args(
        tickers=ticker,
        start_date='2026-05-12',
        end_date='2026-05-12',
        dry_run=False,
    )
    try:
        bf._run_options(args, pg_conn)
        assert not fake_master.exists()
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT status, error_text FROM backfill_audit "
                "WHERE chunk_key = %s ORDER BY id DESC LIMIT 1",
                (f'2026-05-12:{ticker}',),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 'quarantined'
        assert 'contract_symbol' in (row[1] or '')
        assert client.get(f'backfill:5y:options:2026-05-12:{ticker}') == 'quarantined'
    finally:
        _cleanup_audit_pg(pg_conn, ticker)


def test_promote_writes_master(pg_conn, redis_clean, tmp_path, monkeypatch):
    """Happy path: validated chunk writes rows to MASTER_OPTIONS via _append_parquet."""
    client, tid = redis_clean
    ticker = f'P{tid}'
    fake_master = tmp_path / 'options_eod.parquet'
    monkeypatch.setattr(bf, 'MASTER_OPTIONS', fake_master)

    monkeypatch.setattr(
        'src.pipeline.backfillers.universe_options.enumerate_contracts_for_date',
        lambda d, universe: [f'{universe[0]}260619C00100000',
                             f'{universe[0]}260619P00100000'],
    )
    monkeypatch.setattr(
        'src.pipeline.backfillers.universe_options.fetch_eod_for_contracts',
        lambda contracts, d: pd.DataFrame([
            {'date': d.isoformat(), 'contract_symbol': c,
             'close': 1.0, 'volume': 100}
            for c in contracts
        ]),
    )

    args = _build_args(
        tickers=ticker,
        start_date='2026-05-12',
        end_date='2026-05-12',
        dry_run=False,
    )
    try:
        bf._run_options(args, pg_conn)
        assert fake_master.exists()
        df = pd.read_parquet(fake_master)
        assert len(df) == 2
        assert set(df['contract_symbol']) == {
            f'{ticker}260619C00100000', f'{ticker}260619P00100000',
        }
        assert (df['date'] == '2026-05-12').all()
        assert client.get(f'backfill:5y:options:2026-05-12:{ticker}') == 'promoted'
    finally:
        _cleanup_audit_pg(pg_conn, ticker)


# ── Subprocess CLI smoke ─────────────────────────────────────────────────────
def test_driver_options_dry_run_exits_zero():
    """`backfill_universe_5y.py --target options --tickers SPY ... --dry-run`
    must exit 0. The dry-run path doesn't promote but it does hit Alpaca to
    enumerate the chain — skip if the binary or PG/Redis aren't around.
    """
    if not os.path.exists(os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')):
        pytest.skip('alpaca CLI not available')
    if not os.environ.get('POSTGRES_URI'):
        pytest.skip('POSTGRES_URI not set')
    if not os.environ.get('REDIS_URL'):
        os.environ['REDIS_URL'] = 'redis://127.0.0.1:6379/0'
    r = _run(
        '--target', 'options',
        '--tickers', 'SPY',
        '--start-date', '2026-05-12',  # business day (Tue)
        '--end-date', '2026-05-12',
        '--dry-run',
        timeout=120,
    )
    assert r.returncode == 0, (
        f'rc={r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}'
    )


def test_cutover_wrapper_cli_surface_preserved():
    """The cutover-gap script's original CLI shape (`--from-date`/`--to-date`,
    both required) must be intact — operators rely on it. Argparse will exit
    rc=2 with a 'required' error if either flag is missing.
    """
    r = _run(script=WRAPPER, timeout=10)  # no args
    assert r.returncode != 0
    combined = (r.stderr or '') + (r.stdout or '')
    assert '--from-date' in combined or 'from-date' in combined.lower()


def test_cutover_wrapper_dry_run_exits_zero():
    """`backfill_options_eod_cutover_gap.py --tickers SPY --from-date X --to-date Y --dry-run`
    must exit 0 by routing through the driver."""
    if not os.path.exists(os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')):
        pytest.skip('alpaca CLI not available')
    if not os.environ.get('POSTGRES_URI'):
        pytest.skip('POSTGRES_URI not set')
    if not os.environ.get('REDIS_URL'):
        os.environ['REDIS_URL'] = 'redis://127.0.0.1:6379/0'
    r = _run(
        '--tickers', 'SPY',
        '--from-date', '2026-05-12',
        '--to-date', '2026-05-12',
        '--dry-run',
        script=WRAPPER,
        timeout=120,
    )
    assert r.returncode == 0, (
        f'rc={r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}'
    )
