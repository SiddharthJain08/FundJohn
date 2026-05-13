"""Pipeline checks — daily-cycle health post-run."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

from ..registry import check
from ..types import Status

ROOT = Path('/root/openclaw')
LOG_DIR = ROOT / 'logs'
HANDOFF_DIR = ROOT / 'output' / 'handoffs'


def _today() -> str:
    return date.today().isoformat()


def _pg():
    return psycopg2.connect(os.environ['POSTGRES_URI'])


@check(name='pipeline_completed_today', tags=['pipeline'], requires=['fs'])
def _pipeline_completed_today():
    """Today's orchestrator log shows 10/10 steps done — the daily heartbeat."""
    today = _today()
    log = LOG_DIR / f'pipeline_orchestrator_{today}.log'
    if not log.exists():
        return Status.WARN, f'no orchestrator log for {today} yet'
    text = log.read_text()
    if 'Pipeline complete' in text and 'all 10 steps done' in text:
        return Status.PASS, 'all 10 steps done'
    if 'FATAL' in text or 'aborting' in text:
        return Status.FAIL, 'pipeline aborted; see log'
    return Status.WARN, 'pipeline not yet complete (or partial)'


@check(name='signals_persisted_today', tags=['pipeline'], requires=['db'])
def _signals_persisted_today():
    """execution_signals has rows for today — engine actually ran strategies."""
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM execution_signals WHERE signal_date = CURRENT_DATE"
        )
        n = cur.fetchone()[0]
    if n == 0:
        return Status.WARN, '0 signals for today (regime gate or empty universe?)'
    return Status.PASS, f'{n} signals persisted'


@check(name='signals_geometry_ordered_today', tags=['pipeline'], requires=['db'])
def _signals_geometry_ordered_today():
    """For each non-NaN signal today, prices are ordered correctly for the direction:
    LONG  → t1 > entry > stop
    SHORT → t1 < entry < stop
    A signal that fails this is sized into a guaranteed-loss bracket."""
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) AS bad FROM execution_signals
            WHERE signal_date = CURRENT_DATE
              AND entry_price <> 'NaN'::numeric
              AND stop_loss   <> 'NaN'::numeric
              AND target_1    <> 'NaN'::numeric
              AND (
                (UPPER(direction) = 'LONG'  AND NOT (target_1 > entry_price AND entry_price > stop_loss))
                OR
                (UPPER(direction) = 'SHORT' AND NOT (target_1 < entry_price AND entry_price < stop_loss))
              )
        """)
        bad = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM execution_signals WHERE signal_date = CURRENT_DATE")
        total = cur.fetchone()[0]
    if total == 0:
        return Status.SKIP, 'no signals today to validate'
    if bad == 0:
        return Status.PASS, f'all {total} signals geometrically ordered'
    return Status.FAIL, f'{bad}/{total} signals have inverted target/stop ordering'


@check(name='handoff_written_today', tags=['pipeline'], requires=['fs'])
def _handoff_written_today():
    """Sized handoff JSON exists for today; has orders or vetoed list non-empty."""
    today = _today()
    sized = HANDOFF_DIR / f'{today}_sized.json'
    if not sized.exists():
        return Status.WARN, f'no sized handoff for {today}'
    try:
        data = json.loads(sized.read_text())
    except json.JSONDecodeError as e:
        return Status.FAIL, f'sized handoff is not valid JSON: {e}'
    orders = data.get('orders') or []
    vetoed = data.get('vetoed') or []
    if not orders and not vetoed:
        return Status.WARN, 'handoff has zero orders AND zero vetoes (engine empty?)'
    return Status.PASS, f'{len(orders)} orders + {len(vetoed)} vetoed'


@check(name='alpaca_submissions_match_handoff_today', tags=['pipeline'], requires=['db', 'fs'])
def _alpaca_submissions_match_handoff_today():
    """Every order in today's sized handoff has a corresponding alpaca_submissions row."""
    today = _today()
    sized = HANDOFF_DIR / f'{today}_sized.json'
    if not sized.exists():
        return Status.SKIP, 'no handoff today'
    orders = (json.loads(sized.read_text()).get('orders') or [])
    if not orders:
        return Status.SKIP, 'handoff had 0 orders'
    handoff_tickers = {o.get('ticker') for o in orders if o.get('ticker')}
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ticker FROM alpaca_submissions WHERE run_date = CURRENT_DATE"
        )
        db_tickers = {r[0] for r in cur.fetchall()}
    missing = handoff_tickers - db_tickers
    if missing:
        return Status.FAIL, f'{len(missing)} handoff tickers absent from alpaca_submissions: {sorted(missing)[:5]}'
    return Status.PASS, f'all {len(handoff_tickers)} handoff orders persisted to alpaca_submissions'


@check(name='pipeline_log_no_unhandled_traceback_today', tags=['pipeline'], requires=['fs'])
def _pipeline_log_no_unhandled_traceback():
    """Today's orchestrator log doesn't contain a Python Traceback that escaped a stage."""
    today = _today()
    log = LOG_DIR / f'pipeline_orchestrator_{today}.log'
    if not log.exists():
        return Status.SKIP, 'no log today'
    text = log.read_text()
    # Stage handlers wrap exceptions and log "stage ... FAILED" — that's expected.
    # A bare Traceback that wasn't caught means a stage crashed without proper handling.
    n_traceback = text.count('Traceback (most recent call last):')
    if n_traceback == 0:
        return Status.PASS, 'no tracebacks in log'
    return Status.WARN, f'{n_traceback} tracebacks in log; inspect manually'
