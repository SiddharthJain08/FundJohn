"""Pipeline checks — daily-cycle health post-run."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

from ..registry import check
from ..types import Status

ROOT = Path('/root/openclaw')
LOG_DIR = ROOT / 'logs'
HANDOFF_DIR = ROOT / 'output' / 'handoffs'

ALPACA_CLI = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')


def _today() -> str:
    return date.today().isoformat()


def _is_trading_day(d: date) -> bool:
    """True if `d` is an NYSE trading day, per the broker calendar.

    Asks `alpaca calendar --start <d> --end <d>`: a NON-EMPTY JSON array means
    it's a trading day; `[]` means a holiday/weekend. On any CLI error/timeout
    (binary missing, non-zero exit, bad JSON) we FALL BACK to a weekday check
    (Mon-Fri => assume trading day) so the daily-cycle checks still fire on a
    normal day when the CLI is unavailable. The env (ALPACA_* keys) is
    inherited from os.environ by subprocess."""
    iso = d.isoformat()
    try:
        r = subprocess.run(
            [ALPACA_CLI, 'calendar', '--start', iso, '--end', iso],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            raise RuntimeError(f'rc={r.returncode}')
        cal = json.loads(r.stdout)
        return bool(cal)
    except (FileNotFoundError, subprocess.TimeoutExpired,
            json.JSONDecodeError, RuntimeError, OSError):
        # Fallback: Mon-Fri are trading days, weekends are not.
        return d.weekday() < 5


def _pg():
    return psycopg2.connect(os.environ['POSTGRES_URI'])


@check(name='pipeline_completed_today', tags=['pipeline'], requires=['fs'])
def _pipeline_completed_today():
    """Today's orchestrator log shows all steps done — the daily heartbeat."""
    if not _is_trading_day(date.today()):
        return Status.SKIP, f'{_today()} is not a trading day (market holiday/weekend)'
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


# The same-day compute (OPENCLAW_SAMEDAY_EXEC=1) fires at 15:00 ET — signals
# land ~19:02Z. The daily maintenance runner is 12:00 ET (16:00Z), i.e. THREE
# HOURS EARLIER, so a naive "are there signals today?" probe warned on every
# single trading day and carried no information. That is why 2026-08-05 — a
# genuine zero-signal day, both computes killed (rc=137 then rc=124) — was
# indistinguishable from every healthy day. Gate on the window, and separate
# "engine ran and found nothing" from "engine never finished".
_SAMEDAY_COMPUTE_UTC_HOUR = 19


def _sameday_mode() -> bool:
    return os.environ.get('OPENCLAW_SAMEDAY_EXEC') == '1'


@check(name='signals_persisted_today', tags=['pipeline'], requires=['db'])
def _signals_persisted_today():
    """execution_signals has rows for today — engine actually ran strategies.

    A zero count is only meaningful once the compute window has closed, and it
    means two very different things depending on whether the engine COMPLETED:
    a finished run with 0 signals is a dry market (WARN); a run that never
    finished is a dead pipeline (FAIL). Conflating them is what hid 08-05.
    """
    if not _is_trading_day(date.today()):
        return Status.SKIP, f'{_today()} is not a trading day (market holiday/weekend)'

    now_utc = datetime.now(timezone.utc)
    if _sameday_mode() and now_utc.hour < _SAMEDAY_COMPUTE_UTC_HOUR:
        return Status.SKIP, (
            f'same-day compute has not run yet (fires ~{_SAMEDAY_COMPUTE_UTC_HOUR}:00Z, '
            f'now {now_utc:%H:%M}Z) — a zero count here is expected, not a fault'
        )

    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM execution_signals WHERE signal_date = CURRENT_DATE"
        )
        n = cur.fetchone()[0]
        # execution_runs is written by engine.py's log_run() at the very END of
        # a run, so a row today == the engine reached completion at least once.
        cur.execute(
            "SELECT COALESCE(MAX(duration_seconds), 0), COUNT(*)"
            "  FROM execution_runs WHERE run_date = CURRENT_DATE"
        )
        longest, n_runs = cur.fetchone()

    if n > 0:
        return Status.PASS, f'{n} signals persisted ({n_runs} completed engine run(s))'
    if n_runs == 0:
        return Status.FAIL, (
            'ZERO signals and the engine never completed today — no execution_runs '
            'row exists, so the signals step died (check rc=137 OOM / rc=124 timeout '
            'in logs/daily_cycle_aborts_*.log). The book is unchanged.'
        )
    return Status.WARN, (
        f'0 signals but the engine DID complete ({n_runs} run(s), longest {longest}s) '
        f'— strategies genuinely fired nothing today'
    )


@check(name='signals_step_timeout_headroom', tags=['pipeline'], requires=['db'])
def _signals_step_timeout_headroom():
    """The engine's runtime is well inside its subprocess timeout.

    Leading indicator for the 2026-08-05 outage: duration crept 173s (07-21,
    89 strategies) -> 264s (08-04, 97) against a 300s cap and nothing watched
    it, so the first anyone knew was a zero-signal day. The cap is now
    OPENCLAW_SIGNALS_TIMEOUT_SECONDS (default 900) in BOTH resolve_script.js
    and pipeline_orchestrator.py — keep this threshold reading the same knob.
    """
    cap = int(os.environ.get('OPENCLAW_SIGNALS_TIMEOUT_SECONDS', '900'))
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT run_date, duration_seconds FROM execution_runs"
            " WHERE duration_seconds IS NOT NULL"
            " ORDER BY created_at DESC LIMIT 5"
        )
        rows = cur.fetchall()
    if not rows:
        return Status.SKIP, 'no execution_runs rows yet'

    worst_date, worst = max(rows, key=lambda r: float(r[1]))
    pct = float(worst) / cap * 100
    trend = ', '.join(f'{float(d):.0f}s' for _, d in reversed(rows))
    detail = f'peak {float(worst):.0f}s of {cap}s cap ({pct:.0f}%) on {worst_date}; last 5: {trend}'
    if pct >= 85:
        return Status.FAIL, f'signals step is about to blow its timeout — {detail}'
    if pct >= 70:
        return Status.WARN, f'signals step timeout headroom shrinking — {detail}'
    return Status.PASS, detail


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
    if not _is_trading_day(date.today()):
        return Status.SKIP, f'{_today()} is not a trading day (market holiday/weekend)'
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
    """In T+1 EOD-register mode: latest eod_compute_health row must
    be for CURRENT_DATE and healthy=true.  WARN if missing/stale/unhealthy.
    Same-day mode → SKIP (no spurious noise on the same-day flow)."""
    from execution.signal_target_mode import eod_register_on   # §8 alias resolver
    if not eod_register_on():
        return Status.SKIP, 'same-day signal target is ON (T+1 register flow inactive)'
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
    """In T+1 EOD-register mode: expect COMPUTED and/or APPROVED
    execution_signals rows for today.  WARN if zero (engine may have hit the
    regime gate or universe was empty).  Same-day mode → SKIP."""
    if not _is_trading_day(date.today()):
        return Status.SKIP, f'{_today()} is not a trading day (market holiday/weekend)'
    from execution.signal_target_mode import eod_register_on   # §8 alias resolver
    if not eod_register_on():
        return Status.SKIP, 'same-day signal target is ON (T+1 register flow inactive)'
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
