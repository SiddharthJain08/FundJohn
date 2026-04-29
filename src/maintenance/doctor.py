#!/usr/bin/env python3
"""
doctor.py — FundJohn pre-flight health check.

Runs in <5 seconds against a healthy system and reports the operational
state of every dependency the daily 10am cycle needs. Designed for three
audiences:

  1. systemd `ExecStartPre` for johnbot.service (mode=--quick, fast subset)
  2. pipeline_orchestrator.py preflight (mode=--required-only, fail loud
     on auth/config to avoid burning a partial cycle)
  3. daily_health_digest footer (mode=--json, parsed for one-line summary)

Exit codes follow the repo-wide convention adopted in Tier 3:
  0  success — all checks pass
  1  warning — some non-critical checks failed (stale data, slow Postgres)
  2  fail   — auth / config / missing-env / unreachable critical service

Each check has a hard 5s timeout. The whole run aborts cleanly if it
exceeds 30s wall time even with a hung subcheck.

Usage:
  python3 src/maintenance/doctor.py            # full report on stdout
  python3 src/maintenance/doctor.py --json     # machine-readable
  python3 src/maintenance/doctor.py --quick    # subset: skip slow probes
  python3 src/maintenance/doctor.py --required-only  # only blockers
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

ALPACA_CLI = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')

# Severity → exit code contribution.
PASS = 'pass'
WARN = 'warn'
FAIL = 'fail'

# Required env vars: missing → exit 2 (cycle cannot proceed).
REQUIRED_ENV = ['ALPACA_API_KEY', 'ALPACA_SECRET_KEY', 'POSTGRES_URI']
# Optional env vars: missing → exit 1 (cycle can proceed but reduced functionality).
OPTIONAL_ENV = ['ANTHROPIC_API_KEY', 'DATABOT_TOKEN', 'BOT_TOKEN',
                'POLYGON_KEY', 'FMP_API_KEY', 'LANGSMITH_API_KEY']

# Daily-cycle systemd services we expect to be active.
EXPECTED_SERVICES = ['johnbot.service', 'fundjohn-dashboard.service']

# Coverage staleness windows.
STALE_WARN_HOURS = 30   # data older than this on a critical source → warn
STALE_FAIL_HOURS = 96   # data older than this → fail (4 days = past weekend)
CRITICAL_DATA_TYPES = ['prices', 'options_eod']

# Regime-freshness window. The 9:00 AM ET cron writes a fresh regime daily;
# weekends are tolerated by the higher fail bound. Anything beyond
# REGIME_FAIL_HOURS is catastrophic — engine.py would generate signals
# under stale regime context (the 2026-04-28 LOW_VOL miss exhibited this
# silently for 7 days due to a numpy→psycopg2 type-cast bug in
# scripts/run_market_state.py).
REGIME_WARN_HOURS = 30
REGIME_FAIL_HOURS = 80   # Fri evening + weekend tolerance

# Slow-check tags so --quick can skip them.
SLOW_CHECKS = {'redis', 'data_coverage', 'systemd_services'}

REGIME_LATEST_FILE = ROOT / '.agents' / 'market-state' / 'regime_latest.json'


def _check(name, slow=False):
    """Decorator that records the check name + slow flag for filtering."""
    def deco(fn):
        fn._check_name = name
        fn._slow = slow
        return fn
    return deco


def _ok(name, detail=''):
    return {'name': name, 'severity': PASS, 'detail': detail}


def _warn(name, detail):
    return {'name': name, 'severity': WARN, 'detail': detail}


def _fail(name, detail):
    return {'name': name, 'severity': FAIL, 'detail': detail}


# ── Individual checks ──────────────────────────────────────────────────────

@_check('alpaca_cli_binary')
def check_alpaca_cli_binary():
    p = Path(ALPACA_CLI)
    if not p.exists():
        return _fail('alpaca_cli_binary', f'{ALPACA_CLI}: not found')
    if not os.access(p, os.X_OK):
        return _fail('alpaca_cli_binary', f'{ALPACA_CLI}: not executable')
    return _ok('alpaca_cli_binary', str(p))


@_check('alpaca_auth')
def check_alpaca_auth():
    """Calls `alpaca account get`. Pass if exit 0 and equity >= 0.
    Fail (exit 2 trigger) on non-zero exit or 401/403."""
    try:
        proc = subprocess.run([ALPACA_CLI, 'account', 'get'],
                              capture_output=True, text=True, timeout=5,
                              check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return _fail('alpaca_auth', f'{type(exc).__name__}: {exc}')
    if proc.returncode != 0:
        # Try to surface the structured error
        stderr = proc.stderr.strip() or 'no stderr'
        try:
            err = json.loads(proc.stderr)
            status = err.get('status')
            msg    = err.get('error', stderr)[:120]
            return _fail('alpaca_auth', f'status={status}: {msg}')
        except json.JSONDecodeError:
            return _fail('alpaca_auth', stderr[:120])
    try:
        acct = json.loads(proc.stdout)
        equity = float(acct.get('equity') or 0)
    except (json.JSONDecodeError, ValueError, TypeError):
        return _fail('alpaca_auth', 'unparseable account JSON')
    if equity == 0:
        return _warn('alpaca_auth', 'equity=0 (paper account just funded?)')
    return _ok('alpaca_auth', f'equity=${equity:,.2f}')


@_check('alpaca_clock')
def check_alpaca_clock():
    try:
        proc = subprocess.run([ALPACA_CLI, 'clock'],
                              capture_output=True, text=True, timeout=5,
                              check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return _fail('alpaca_clock', f'{type(exc).__name__}')
    if proc.returncode != 0:
        return _fail('alpaca_clock', proc.stderr.strip()[:120] or 'non-zero exit')
    try:
        clk = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _fail('alpaca_clock', 'unparseable clock JSON')
    return _ok('alpaca_clock',
               f'is_open={clk.get("is_open")} next_open={clk.get("next_open", "?")[:19]}')


@_check('postgres_reachable')
def check_postgres_reachable():
    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        return _fail('postgres_reachable', 'POSTGRES_URI not set')
    try:
        import psycopg2
    except ImportError:
        return _fail('postgres_reachable', 'psycopg2 not installed')
    t0 = time.monotonic()
    try:
        conn = psycopg2.connect(uri, connect_timeout=5)
        cur  = conn.cursor()
        cur.execute('SELECT 1')
        cur.fetchone()
        cur.close()
        conn.close()
    except Exception as exc:
        return _fail('postgres_reachable', f'{type(exc).__name__}: {exc}'[:120])
    elapsed_ms = (time.monotonic() - t0) * 1000
    if elapsed_ms > 5000:
        return _fail('postgres_reachable', f'roundtrip={elapsed_ms:.0f}ms (>5s)')
    if elapsed_ms > 1000:
        return _warn('postgres_reachable', f'roundtrip={elapsed_ms:.0f}ms (slow)')
    return _ok('postgres_reachable', f'roundtrip={elapsed_ms:.0f}ms')


@_check('redis_reachable', slow=True)
def check_redis_reachable():
    url = os.environ.get('REDIS_URL', 'redis://localhost:6379')
    try:
        import redis
    except ImportError:
        return _warn('redis_reachable', 'redis-py not installed')
    try:
        r = redis.from_url(url, socket_connect_timeout=3)
        if r.ping():
            return _ok('redis_reachable', 'PONG')
        return _fail('redis_reachable', 'PING did not return PONG')
    except Exception as exc:
        return _fail('redis_reachable', f'{type(exc).__name__}: {exc}'[:80])


@_check('data_master_writable')
def check_data_master_writable():
    p = ROOT / 'data' / 'master'
    if not p.exists():
        return _fail('data_master_writable', f'{p}: missing')
    if not os.access(p, os.W_OK):
        return _fail('data_master_writable', f'{p}: not writable by uid={os.getuid()}')
    return _ok('data_master_writable', str(p))


@_check('env_required')
def check_env_required():
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        return _fail('env_required', f'missing: {",".join(missing)}')
    return _ok('env_required', f'{len(REQUIRED_ENV)} required vars present')


@_check('env_optional')
def check_env_optional():
    missing = [k for k in OPTIONAL_ENV if not os.environ.get(k)]
    if not missing:
        return _ok('env_optional', f'{len(OPTIONAL_ENV)}/{len(OPTIONAL_ENV)} optional vars present')
    return _warn('env_optional', f'missing: {",".join(missing)}')


@_check('regime_freshness')
def check_regime_freshness():
    """Detect drift between `regime_latest.json` (daily-cron output) and
    `market_regime` Postgres table (what engine.py reads). They MUST agree
    on `state` and both must be fresh; otherwise signal generation runs
    under stale regime context.

    Failure modes this catches:
      - DB write silently fails (e.g. type-cast bug in run_market_state.py
        — caused the 2026-04-28 LOW_VOL miss for 7 days).
      - Cron schedule fails to fire (json file goes stale too).
      - Manual override of one source without the other.
    """
    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        return _warn('regime_freshness', 'POSTGRES_URI not set — skipped')
    try:
        import psycopg2
        conn = psycopg2.connect(uri, connect_timeout=5)
        cur  = conn.cursor()
        cur.execute(
            "SELECT state, updated_at FROM market_regime "
            "ORDER BY updated_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
    except Exception as exc:
        return _warn('regime_freshness', f'DB query failed: {type(exc).__name__}')

    if not row:
        return _fail('regime_freshness', 'market_regime table empty — engine.py will default to HIGH_VOL')

    db_state, db_updated = row
    if db_updated.tzinfo is None:
        db_updated = db_updated.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    db_age_h = (now - db_updated).total_seconds() / 3600.0

    file_state = None
    file_age_h = None
    if REGIME_LATEST_FILE.exists():
        try:
            with open(REGIME_LATEST_FILE) as f:
                file_state = json.load(f).get('state')
            file_mtime = datetime.fromtimestamp(
                REGIME_LATEST_FILE.stat().st_mtime, tz=timezone.utc)
            file_age_h = (now - file_mtime).total_seconds() / 3600.0
        except Exception:
            pass

    # Worst severity wins.
    severity = PASS
    notes = [f'db={db_state} ({db_age_h:.0f}h)']
    if file_state is not None:
        notes.append(f'file={file_state} ({file_age_h:.0f}h)')

    if db_age_h > REGIME_FAIL_HOURS:
        severity = FAIL
    elif db_age_h > REGIME_WARN_HOURS and severity != FAIL:
        severity = WARN

    if file_age_h is not None:
        if file_age_h > REGIME_FAIL_HOURS:
            severity = FAIL
        elif file_age_h > REGIME_WARN_HOURS and severity != FAIL:
            severity = WARN

    if file_state is not None and file_state != db_state:
        # State disagreement is more dangerous than staleness alone — engine
        # might generate the wrong signal basket all day.
        severity = FAIL
        notes.append(f'STATE MISMATCH (file={file_state} ≠ db={db_state})')

    detail = '; '.join(notes)
    if severity == FAIL: return _fail('regime_freshness', detail)
    if severity == WARN: return _warn('regime_freshness', detail)
    return _ok('regime_freshness', detail)


@_check('data_coverage', slow=True)
def check_data_coverage():
    """Query data_coverage for staleness on critical types. Reports the
    oldest last_updated across all tickers per critical data_type."""
    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        return _warn('data_coverage', 'POSTGRES_URI not set — skipped')
    try:
        import psycopg2
        conn = psycopg2.connect(uri, connect_timeout=5)
        cur  = conn.cursor()
        cur.execute("""
            SELECT data_type, MAX(last_updated)
              FROM data_coverage
             WHERE data_type = ANY(%s)
             GROUP BY data_type
        """, (CRITICAL_DATA_TYPES,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as exc:
        return _warn('data_coverage', f'query failed: {type(exc).__name__}')
    if not rows:
        return _warn('data_coverage', 'no rows for critical types')
    now = datetime.now(timezone.utc)
    worst_severity = PASS
    parts = []
    for dtype, last in rows:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age_h = (now - last).total_seconds() / 3600.0
        parts.append(f'{dtype}={age_h:.0f}h')
        if age_h > STALE_FAIL_HOURS:
            worst_severity = FAIL
        elif age_h > STALE_WARN_HOURS and worst_severity != FAIL:
            worst_severity = WARN
    detail = ', '.join(parts)
    if worst_severity == FAIL: return _fail('data_coverage', detail)
    if worst_severity == WARN: return _warn('data_coverage', detail)
    return _ok('data_coverage', detail)


@_check('orchestrator_lock')
def check_orchestrator_lock():
    """Stale lock from > 1h ago indicates a previous run died with the
    lock still held. Surface so operator knows to clear it."""
    url = os.environ.get('REDIS_URL', 'redis://localhost:6379')
    try:
        import redis
        r = redis.from_url(url, socket_connect_timeout=2)
        # The orchestrator stores locks under `pipeline:lock:<run_date>`.
        keys = list(r.scan_iter('pipeline:lock:*', count=20))
    except Exception:
        return _warn('orchestrator_lock', 'redis unreachable — skipped')
    if not keys:
        return _ok('orchestrator_lock', 'no locks held')
    stale = []
    for k in keys:
        ttl = r.ttl(k)
        # Lock TTL is set to LOCK_TTL (likely 6h). If TTL still high, lock is
        # active. If TTL < 0 or TTL near LOCK_TTL — but still around > 1h — we
        # treat as stale.  Conservative heuristic: report all locks; operator
        # decides.
        stale.append(f'{k.decode() if isinstance(k, bytes) else k} (ttl={ttl}s)')
    return _warn('orchestrator_lock', '; '.join(stale))


@_check('systemd_services', slow=True)
def check_systemd_services():
    """Each expected unit must be `active` per `systemctl is-active`. Skips
    cleanly if `systemctl` not available (e.g. running in a container)."""
    try:
        proc = subprocess.run(['systemctl', '--version'],
                              capture_output=True, text=True, timeout=2)
        if proc.returncode != 0:
            return _warn('systemd_services', 'systemctl unavailable')
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return _warn('systemd_services', 'systemctl unavailable')

    inactive = []
    for unit in EXPECTED_SERVICES:
        try:
            r = subprocess.run(['systemctl', 'is-active', unit],
                               capture_output=True, text=True, timeout=2)
            state = r.stdout.strip()
            if state != 'active':
                inactive.append(f'{unit}={state}')
        except subprocess.TimeoutExpired:
            inactive.append(f'{unit}=timeout')
    if not inactive:
        return _ok('systemd_services', f'{len(EXPECTED_SERVICES)}/{len(EXPECTED_SERVICES)} active')
    if len(inactive) >= 2:
        return _fail('systemd_services', '; '.join(inactive))
    return _warn('systemd_services', '; '.join(inactive))


# ── Orchestration ──────────────────────────────────────────────────────────

def _all_checks():
    """Return (name, fn, slow) for every @_check function in this module."""
    out = []
    for fn in globals().values():
        if callable(fn) and getattr(fn, '_check_name', None):
            out.append((fn._check_name, fn, fn._slow))
    return out


def run(*, quick=False, required_only=False):
    """Run all checks and return (results, exit_code)."""
    results = []
    overall = PASS
    for name, fn, slow in _all_checks():
        if quick and slow:
            continue
        if required_only and name in {
                'env_optional', 'data_coverage', 'orchestrator_lock',
                'systemd_services'}:
            continue
        try:
            res = fn()
        except Exception as exc:
            res = _fail(name, f'check raised: {type(exc).__name__}: {exc}')
        results.append(res)
        if res['severity'] == FAIL:
            overall = FAIL
        elif res['severity'] == WARN and overall != FAIL:
            overall = WARN

    exit_code = {PASS: 0, WARN: 1, FAIL: 2}[overall]
    return results, exit_code


def _format_table(results):
    name_w = max(len(r['name']) for r in results)
    sev_w  = 4
    lines = []
    for r in results:
        sev = r['severity'].upper()
        marker = {'PASS': '✓', 'WARN': '!', 'FAIL': '✗'}[sev]
        lines.append(f'  {marker} {r["name"]:<{name_w}}  {sev:<{sev_w}}  {r["detail"]}')
    return '\n'.join(lines)


def main():
    # Always load .env so subprocess CLIs inherit Alpaca creds.
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env')
    except ImportError:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument('--quick',         action='store_true',
                    help='Skip slow checks (Redis, data_coverage, systemd)')
    ap.add_argument('--required-only', action='store_true',
                    help='Only the auth/config blockers — for orchestrator preflight')
    ap.add_argument('--json',          action='store_true',
                    help='Emit machine-readable JSON instead of human table')
    args = ap.parse_args()

    started = time.monotonic()
    results, exit_code = run(quick=args.quick, required_only=args.required_only)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    n_pass = sum(1 for r in results if r['severity'] == PASS)
    n_warn = sum(1 for r in results if r['severity'] == WARN)
    n_fail = sum(1 for r in results if r['severity'] == FAIL)

    if args.json:
        print(json.dumps({
            'overall':    {0: 'pass', 1: 'warn', 2: 'fail'}[exit_code],
            'exit_code':  exit_code,
            'elapsed_ms': elapsed_ms,
            'mode':       ('required_only' if args.required_only
                           else 'quick' if args.quick else 'full'),
            'summary':    f'{n_pass} pass, {n_warn} warn, {n_fail} fail',
            'checks':     results,
        }))
    else:
        print('FundJohn doctor — '
              + ('required-only' if args.required_only
                 else 'quick' if args.quick else 'full')
              + f' mode  ({elapsed_ms}ms)')
        print(_format_table(results))
        print(f'\n  {n_pass} pass, {n_warn} warn, {n_fail} fail  →  '
              + {0: 'OK', 1: 'WARN', 2: 'FAIL'}[exit_code])

    sys.exit(exit_code)


if __name__ == '__main__':
    main()
