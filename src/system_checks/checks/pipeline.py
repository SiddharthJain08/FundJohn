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
    """Today's orchestrator log shows all steps done — the daily heartbeat."""
    today = _today()
    log = LOG_DIR / f'pipeline_orchestrator_{today}.log'
    if not log.exists():
        return Status.WARN, f'no orchestrator log for {today} yet'
    text = log.read_text()
    if 'Pipeline complete' in text and 'steps done' in text:
        return Status.PASS, 'pipeline completed today'
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


# ── SP-6 Phase A: EOD signal-register + premarket-gate health ─────────────
# All three checks are gated: they SKIP gracefully when the relevant EOD gate
# is off, so the legacy close-exec flow gets zero spurious warnings.


@check(name='eod_compute_health_fresh', tags=['pipeline'], requires=['db'])
def _eod_compute_health_fresh():
    """When OPENCLAW_EOD_SIGNAL_REGISTER=1: latest eod_compute_health row must
    be for CURRENT_DATE and healthy=true.  WARN if missing/stale/unhealthy.
    Gate off → SKIP (no spurious noise on the legacy flow)."""
    if os.environ.get('OPENCLAW_EOD_SIGNAL_REGISTER') != '1':
        return Status.SKIP, 'OPENCLAW_EOD_SIGNAL_REGISTER is OFF'
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT healthy, run_date FROM eod_compute_health
            ORDER BY run_date DESC LIMIT 1
        """)
        row = cur.fetchone()
    if row is None:
        return Status.WARN, 'no eod_compute_health row yet today'
    healthy, run_date = row
    today = date.today()
    if run_date != today:
        return Status.WARN, f'latest eod_compute_health is {(today - run_date).days}d old'
    if not healthy:
        return Status.WARN, 'latest eod_compute_health marked unhealthy'
    return Status.PASS, 'eod_compute_health fresh and healthy'


@check(name='carried_set_present', tags=['pipeline'], requires=['db'])
def _carried_set_present():
    """When OPENCLAW_EOD_SIGNAL_REGISTER=1: expect COMPUTED and/or APPROVED
    execution_signals rows for today.  WARN if zero (engine may have hit the
    regime gate or universe was empty).  Gate off → SKIP."""
    if os.environ.get('OPENCLAW_EOD_SIGNAL_REGISTER') != '1':
        return Status.SKIP, 'OPENCLAW_EOD_SIGNAL_REGISTER is OFF'
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM execution_signals
            WHERE (signal_date = CURRENT_DATE OR target_date = CURRENT_DATE)
              AND lifecycle_state IN ('COMPUTED', 'APPROVED')
        """)
        n = cur.fetchone()[0]
    if n == 0:
        return Status.WARN, '0 COMPUTED/APPROVED signals for today despite gate ON'
    return Status.PASS, f'{n} COMPUTED/APPROVED signals for today'


@check(name='gate_ran_today', tags=['pipeline'], requires=['db'])
def _gate_ran_today():
    """When OPENCLAW_EOD_PREMARKET_GATE=1: expect a signal_gate_verdicts sentinel
    row with gate_type='__gate_ran__' for today.  Without it the 9:32 reconcile
    step will refuse to flatten (it checks for gate completion).
    Gate off → SKIP."""
    if os.environ.get('OPENCLAW_EOD_PREMARKET_GATE') != '1':
        return Status.SKIP, 'OPENCLAW_EOD_PREMARKET_GATE is OFF'
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM signal_gate_verdicts
            WHERE target_date = CURRENT_DATE
              AND gate_type = '__gate_ran__'
        """)
        n = cur.fetchone()[0]
    if n == 0:
        return Status.WARN, 'no __gate_ran__ sentinel for today; reconcile will refuse to flatten'
    return Status.PASS, f'gate sentinel present for today ({n} row)'
