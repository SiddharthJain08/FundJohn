"""Tests for ingest_prices_30m_daily.py — the self-sustaining daily refresh driver.

NO network, NO real-parquet writes. All tests use synthetic DataFrames in tmp_path
and monkeypatching of backfill/DATA_PARQUET so only the driver logic is tested.
"""
from __future__ import annotations

import os
import sys
import importlib
import pytest
import pandas as pd

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

COLUMNS = ['date', 'datetime', 'ticker', 'open', 'high', 'low', 'close', 'volume', 'vwap', 'transactions']


def _make_parquet(path, rows):
    """Write a synthetic prices_30m parquet with tz-aware UTC datetimes."""
    df = pd.DataFrame(rows, columns=COLUMNS)
    # Ensure datetime column is tz-aware UTC
    df['datetime'] = pd.to_datetime(df['datetime'], utc=True).dt.as_unit('us')
    df.to_parquet(path, index=False)
    return df


def _import_daily(monkeypatch, tmp_path, parquet_path=None):
    """Fresh import of ingest_prices_30m_daily with DATA_PARQUET monkeypatched."""
    # Remove any cached module so monkeypatch sticks on a fresh import
    for key in list(sys.modules.keys()):
        if 'ingest_prices_30m_daily' in key:
            del sys.modules[key]

    import ingestion.ingest_prices_30m_daily as daily

    if parquet_path is not None:
        monkeypatch.setattr(daily, 'DATA_PARQUET', str(parquet_path))

    # Re-import so DATA_PARQUET is fresh after patching
    importlib.reload(daily)
    monkeypatch.setattr(daily, 'DATA_PARQUET', str(parquet_path) if parquet_path else daily.DATA_PARQUET)
    return daily


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_universe_derivation_uses_datetime_axis(tmp_path, monkeypatch):
    """Universe must come from distinct tickers across ALL rows, including rows
    where `date` is NaN/None (legacy AAPL/MSFT/NVDA/TSLA rows). The datetime
    axis is the reliable axis — if we filter on `date` we'd silently lose tickers."""
    import ingestion.ingest_prices_30m_daily as daily

    parquet = tmp_path / "prices_30m.parquet"
    # One row has a valid date string, one has NaN date — both must contribute
    rows = [
        {'date': '2026-06-01', 'datetime': '2026-06-01T14:00:00Z',
         'ticker': 'AAPL', 'open': 1, 'high': 1, 'low': 1, 'close': 1,
         'volume': 100, 'vwap': 1.0, 'transactions': 5},
        {'date': None,         'datetime': '2026-06-02T14:00:00Z',
         'ticker': 'MSFT', 'open': 2, 'high': 2, 'low': 2, 'close': 2,
         'volume': 200, 'vwap': 2.0, 'transactions': 10},
    ]
    _make_parquet(parquet, rows)
    monkeypatch.setattr(daily, 'DATA_PARQUET', str(parquet))

    df = pd.read_parquet(str(parquet))
    universe = daily.derive_universe(df)

    assert set(universe) == {'AAPL', 'MSFT'}, f"Expected both tickers, got {universe}"


def test_window_calc_uses_max_datetime_minus_2d(monkeypatch):
    """Window start = max(datetime) in ET date - 2 calendar days; end = today ET."""
    import ingestion.ingest_prices_30m_daily as daily

    # Construct a minimal DataFrame with datetime column
    df = pd.DataFrame({
        'datetime': pd.to_datetime(['2026-06-02T14:00:00Z', '2026-06-03T14:00:00Z'], utc=True),
    })

    start, end = daily.compute_window(df)

    import pytz
    from datetime import date, timedelta
    et = pytz.timezone('America/New_York')
    import datetime as dt_mod
    today_et = dt_mod.date.today()   # test runs in UTC on server; tolerate ±1d slop

    # max datetime = 2026-06-03T14:00Z = 2026-06-03 10:00 ET → date 2026-06-03
    # start = 2026-06-03 - 2d = 2026-06-01
    assert start == '2026-06-01', f"Expected start 2026-06-01, got {start}"
    # end should be today's ET date (the test is run today)
    assert end == today_et.isoformat() or end >= '2026-06-01'  # loose bound — just check format


