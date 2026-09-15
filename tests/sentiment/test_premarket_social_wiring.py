"""C4 (item 16): the pre-market scan reads the real per-ticker social row from
ticker_sentiment_daily instead of hardcoding zeros, and reports where the value
came from via `social_source` in its log line.

Every DB surface is a fake cursor — these tests must pass with Postgres down.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from src.pipeline import run_premarket_scan as mod
from src.sentiment.premarket_scorer import ScoreInputs, panic_score


class FakeCursor:
    """Minimal psycopg2 cursor stand-in: records the SQL + params, replays rows."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return list(self._rows)


TODAY = date(2026, 9, 13)


# ── _social_rows_from_cursor ────────────────────────────────────────────────

def test_rows_from_cursor_maps_posts_and_bear_ratio():
    cur = FakeCursor([('SNDK', date(2026, 9, 12), 140, 0.62)])
    out = mod._social_rows_from_cursor(cur, ['SNDK'], TODAY, 3)
    assert out == {
        'SNDK': {
            'social_posts_24h': 140,
            'social_bear_ratio': 0.62,
            'social_source': 'ticker_sentiment_daily:2026-09-12',
        }
    }


def test_rows_from_cursor_bounds_the_date_window():
    cur = FakeCursor([])
    mod._social_rows_from_cursor(cur, ['AAPL', 'MSFT'], TODAY, 3)
    assert cur.params == (['AAPL', 'MSFT'], TODAY, date(2026, 9, 10))
    assert 'DISTINCT ON (ticker)' in cur.sql
    assert 'ORDER BY ticker, date DESC' in cur.sql


def test_rows_from_cursor_null_bear_ratio_becomes_zero():
    cur = FakeCursor([('AMD', date(2026, 9, 13), 0, None)])
    out = mod._social_rows_from_cursor(cur, ['AMD'], TODAY, 3)
    assert out['AMD']['social_bear_ratio'] == 0.0
    assert out['AMD']['social_posts_24h'] == 0


def test_load_social_is_fail_open(monkeypatch):
    monkeypatch.delenv('POSTGRES_URI', raising=False)
    assert mod._load_social_for_tickers(['AAPL'], TODAY) == {}


def test_load_social_short_circuits_on_empty_tickers(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError('must not open a connection for an empty ticker list')

    monkeypatch.setattr(mod.psycopg2, 'connect', _boom)
    assert mod._load_social_for_tickers([], TODAY) == {}


def test_social_max_age_days_env_override(monkeypatch):
    monkeypatch.setenv('OPENCLAW_PREMARKET_SOCIAL_MAX_AGE_DAYS', '7')
    assert mod._social_max_age_days() == 7
    monkeypatch.setenv('OPENCLAW_PREMARKET_SOCIAL_MAX_AGE_DAYS', 'nonsense')
    assert mod._social_max_age_days() == 3
    monkeypatch.delenv('OPENCLAW_PREMARKET_SOCIAL_MAX_AGE_DAYS', raising=False)
    assert mod._social_max_age_days() == 3


# ── _evaluate_ticker wiring ─────────────────────────────────────────────────

def _cfg(confirmer=False):
    return mod.ScanConfig(
        scan_enabled=True, confirmer_enabled=confirmer, autoclose_enabled=False,
        advisory_threshold=35.0, autoclose_min_severity=4,
        max_tickers_per_scan=25, confirmer_budget_usd=0.5,
    )


def _news(monkeypatch, count=5, neg=0.4):
    monkeypatch.setattr(mod, 'score_news_for_tickers', lambda tickers, start: [{
        'news_count_24h': count, 'news_finbert_neg': neg, 'news_mean_score': -0.2,
        'news_top_headlines': [], 'evidence_uuids': [],
    }])


SCAN_TS = datetime(2026, 9, 13, 11, 30, tzinfo=timezone.utc)
WINDOW = datetime(2026, 9, 12, 22, 0, tzinfo=timezone.utc)
POS = {'symbol': 'SNDK', 'qty': 10, 'avg_entry_price': 100.0}


def test_evaluate_ticker_passes_social_through_to_the_score(monkeypatch):
    _news(monkeypatch)
    social = {'SNDK': {'social_posts_24h': 140, 'social_bear_ratio': 1.0,
                       'social_source': 'ticker_sentiment_daily:2026-09-12'}}
    row = mod._evaluate_ticker(POS, _cfg(), SCAN_TS, '07:30', WINDOW, social=social)
    assert row['social_post_count_window'] == 140
    assert row['social_bear_ratio'] == 1.0
    # 60*0.4 + 30*min(50,100)/100 + 10*1.0 = 24 + 15 + 10 = 49
    assert row['panic_score'] == 49.0


def test_evaluate_ticker_absent_social_scores_as_zero(monkeypatch):
    _news(monkeypatch)
    row = mod._evaluate_ticker(POS, _cfg(), SCAN_TS, '07:30', WINDOW, social={})
    assert row['social_post_count_window'] == 0
    assert row['social_bear_ratio'] == 0.0
    assert row['panic_score'] == 39.0     # 24 + 15 + 0


def test_evaluate_ticker_logs_social_source(monkeypatch, caplog):
    _news(monkeypatch)
    social = {'SNDK': {'social_posts_24h': 3, 'social_bear_ratio': 0.5,
                       'social_source': 'ticker_sentiment_daily:2026-09-11'}}
    with caplog.at_level(logging.INFO, logger=mod.log.name):
        mod._evaluate_ticker(POS, _cfg(), SCAN_TS, '07:30', WINDOW, social=social)
    line = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'social_source=ticker_sentiment_daily:2026-09-11' in line
    assert 'social_posts=3' in line


def test_evaluate_ticker_logs_absent_source(monkeypatch, caplog):
    _news(monkeypatch)
    with caplog.at_level(logging.INFO, logger=mod.log.name):
        mod._evaluate_ticker(POS, _cfg(), SCAN_TS, '07:30', WINDOW, social=None)
    assert 'social_source=absent' in '\n'.join(r.getMessage() for r in caplog.records)


def test_confirmer_receives_the_real_social_bear_ratio(monkeypatch):
    _news(monkeypatch, count=9, neg=0.9)
    seen = {}

    class _Result:
        verdict = 'bearish_news_driven'
        severity = 5
        rationale = 'r'
        evidence_uuids: list = []
        cost_usd = 0.01

    def _confirm(inp, max_budget_usd=None):
        seen['social_bear_ratio'] = inp.social_bear_ratio
        return _Result()

    monkeypatch.setattr(mod, 'confirm_panic', _confirm)
    social = {'SNDK': {'social_posts_24h': 50, 'social_bear_ratio': 0.75,
                       'social_source': 'ticker_sentiment_daily:2026-09-12'}}
    mod._evaluate_ticker(POS, _cfg(confirmer=True), SCAN_TS, '07:30', WINDOW,
                         social=social)
    assert seen['social_bear_ratio'] == 0.75


# ── the scorer's MVP precondition is deliberately preserved ─────────────────

def test_pure_social_with_no_news_still_scores_zero():
    """news_count_window < 1 => 0.0 is a documented precondition
    (premarket_scorer.py:35-39). Item 16 wires the INPUT; it must not turn the
    scan into a pure-social scorer."""
    assert panic_score(ScoreInputs(0, 0.0, 0.0, 500, 1.0)) == 0.0
