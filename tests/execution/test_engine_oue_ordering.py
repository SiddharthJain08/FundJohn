"""Regression: OUE classification must run AFTER the cycle commits.

Bug (live 2026-05-16 → 2026-05-29): engine.update_pnl called
classify_batch on a SEPARATE psycopg2 connection *before* run()
committed the closes (conn.commit() is after update_pnl returns). The
classifier's connection therefore saw no committed signal_pnl rows,
`realized` came back None, and every signal was skipped — proven by the
cycle logs: "OUE classified N closes: {... 'skipped': N}". oue_kind
stayed NULL forever.

Fix: update_pnl returns the newly-closed signal ids and classifies
nothing; run() calls classify_batch after conn.commit().
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
from execution import engine            # noqa: E402
from execution import oue_classifier    # noqa: E402


class _FakeCursor:
    """Minimal RealDictCursor stand-in for update_pnl: returns the canned
    open signals on the first SELECT (the only query that fetches), records
    every execute, fetches [] afterwards."""

    def __init__(self, open_rows):
        self._open_rows = open_rows
        self._fetch: list = []
        self.executed: list = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        self._fetch = list(self._open_rows) if "status = 'open'" in sql else []

    def fetchall(self):
        return self._fetch


def _open_long_about_to_stop():
    return [{
        'id': 'sig-1', 'strategy_id': 'S_test', 'ticker': 'AAPL',
        'direction': 'LONG', 'entry_price': 100.0, 'stop_loss': 95.0,
        'target_1': 110.0, 'signal_date': date(2026, 5, 20),
    }]


def _prices_at(px):
    return pd.DataFrame({'AAPL': [px]})


def test_update_pnl_returns_count_and_newly_closed_ids():
    cur = _FakeCursor(_open_long_about_to_stop())
    # Price below the stop -> the LONG closes via stop_loss.
    n, closed_ids = engine.update_pnl(cur, _prices_at(90.0), date(2026, 5, 27))
    assert n == 1
    assert closed_ids == ['sig-1']


def test_update_pnl_does_not_classify_inline(monkeypatch):
    """The pre-commit classify_batch call is the bug; update_pnl must not
    classify at all — only return ids for the caller to classify post-commit."""
    called = {'n': 0}

    def _spy(*a, **k):
        called['n'] += 1
        return {}

    monkeypatch.setattr(oue_classifier, 'classify_batch', _spy)
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://unused')
    cur = _FakeCursor(_open_long_about_to_stop())
    engine.update_pnl(cur, _prices_at(90.0), date(2026, 5, 27))
    assert called['n'] == 0, 'update_pnl must NOT classify inline (pre-commit)'
