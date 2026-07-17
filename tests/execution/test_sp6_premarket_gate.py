"""Tests for src/execution/premarket_gate.py — SP-6 Task 7.

Test cases:
  (a) bearish news (mocked scorers) → REJECTED verdict + audit row + lifecycle_state='REJECTED'
  (b) benign/no news (mocked scorers return []) → APPROVED + lifecycle_state='APPROVED'
  (c) fail-open: score_news_for_tickers raises → APPROVED + warning logged
  (d) __gate_ran__ sentinel row written even with zero COMPUTED signals
  (e) gate OFF (env var unset) → no-op, no DB rows touched

Scorers are mocked at the premarket_gate module scope so monkeypatch takes effect.
All tests use the rollback-isolated db_conn fixture; no data persists to the live DB.
"""
from __future__ import annotations

import logging
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import execution.premarket_gate as gate_mod  # noqa: E402
from sentiment.sonnet_premarket_confirmer import (  # noqa: E402
    PremarketConfirmerResult,
)

pytestmark = pytest.mark.integration


# ─────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────

@pytest.fixture
def db_conn():
    """Auto-rollback psycopg2 DictCursor connection (no persistent side-effects)."""
    uri = os.environ.get(
        'TEST_POSTGRES_URI',
        os.environ.get('POSTGRES_URI', 'postgresql://openclaw:password@localhost:5432/openclaw'),
    )
    conn = psycopg2.connect(uri, cursor_factory=psycopg2.extras.DictCursor)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture(autouse=True)
def gate_on(monkeypatch):
    """Enable the gate for all tests in this module (override per-test where needed)."""
    monkeypatch.setenv('OPENCLAW_EOD_PREMARKET_GATE', '1')


@pytest.fixture
def any_strategy_id(db_conn) -> str:
    """Return any strategy_id from strategy_registry (FK-safe for test inserts)."""
    cur = db_conn.cursor()
    cur.execute("SELECT id FROM strategy_registry LIMIT 1")
    row = cur.fetchone()
    assert row is not None, "No strategies in strategy_registry"
    return row[0]


