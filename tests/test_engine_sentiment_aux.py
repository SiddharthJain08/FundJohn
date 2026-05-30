"""Engine wiring for aux['sentiment'] — reads ticker_sentiment_daily.alpaca_news_*,
remaps to news_* keys, fail-open. The forward-fill/staleness logic lives in the
pure src/execution/sentiment_aux.build_sentiment_aux (tested separately); this
covers the DB wrapper: column unpacking, empty-universe short-circuit, fail-open,
connection close.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from datetime import date

import execution.engine as eng


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.sql = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.sql, self.params = sql, params

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._rows)

    def close(self):
        self.closed = True


def test_sentiment_slice_remaps_columns_and_closes_conn(monkeypatch):
    rows = [
        ('AAPL', date(2026, 5, 29), 4, 0.42, 3, 0, 1),
        ('MSFT', date(2026, 5, 26), 5, 0.30, 4, 1, 0),
    ]
    fake = _FakeConn(rows)
    monkeypatch.setattr(eng, 'get_db', lambda: fake)
    out = eng._sentiment_slice(['AAPL', 'MSFT'], as_of=date(2026, 5, 30))
    assert out['AAPL']['news_count_24h'] == 4
    assert out['AAPL']['news_mean_score'] == 0.42
    assert out['AAPL']['news_finbert_neg'] == 1
    assert out['MSFT']['news_count_24h'] == 5
    assert fake.closed is True


def test_sentiment_slice_empty_universe_skips_db(monkeypatch):
    def _boom():
        raise AssertionError('must not open a DB connection for empty universe')
    monkeypatch.setattr(eng, 'get_db', _boom)
    assert eng._sentiment_slice([]) == {}


def test_sentiment_slice_fail_open_on_db_error(monkeypatch):
    def _boom():
        raise RuntimeError('db down')
    monkeypatch.setattr(eng, 'get_db', _boom)
    assert eng._sentiment_slice(['AAPL']) == {}
