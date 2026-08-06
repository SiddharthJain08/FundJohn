"""ingest_earnings_master merge semantics — no network, tmp parquets.

Guards the §3 remediation (2026-08-06 spec): earnings.parquet froze at
2026-04-30 because its refresh was orphaned dead code. The new ingester must
(a) append upcoming events from the fresh earnings_calendar, (b) fill
reported EPS in place with ±1d tolerance, and (c) NEVER shrink the master
(append-only contract, CLAUDE.md).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from src.ingestion import ingest_earnings_master as iem  # noqa: E402

TODAY = date(2026, 8, 6)


def _master_df():
    return pd.DataFrame({
        'ticker':            ['AAPL', 'AAPL', 'ZTS'],
        'date':              pd.to_datetime(['2026-04-30', '2026-08-04',
                                             '2025-12-31']),
        'eps_actual':        [2.01, float('nan'), 1.48],
        'eps_estimated':     [1.94, 1.89, 1.40],
        'revenue_actual':    [float('nan')] * 3,
        'revenue_estimated': [float('nan')] * 3,
        'last_updated':      ['2026-04-30'] * 3,
    })


def _calendar_df():
    return pd.DataFrame({
        'ticker':             ['AAPL', 'MSFT'],
        'next_earnings_date': pd.to_datetime(['2026-10-29', '2026-10-27']),
        'time':               ['', ''],
        'eps_estimate':       [1.98, 3.20],
        'revenue_estimate':   [1.2e11, 6.9e10],
        'fiscal_period':      [None, None],
    })


@pytest.fixture
def paths(tmp_path, monkeypatch):
    m = tmp_path / 'earnings.parquet'
    c = tmp_path / 'earnings_calendar.parquet'
    monkeypatch.setattr(iem, 'MASTER_PATH', m)
    monkeypatch.setattr(iem, 'CALENDAR_PATH', c)
    _master_df().to_parquet(m, index=False)
    _calendar_df().to_parquet(c, index=False)
    return m, c


def test_forward_rows_appends_only_unseen(paths):
    master = iem._load_master()
    fwd = iem.forward_rows(master, TODAY)
    assert set(zip(fwd['ticker'], fwd['date'].dt.date)) == {
        ('AAPL', date(2026, 10, 29)), ('MSFT', date(2026, 10, 27))}
    assert fwd['eps_actual'].isna().all()
    assert (fwd['last_updated'] == TODAY.isoformat()).all()
    assert list(fwd.columns) == iem.MASTER_COLUMNS
    # Re-running after the merge adds nothing (idempotent).
    merged = pd.concat([master, fwd], ignore_index=True)
    assert iem.forward_rows(merged, TODAY).empty


def test_tickers_needing_actuals_window(paths):
    master = iem._load_master()
    # AAPL 2026-08-04 is unreported and inside the 14d lookback.
    assert iem.tickers_needing_actuals(master, TODAY) == ['AAPL']


def test_apply_actuals_fills_in_place_with_day_skew(paths):
    master = iem._load_master()
    n0 = len(master)
    actuals = pd.DataFrame({
        'ticker':        ['AAPL', 'NVDA'],
        # AAPL reported 08-05 in yfinance vs master's forward row 08-04 (amc
        # skew) — must fill the existing row, not append a near-duplicate.
        'date':          [date(2026, 8, 5), date(2026, 5, 28)],
        'eps_estimated': [1.89, 0.85],
        'eps_actual':    [2.02, 0.91],
    })
    out, filled, appended = iem.apply_actuals(master, actuals, TODAY)
    assert (filled, appended) == (1, 1)
    assert len(out) == n0 + 1
    aapl = out[(out['ticker'] == 'AAPL')
               & (out['date'] == pd.Timestamp('2026-08-04'))].iloc[0]
    assert aapl['eps_actual'] == 2.02
    assert aapl['last_updated'] == TODAY.isoformat()
    # Existing reported rows are untouched.
    apr = out[(out['ticker'] == 'AAPL')
              & (out['date'] == pd.Timestamp('2026-04-30'))].iloc[0]
    assert apr['eps_actual'] == 2.01 and apr['last_updated'] == '2026-04-30'


def test_main_daily_appends_and_writes(paths, monkeypatch):
    m, _ = paths
    monkeypatch.setattr(iem, '_fetch_actuals',
                        lambda tickers, throttle_s: pd.DataFrame({
                            'ticker': ['AAPL'], 'date': [date(2026, 8, 4)],
                            'eps_estimated': [1.89], 'eps_actual': [2.02]}))
    rc = iem.main([])
    assert rc == 0
    out = pd.read_parquet(m)
    assert len(out) == 5  # 3 original + 2 forward
    assert out[(out['ticker'] == 'AAPL')
               & (out['date'] == pd.Timestamp('2026-08-04'))]['eps_actual'].iloc[0] == 2.02


def test_main_dry_run_writes_nothing(paths, monkeypatch):
    m, _ = paths
    before = m.read_bytes()
    called = []
    monkeypatch.setattr(iem, '_fetch_actuals',
                        lambda *a, **k: called.append(1))
    rc = iem.main(['--dry-run'])
    assert rc == 0
    assert m.read_bytes() == before, '--dry-run must not rewrite the master'
    assert not called, '--dry-run must not hit the network'


def test_shrink_aborts(paths, monkeypatch):
    """A pathological merge that loses rows must refuse to write."""
    m, _ = paths
    before = m.read_bytes()
    monkeypatch.setattr(iem, 'apply_actuals',
                        lambda master, actuals, today: (master.iloc[:1], 0, 0))
    monkeypatch.setattr(iem, '_fetch_actuals',
                        lambda *a, **k: pd.DataFrame(
                            {'ticker': ['AAPL'], 'date': [date(2026, 8, 4)],
                             'eps_estimated': [1.0], 'eps_actual': [1.0]}))
    monkeypatch.setattr(iem, 'forward_rows',
                        lambda master, today: pd.DataFrame(
                            columns=iem.MASTER_COLUMNS))
    rc = iem.main([])
    assert rc == 1
    assert m.read_bytes() == before, 'shrinking merge must not be written'
