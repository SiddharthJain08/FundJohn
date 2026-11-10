"""universe_* system checks with crypto rows in alpaca_tradable_universe
(crypto engine Phase 3): the 5,000-row floor counts us_equity only, and a
crypto pull failure recorded in refresh_log.notes surfaces as WARN."""
from datetime import datetime

import pytest

from src.system_checks.checks import universe as u
from src.system_checks.types import Status


class _Cur:
    def __init__(self, row, log):
        self.row, self.log = row, log

    def execute(self, sql, params=None):
        self.log.append(sql)

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, row, log):
        self.row, self.log = row, log

    def cursor(self):
        return _Cur(self.row, self.log)

    def close(self):
        pass


def _fn(check):
    return getattr(check, '__wrapped__', check)


@pytest.fixture
def fake(monkeypatch):
    state = {'row': None, 'log': []}
    monkeypatch.setattr(u, '_connect', lambda: _Conn(state['row'], state['log']))
    return state


def test_populated_counts_us_equity_only(fake):
    fake['row'] = (12000,)
    status, msg = _fn(u._universe_table_populated)()
    assert status == Status.PASS
    sql = fake['log'][-1]
    assert "COALESCE(asset_class, 'us_equity') = 'us_equity'" in sql


def _diff_row(notes):
    return (12040, 12000, 0, 0, 0, datetime(2026, 11, 10, 13, 30), notes)


def test_recent_diff_warns_on_crypto_error(fake):
    fake['row'] = _diff_row('crypto fetched=0 upserted=0 skipped_non_usd=0 '
                            'unmapped_usd=0 crypto_error=RuntimeError: down')
    status, msg = _fn(u._universe_recent_diff)()
    assert status == Status.WARN and 'crypto' in msg


def test_recent_diff_warns_on_unmapped_usd(fake):
    fake['row'] = _diff_row('crypto fetched=41 upserted=3 skipped_non_usd=37 '
                            'unmapped_usd=1 unmapped=ZZZ/USD')
    status, _ = _fn(u._universe_recent_diff)()
    assert status == Status.WARN


@pytest.mark.parametrize('notes', [None, 'crypto fetched=40 upserted=3 '
                                         'skipped_non_usd=37 unmapped_usd=0'])
def test_recent_diff_passes_clean(fake, notes):
    fake['row'] = _diff_row(notes)
    status, _ = _fn(u._universe_recent_diff)()
    assert status == Status.PASS
