"""master_freshness: shares_outstanding.parquet must be judged by fetched_at.

Live file 2026-08-23: max(asof_date) = 2034-03-05 (a mis-tagged XBRL cover
date), max(fetched_at) = 2026-06-04. Keyed on asof_date the 35d cadence can
NEVER fire — the store froze for 80 days while the check said PASS. The
EDGAR shares refresh appends rows stamped fetched_at on every run, so that
column is the honest 'last fed' signal.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd

from src.system_checks.checks import master_freshness as chk
from src.system_checks.types import Status


def _write_shares(path, fetched_days_ago: int):
    fetched = (datetime.now(timezone.utc) - timedelta(days=fetched_days_ago)).isoformat()
    pd.DataFrame({
        'ticker': ['AAPL', 'ZZZZ'],
        'asof_date': ['2026-06-30', '2034-03-05'],   # junk future date present
        'shares': [1.5e10, 2.0e7],
        'form': ['10-Q', '10-K'],
        'filed': ['2026-07-31', '2026-06-04'],
        'fetched_at': [fetched, fetched],
    }).to_parquet(path, index=False)


def test_cadence_keyed_on_fetched_at():
    column, max_lag = chk._CADENCES['shares_outstanding.parquet']
    assert column == 'fetched_at'
    assert max_lag <= 14, 'weekly refresh + slack; 35d hid the freeze'


def test_stale_fetch_fails_despite_future_asof(tmp_path, monkeypatch):
    monkeypatch.setattr(chk, '_MASTER_DIR', tmp_path)
    _write_shares(tmp_path / 'shares_outstanding.parquet', fetched_days_ago=80)
    status, detail = chk._master_freshness()
    assert status is Status.FAIL
    assert 'shares_outstanding.parquet' in detail and 'stale' in detail


def test_fresh_fetch_is_not_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(chk, '_MASTER_DIR', tmp_path)
    _write_shares(tmp_path / 'shares_outstanding.parquet', fetched_days_ago=2)
    status, detail = chk._master_freshness()
    # other masters are absent in tmp -> WARN(missing), but never FAIL/stale
    assert status is not Status.FAIL
    assert 'shares_outstanding' not in detail.split('missing')[0]