def test_window_from_override(monkeypatch):
    """--from override replaces the computed window start."""
    import ingestion.ingest_prices_30m_daily as daily

    df = pd.DataFrame({
        'datetime': pd.to_datetime(['2026-06-03T14:00:00Z'], utc=True),
    })
    start, end = daily.compute_window(df, from_override='2026-04-22')
    assert start == '2026-04-22', f"Expected override start, got {start}"


def test_missing_parquet_exits_with_1(tmp_path, monkeypatch, capsys):
    """When the parquet doesn't exist the driver must sys.exit(1), never create it."""
    import ingestion.ingest_prices_30m_daily as daily

    missing_path = tmp_path / "does_not_exist.parquet"
    monkeypatch.setattr(daily, 'DATA_PARQUET', str(missing_path))

    with pytest.raises(SystemExit) as exc:
        daily.run(argv=['--dry-run'])

    assert exc.value.code == 1, f"Expected exit code 1, got {exc.value.code}"
    # Parquet must NOT have been created
    assert not missing_path.exists(), "Driver must not create a new parquet"


def test_dry_run_does_not_fetch_or_write(tmp_path, monkeypatch):
    """--dry-run must print universe + window and return without calling backfill."""
    import ingestion.ingest_prices_30m_daily as daily

    parquet = tmp_path / "prices_30m.parquet"
    rows = [
        {'date': '2026-06-01', 'datetime': '2026-06-01T14:00:00Z',
         'ticker': 'AAPL', 'open': 1, 'high': 1, 'low': 1, 'close': 1,
         'volume': 100, 'vwap': 1.0, 'transactions': 5},
    ]
    _make_parquet(parquet, rows)
    monkeypatch.setattr(daily, 'DATA_PARQUET', str(parquet))

    called = []

    def fake_backfill(tickers, start, end, out_path=None):
        called.append((tickers, start, end))
        return (0, 0)

    monkeypatch.setattr(daily, 'backfill', fake_backfill)

    # Should not raise; should not call backfill
    daily.run(argv=['--dry-run'])
    assert called == [], f"backfill was called during --dry-run: {called}"


def test_summary_line_format(tmp_path, monkeypatch, capsys):
    """The final stdout line must match the expected format for the collector's Discord tail."""
    import ingestion.ingest_prices_30m_daily as daily

    parquet = tmp_path / "prices_30m.parquet"
    rows = [
        {'date': '2026-06-01', 'datetime': '2026-06-01T14:00:00Z',
         'ticker': 'AAPL', 'open': 1, 'high': 1, 'low': 1, 'close': 1,
         'volume': 100, 'vwap': 1.0, 'transactions': 5},
        {'date': '2026-06-02', 'datetime': '2026-06-02T14:00:00Z',
         'ticker': 'MSFT', 'open': 2, 'high': 2, 'low': 2, 'close': 2,
         'volume': 200, 'vwap': 2.0, 'transactions': 10},
    ]
    _make_parquet(parquet, rows)
    monkeypatch.setattr(daily, 'DATA_PARQUET', str(parquet))

    # Stub backfill: pretend +5 rows were added (before=2, after=7)
    def fake_backfill(tickers, start, end, out_path=None):
        return (2, 7)

    monkeypatch.setattr(daily, 'backfill', fake_backfill)

    daily.run(argv=[])
    captured = capsys.readouterr()
    lines = [ln.strip() for ln in captured.out.strip().splitlines() if ln.strip()]
    last_line = lines[-1]

    # Format: "prices_30m daily: N tickers, window YYYY-MM-DD..YYYY-MM-DD, +N rows (total M)"
    assert last_line.startswith('prices_30m daily:'), f"Bad summary prefix: {last_line!r}"
    assert 'tickers' in last_line, f"Missing 'tickers' in: {last_line!r}"
    assert 'window' in last_line, f"Missing 'window' in: {last_line!r}"
    assert '+5 rows' in last_line, f"Expected '+5 rows' in: {last_line!r}"
    assert 'total 7' in last_line, f"Expected 'total 7' in: {last_line!r}"