def _insert_computed_signal(cur, ticker: str, target_date: date, strategy_id: str) -> str:
    """Insert a COMPUTED execution_signal for today and return its UUID."""
    signal_date = target_date - timedelta(days=1)
    cur.execute(
        """
        INSERT INTO execution_signals
            (strategy_id, signal_date, ticker, direction, position_size_pct,
             target_1, stop_loss, lifecycle_state, target_date, computed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            strategy_id,
            signal_date,
            ticker,
            'long',
            5.0,
            110.0,
            95.0,
            'COMPUTED',
            target_date,
            datetime.now(timezone.utc),
        ),
    )
    return str(cur.fetchone()[0])


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _count_sentinel(cur, target_date: date) -> int:
    cur.execute(
        """
        SELECT COUNT(*) FROM signal_gate_verdicts
         WHERE gate_type = '__gate_ran__' AND target_date = %s
        """,
        (target_date,),
    )
    return cur.fetchone()[0]


def _count_verdict_rows(cur, signal_id: str) -> int:
    cur.execute(
        "SELECT COUNT(*) FROM signal_gate_verdicts WHERE signal_id = %s",
        (signal_id,),
    )
    return cur.fetchone()[0]


def _get_signal_lifecycle(cur, signal_id: str) -> dict:
    cur.execute(
        "SELECT lifecycle_state, approved_at, gate_verdict FROM execution_signals WHERE id = %s",
        (signal_id,),
    )
    row = cur.fetchone()
    assert row is not None, f"signal {signal_id} not found"
    return dict(row)


# ─────────────────────────────────────────────────────────
# (a) bearish news → REJECTED
# ─────────────────────────────────────────────────────────

def test_bearish_news_rejects_signal(db_conn, monkeypatch, any_strategy_id):
    """Strong bearish news + confirmer bearish verdict → REJECTED + audit row."""
    today = _today()
    cur = db_conn.cursor()
    signal_id = _insert_computed_signal(cur, 'TSTB', today, any_strategy_id)

    # Mock score_news_for_tickers: return neg_ratio=1.0, count=3 → panic≈78
    bearish_news = [
        {
            'ticker': 'TSTB',
            'news_count_24h': 3,
            'news_finbert_pos': 0,
            'news_finbert_neu': 0,
            'news_finbert_neg': 3,
            'news_mean_score': -0.9,
            'news_top_headlines': ['CFO resigns', 'Fraud probe', 'Revenue miss'],
            'evidence_uuids': [str(uuid.uuid4()), str(uuid.uuid4())],
        }
    ]
    monkeypatch.setattr(gate_mod, 'score_news_for_tickers', lambda *a, **kw: bearish_news)

    # Mock confirm_panic: return bearish_news_driven
    bearish_result = PremarketConfirmerResult(
        verdict='bearish_news_driven',
        severity=4,
        rationale='CFO resigned and fraud probe opened.',
        evidence_uuids=[],
        cost_usd=0.01,
    )
    monkeypatch.setattr(gate_mod, 'confirm_panic', lambda *a, **kw: bearish_result)

    result = gate_mod.run_gate(conn=db_conn)

    assert result['gate_ran'] is True
    assert result['n_processed'] == 1
    assert result['n_rejected'] == 1
    assert result['n_approved'] == 0

    # Verdict row written
    assert _count_verdict_rows(cur, signal_id) == 1

    # Signal lifecycle updated
    lc = _get_signal_lifecycle(cur, signal_id)
    assert lc['lifecycle_state'] == 'REJECTED'
    assert lc['approved_at'] is not None
    assert lc['gate_verdict'] is not None
    assert lc['gate_verdict']['verdict'] == 'REJECTED'

    # Sentinel written
    assert _count_sentinel(cur, today) >= 1


# ─────────────────────────────────────────────────────────
# (b) benign/no news → APPROVED
# ─────────────────────────────────────────────────────────

def test_benign_news_approves_signal(db_conn, monkeypatch, any_strategy_id):
    """No news returned → panic_score=0 → APPROVED, confirmer never called."""
    today = _today()
    cur = db_conn.cursor()
    signal_id = _insert_computed_signal(cur, 'TSTN', today, any_strategy_id)

    # No news for any ticker → panic=0 → APPROVED without calling confirmer
    monkeypatch.setattr(gate_mod, 'score_news_for_tickers', lambda *a, **kw: [])

    # confirmer should not be invoked; mock it to raise so we catch accidental calls
    monkeypatch.setattr(
        gate_mod, 'confirm_panic',
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError('confirmer must not run on benign signal')),
    )

    result = gate_mod.run_gate(conn=db_conn)

    assert result['gate_ran'] is True
    assert result['n_processed'] == 1
    assert result['n_approved'] == 1
    assert result['n_rejected'] == 0

    # Verdict row written
    assert _count_verdict_rows(cur, signal_id) == 1

    # Signal lifecycle updated
    lc = _get_signal_lifecycle(cur, signal_id)
    assert lc['lifecycle_state'] == 'APPROVED'
    assert lc['approved_at'] is not None
    assert lc['gate_verdict']['verdict'] == 'APPROVED'

    # Sentinel written
    assert _count_sentinel(cur, today) >= 1


# ─────────────────────────────────────────────────────────
# (c) fail-open: FinBERT raises → APPROVED + warning
# ─────────────────────────────────────────────────────────

def test_finbert_error_fail_open(db_conn, monkeypatch, caplog, any_strategy_id):
    """score_news_for_tickers raises → caught, APPROVED, warning logged."""
    today = _today()
    cur = db_conn.cursor()
    signal_id = _insert_computed_signal(cur, 'TSTF', today, any_strategy_id)

    def _raise(*args, **kwargs):
        raise ConnectionError('FinBERT service :7872 unreachable')

    monkeypatch.setattr(gate_mod, 'score_news_for_tickers', _raise)
    monkeypatch.setattr(gate_mod, 'confirm_panic', lambda *a, **kw: None)  # should not run

    with caplog.at_level(logging.WARNING, logger='execution.premarket_gate'):
        result = gate_mod.run_gate(conn=db_conn)

    assert result['gate_ran'] is True
    assert result['n_processed'] == 1
    assert result['n_approved'] == 1
    assert result['n_rejected'] == 0

    # At least one error recorded
    assert any('news_score_error' in e for e in result['errors'])

    # Warning was logged
    assert any('fail-open' in r.message for r in caplog.records), (
        f"Expected a 'fail-open' warning; got: {[r.message for r in caplog.records]}"
    )

    # Signal still gets APPROVED lifecycle
    lc = _get_signal_lifecycle(cur, signal_id)
    assert lc['lifecycle_state'] == 'APPROVED'

    # Sentinel still written
    assert _count_sentinel(cur, today) >= 1


# ─────────────────────────────────────────────────────────
# (d) __gate_ran__ sentinel written even with zero COMPUTED signals
# ─────────────────────────────────────────────────────────

def test_sentinel_written_with_zero_signals(db_conn, monkeypatch):
    """No COMPUTED signals for today → gate_ran=True, sentinel row exists."""
    today = _today()
    cur = db_conn.cursor()

    # Ensure there are no COMPUTED signals for today (rolled-back fixture guarantees clean state,
    # but also check any stray rows by querying before running)
    cur.execute(
        "SELECT COUNT(*) FROM execution_signals WHERE lifecycle_state='COMPUTED' AND target_date=%s",
        (today,),
    )
    pre_count = cur.fetchone()[0]

    # We don't delete existing rows (they're not ours); just verify the sentinel appears
    before_sentinel = _count_sentinel(cur, today)

    # score_news_for_tickers should not be called when no signals
    monkeypatch.setattr(gate_mod, 'score_news_for_tickers', lambda *a, **kw: [])
    monkeypatch.setattr(gate_mod, 'confirm_panic', lambda *a, **kw: None)

    result = gate_mod.run_gate(conn=db_conn)

    assert result['gate_ran'] is True

    # Sentinel count increased by 1
    after_sentinel = _count_sentinel(cur, today)
    assert after_sentinel == before_sentinel + 1, (
        f"Expected sentinel count to increase by 1 (was {before_sentinel}, now {after_sentinel})"
    )

    # n_processed may be non-zero if there were pre-existing COMPUTED rows
    assert result['n_processed'] == pre_count


# ─────────────────────────────────────────────────────────
# (e) gate OFF → no-op (no DB rows touched)
# ─────────────────────────────────────────────────────────

def test_gate_off_noop(db_conn, monkeypatch, any_strategy_id):
    """Gate env var unset → run_gate returns gate_ran=False, no rows written."""
    monkeypatch.delenv('OPENCLAW_EOD_PREMARKET_GATE', raising=False)

    today = _today()
    cur = db_conn.cursor()

    # Insert a COMPUTED signal that should NOT be touched
    signal_id = _insert_computed_signal(cur, 'TSTG', today, any_strategy_id)

    before_sentinel = _count_sentinel(cur, today)
    before_verdict = _count_verdict_rows(cur, signal_id)

    result = gate_mod.run_gate(conn=db_conn)

    assert result['gate_ran'] is False
    assert result['n_processed'] == 0

    # No new rows written
    assert _count_sentinel(cur, today) == before_sentinel
    assert _count_verdict_rows(cur, signal_id) == before_verdict

    # Signal lifecycle untouched
    lc = _get_signal_lifecycle(cur, signal_id)
    assert lc['lifecycle_state'] == 'COMPUTED', (
        "lifecycle_state must remain COMPUTED when gate is OFF"
    )
    assert lc['approved_at'] is None
    assert lc['gate_verdict'] is None
