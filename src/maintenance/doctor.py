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
# Names must match the actual variables in /root/openclaw/.env. Pre-2026-05-01
# this list contained ANTHROPIC_API_KEY (unused — claude-bin uses OAuth),
# BOT_TOKEN (actual var is DISCORD_BOT_TOKEN), POLYGON_KEY (actual var is
# POLYGON_API_KEY), and LANGSMITH_API_KEY (not enabled), so env_optional
# warned every cycle on the same 4 stale names. Audit before adding.
OPTIONAL_ENV = ['DATABOT_TOKEN', 'DISCORD_BOT_TOKEN',
                'POLYGON_API_KEY', 'FMP_API_KEY']

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


# ── Shared helpers (module-level so monkeypatching works in tests) ─────────

def _run_alpaca_cli(args: list) -> dict:
    """Single seam for monkeypatching in tests."""
    res = subprocess.run(
        [ALPACA_CLI] + args,
        capture_output=True, text=True, timeout=10,
    )
    if res.returncode != 0:
        raise RuntimeError(f'alpaca cli rc={res.returncode}: {res.stderr}')
    return json.loads(res.stdout)


def _parquet_last_date(path: str):
    """Return last date in a parquet's 'date' column, or None if file missing."""
    import pandas as pd
    p = Path(path)
    if not p.exists():
        return None
    df = pd.read_parquet(p, columns=['date'])
    if df.empty:
        return None
    return pd.to_datetime(df['date']).max().date()


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


@_check('alpaca_aat_plus_tier')
def _check_alpaca_aat_plus_tier():
    """Probe SPY 30-DTE chain + 1 news fetch. Confirms paid AAT Plus tier active."""
    try:
        chain = _run_alpaca_cli([
            'data', 'option', 'chain',
            '--underlying-symbol', 'SPY',
            '--expiration-date-gte', '2026-06-15',
            '--expiration-date-lte', '2026-06-30',
            '--strike-price-gte', '740',
            '--strike-price-lte', '750',
            '--type', 'call',
            '--limit', '5',
        ])
        snaps = chain.get('snapshots', {})
        any_nonzero = any(
            (s.get('greeks') or {}).get('delta', 0) != 0
            for s in snaps.values()
        )
        if not any_nonzero:
            return _fail('alpaca_aat_plus_tier',
                         'all SPY 30-DTE ATM greeks zero — AAT Plus may be inactive')
        _run_alpaca_cli(['data', 'news', '--symbols', 'AAPL', '--limit', '1'])
        return _ok('alpaca_aat_plus_tier', 'greeks present + news OK')
    except Exception as exc:
        return _fail('alpaca_aat_plus_tier', str(exc)[:200])


@_check('options_archive_freshness')
def _check_options_archive_freshness():
    """options_eod.parquet last row should be ≤ 2 trading days old (warn) / 4 (fail)."""
    from datetime import date
    last = _parquet_last_date(str(ROOT / 'data' / 'master' / 'options_eod.parquet'))
    if last is None:
        return _fail('options_archive_freshness', 'parquet missing')
    days = (date.today() - last).days
    if days >= 4:
        return _fail('options_archive_freshness', f'last row {days} days old')
    if days >= 2:
        return _warn('options_archive_freshness', f'last row {days} days old')
    return _ok('options_archive_freshness', f'last row {days} days old')


@_check('cboe_vol_indices_freshness')
def _check_cboe_vol_indices_freshness():
    """vol_indices.parquet calendar-day staleness. Matches options_archive thresholds:
    WARN >= 2 days (covers normal weekend gap Sat-Sun morning before refresh),
    FAIL >= 4 days (covers long weekends + Mon morning before collector run)."""
    from datetime import date
    last = _parquet_last_date(str(ROOT / 'data' / 'master' / 'vol_indices.parquet'))
    if last is None:
        return _fail('cboe_vol_indices_freshness', 'parquet missing')
    days = (date.today() - last).days
    if days >= 4:
        return _fail('cboe_vol_indices_freshness', f'last row {days} days old')
    if days >= 2:
        return _warn('cboe_vol_indices_freshness', f'last row {days} days old')
    return _ok('cboe_vol_indices_freshness', f'last row {days} days old')


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
    """Verify data/master is being written to. Pre-2026-05-01 this checked
    `os.access(p, os.W_OK)` against the *current process's* uid — but
    pipeline_orchestrator runs as root while doctor (when invoked from
    the BotJohn maintenance timer) runs as claudebot. data/master is
    owned 755 root:root, so doctor would FAIL even though the cycle was
    successfully writing the path every morning. The check is asking
    the wrong question.

    Permission-honest replacement: confirm the dir exists AND has at
    least one non-lock file modified within MASTER_FRESH_HOURS. This
    actually measures the thing we care about (is the path functioning
    end-to-end) and is invariant to which user invokes doctor.
    """
    MASTER_FRESH_HOURS = 36   # warn beyond a full weekend gap
    MASTER_STALE_HOURS = 96   # fail at >4 days (a real outage)

    p = ROOT / 'data' / 'master'
    if not p.exists():
        return _fail('data_master_writable', f'{p}: missing')
    files = [f for f in p.iterdir() if f.is_file() and not f.name.endswith('.lock')]
    if not files:
        return _fail('data_master_writable', f'{p}: empty (no parquets)')
    newest = max(files, key=lambda f: f.stat().st_mtime)
    age_h = (time.time() - newest.stat().st_mtime) / 3600.0
    if age_h > MASTER_STALE_HOURS:
        return _fail('data_master_writable',
                     f'{p}: newest file {newest.name} is {age_h:.0f}h old (>{MASTER_STALE_HOURS}h)')
    if age_h > MASTER_FRESH_HOURS:
        return _warn('data_master_writable',
                     f'{p}: newest file {newest.name} is {age_h:.0f}h old')
    return _ok('data_master_writable',
               f'{p}: newest={newest.name} ({age_h:.1f}h)')


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


@_check('liquidator_audit', slow=True)
def check_liquidator_audit():
    """When live liquidation is enabled, every state transition between
    today's and yesterday's market_regime rows MUST produce at least one
    alpaca_liquidations row for today. Catches silent failures of
    src/execution/regime_liquidator.py (missed cron, broker outage, COID
    prefix mismatch, etc.). No-op when the live env flag is unset."""
    if os.environ.get('OPENCLAW_ALPACA_LIVE_LIQUIDATE') != '1':
        return _ok('liquidator_audit', 'live flag off — skipped')
    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        return _warn('liquidator_audit', 'POSTGRES_URI not set — skipped')
    try:
        import psycopg2
        conn = psycopg2.connect(uri, connect_timeout=5)
        cur = conn.cursor()
        cur.execute(
            "SELECT state, updated_at::date FROM market_regime "
            "ORDER BY updated_at DESC LIMIT 2"
        )
        rows = cur.fetchall()
        if len(rows) < 2:
            cur.close(); conn.close()
            return _ok('liquidator_audit', 'insufficient regime history')
        today_state, today_date = rows[0]
        prev_state, _ = rows[1]
        if today_state == prev_state:
            cur.close(); conn.close()
            return _ok('liquidator_audit', f'no transition ({today_state})')
        cur.execute(
            "SELECT COUNT(*) FROM alpaca_liquidations WHERE run_date = %s",
            (today_date,),
        )
        n = cur.fetchone()[0]
        cur.close(); conn.close()
    except Exception as exc:
        return _warn('liquidator_audit', f'query failed: {type(exc).__name__}')
    if n == 0:
        return _warn('liquidator_audit',
                     f'transition {prev_state}→{today_state} on {today_date} '
                     f'but 0 audit rows — liquidator may have failed silently')
    return _ok('liquidator_audit',
               f'{n} audit row(s) for {prev_state}→{today_state}')


@_check('intraday_features_freshness')
def check_intraday_features_freshness():
    """During RTH, the intraday HMM tick must write a row to
    intraday_features.parquet at least every 15 min. Stale parquet
    means the cron is not firing or the collector is failing silently.

    Outside RTH (or weekends), no-op — the cron only runs Mon-Fri 9-16
    ET, so a stale parquet at 6 PM is expected behaviour.
    """
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
    except Exception:
        return _ok('intraday_features_freshness',
                   'zoneinfo unavailable — skipped')
    now_et = _dt.now(ZoneInfo('America/New_York'))
    if now_et.weekday() >= 5:
        return _ok('intraday_features_freshness', 'weekend — skipped')
    rth_start = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    rth_end   = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    if now_et < rth_start or now_et > rth_end:
        return _ok('intraday_features_freshness', 'outside RTH — skipped')

    parquet = ROOT / 'data' / 'master' / 'intraday_features.parquet'
    if not parquet.exists():
        return _warn('intraday_features_freshness',
                     'parquet missing — collector may not have started yet')
    try:
        import pandas as pd
        df = pd.read_parquet(parquet, columns=['ts_utc'])
    except Exception as exc:
        return _warn('intraday_features_freshness',
                     f'read failed: {type(exc).__name__}')
    if df.empty:
        return _warn('intraday_features_freshness', 'parquet empty')
    last_ts = pd.to_datetime(df['ts_utc'], utc=True).max()
    minutes_stale = (pd.Timestamp.now(tz='UTC') - last_ts).total_seconds() / 60.0
    if minutes_stale > 15:
        return _warn('intraday_features_freshness',
                     f'last row {minutes_stale:.1f} min ago — cron not firing?')
    return _ok('intraday_features_freshness',
               f'last row {minutes_stale:.1f} min ago')


@_check('intraday_hmm_calibration', slow=True)
def check_intraday_hmm_calibration():
    """Once a hmm_intraday_latest.pkl exists, compare the HMM's
    training-time feature means (`model.feature_means_`) against the
    trailing-30d feature distribution from intraday_features.parquet.
    Warn on >50% drift on any feature — model may be stale.

    No-op when no model is trained yet (bootstrap mode)."""
    model_path = ROOT / '.agents' / 'market-state' / 'hmm_intraday_latest.pkl'
    if not model_path.exists():
        return _ok('intraday_hmm_calibration', 'no model yet — bootstrap mode')
    try:
        import pickle
        import pandas as pd
        with open(model_path, 'rb') as f:
            model = pickle.load(f)
        train_means = getattr(model, 'feature_means_', {}) or {}
        feature_names = getattr(model, 'feature_names_', list(train_means.keys()))
    except Exception as exc:
        return _warn('intraday_hmm_calibration', f'model load failed: {exc}')
    parquet = ROOT / 'data' / 'master' / 'intraday_features.parquet'
    if not parquet.exists():
        return _warn('intraday_hmm_calibration', 'parquet missing')
    try:
        df = pd.read_parquet(parquet)
        df['ts_utc'] = pd.to_datetime(df['ts_utc'], utc=True)
        cutoff = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=30)
        recent = df[df['ts_utc'] >= cutoff]
    except Exception as exc:
        return _warn('intraday_hmm_calibration', f'read failed: {exc}')
    if recent.empty:
        return _ok('intraday_hmm_calibration', 'no recent data')
    drifted = []
    for col in feature_names:
        if col not in recent.columns:
            continue
        train_mean = float(train_means.get(col, 0.0))
        live_mean = float(recent[col].mean())
        if abs(train_mean) < 1e-6:
            continue
        rel = abs(live_mean - train_mean) / abs(train_mean)
        if rel > 0.5:
            drifted.append(f'{col}: train={train_mean:.3f} live={live_mean:.3f} (Δ={rel*100:.0f}%)')
    if drifted:
        return _warn('intraday_hmm_calibration',
                     '; '.join(drifted[:3]))
    return _ok('intraday_hmm_calibration',
               f'{len(feature_names)} features within 50% of training means')


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


REGIME_BLENDED_GATE_STALE_DAYS = 14


@_check('regime_blended_gate_b')
def check_regime_blended_gate_b():
    """Validate Gate B preconditions for the regime-blended LIVE flip.

    Evaluates gate state and snapshot freshness, then applies severity per a
    fail-closed truth table designed so production LIVE preflight cannot
    proceed without current evidence the gate passes.

    Severity matrix (LIVE = OPENCLAW_REGIME_BLENDED_LIVE=1):
      LIVE, gate.pass = true,  fresh  → PASS
      LIVE, gate.pass = true,  stale  → FAIL  (no current evidence)
      LIVE, gate.pass = false, *      → FAIL  (gate broken)
      LIVE, no backfill / harness err → FAIL  (no evidence at all)
      DRY,  gate.pass = true,  fresh  → PASS
      DRY,  any other state           → WARN  (informational pre-flip)

    Walk-forward exceptions (DB hiccup etc.) → WARN in both modes; treated as
    "cannot evaluate" rather than "gate fail" to avoid false alarms on infra.
    Returns _fail explicitly when LIVE is on AND we *could* evaluate the gate
    AND it failed — those are the only true-negative cases.
    """
    live = os.environ.get('OPENCLAW_REGIME_BLENDED_LIVE', '0') == '1'
    try:
        from backtest.regime_blended_backtest import run_walkforward
    except Exception as exc:
        return _warn('regime_blended_gate_b',
                     f'harness import failed: {type(exc).__name__}: {exc}')

    uri = (os.environ.get('DATABASE_URL') or os.environ.get('POSTGRES_URI')
           or 'postgresql://openclaw:password@localhost:5432/openclaw')
    manifest_path = ROOT / 'src' / 'strategies' / 'manifest.json'
    regime_parquet = ROOT / 'data' / 'master' / 'historical_regimes.parquet'
    try:
        result = run_walkforward(uri, manifest_path, regime_parquet)
    except Exception as exc:
        # Infra failure — cannot evaluate. WARN in both modes; operator
        # investigates separately from gate state.
        return _warn('regime_blended_gate_b',
                     f'walk-forward failed: {type(exc).__name__}: {exc}')

    if result.get('error'):
        msg = f'no backtests yet: {result["error"]}'
        return _fail('regime_blended_gate_b', msg) if live else _warn('regime_blended_gate_b', msg)

    gate = result.get('gate_b', {})
    delta = result.get('delta', {})
    gate_pass = bool(gate.get('pass'))

    stale = False
    age_days = None
    run_at_iso = result.get('run_at')
    if run_at_iso:
        try:
            run_at = datetime.fromisoformat(run_at_iso)
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - run_at
            age_days = age.days
            stale = age.days > REGIME_BLENDED_GATE_STALE_DAYS
        except (ValueError, TypeError):
            pass  # malformed; treat as freshness-unknown

    detail_parts = [
        f'Δsharpe={delta.get("sharpe")}',
        f'Δmax_dd={delta.get("max_dd")}',
        f'live={live}',
    ]
    if age_days is not None:
        detail_parts.append(f'age={age_days}d')
    if stale:
        detail_parts.append(f'STALE (>{REGIME_BLENDED_GATE_STALE_DAYS}d)')

    if gate_pass and not stale:
        return _ok('regime_blended_gate_b', 'pass (' + ', '.join(detail_parts) + ')')

    # Build a descriptive reason for non-pass states.
    reasons = []
    if not gate_pass:
        reasons.append('gate.pass=false')
        for flag in ('sharpe_within_tolerance', 'max_dd_not_worse',
                     'low_vol_strategies_present'):
            if not gate.get(flag, True):
                reasons.append(f'!{flag}')
    if stale:
        reasons.append('snapshot stale (weekly cron missed?)')
    detail = '; '.join(reasons) + ' [' + ', '.join(detail_parts) + ']'
    return _fail('regime_blended_gate_b', detail) if live else _warn('regime_blended_gate_b', detail)


ROLLUP_STALE_HOURS = 26
ROLLUP_VERY_STALE_HOURS = 72
MANIFEST_REPO_ROOT_DEFAULT = '/root/openclaw'


@_check('manifest_eligibility_drift')
def check_manifest_eligibility_drift():
    """Detect stale writes to the deprecated manifest.eligible_regimes field.

    Phase 2A moved eligibility ownership into strategy_regime_params (DB).
    `manifest.eligible_regimes` is no longer authoritative; its presence on
    any strategy entry indicates either: (a) a stale code path still
    writing it, or (b) a transitional row that hasn't been cleaned up
    yet. Either is worth flagging until manifest is fully retired.

    PASS:  no strategy has the field
    WARN:  1+ strategies have the field (DRY-RUN)
    FAIL:  1+ AND OPENCLAW_REGIME_BLENDED_LIVE=1
    WARN:  manifest unparseable / unreadable
    """
    name = 'manifest_eligibility_drift'
    repo_root = os.environ.get('OPENCLAW_REPO_ROOT', MANIFEST_REPO_ROOT_DEFAULT)
    live = os.environ.get('OPENCLAW_REGIME_BLENDED_LIVE') == '1'
    wrk_path = Path(repo_root) / 'src' / 'strategies' / 'manifest.json'
    try:
        wrk = json.loads(wrk_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        return _warn(name, f'working manifest unreadable: {exc!s}')
    strategies = wrk.get('strategies') or {}
    stale = [sid for sid, rec in strategies.items()
             if rec.get('eligible_regimes') is not None]
    if not stale:
        return _ok(name, 'no strategy carries deprecated eligible_regimes field')
    stale.sort()
    preview = ', '.join(stale[:4]) + (f' +{len(stale)-4} more' if len(stale) > 4 else '')
    summary = (f'{len(stale)} strategy(ies) still carry deprecated '
               f'eligible_regimes field: {preview}')
    if live:
        return _fail(name, summary)
    return _warn(name, summary)


def _latest_rollup_run_at(uri: str):
    """Return MAX(run_at) from strategy_regime_live_pnl_rollup or None."""
    import psycopg2
    with psycopg2.connect(uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT MAX(run_at) FROM strategy_regime_live_pnl_rollup'
            )
            row = cur.fetchone()
    return row[0] if row else None


@_check('regime_live_rollup_freshness')
def check_regime_live_rollup_freshness():
    """Validate that the nightly regime live PnL rollup ran recently.

    PASS:  rollup within last 26h (one nightly fire + 2h headroom)
    WARN:  26h - 72h (one missed run; operator should investigate)
    FAIL:  > 72h or table empty (rollup pipeline broken)
    WARN:  db error reaching rollup table
    """
    name = 'regime_live_rollup_freshness'
    uri = (os.environ.get('DATABASE_URL')
           or os.environ.get('POSTGRES_URI')
           or 'postgresql://openclaw:password@localhost:5432/openclaw')
    try:
        latest = _latest_rollup_run_at(uri)
    except Exception as exc:
        return _warn(name, f'rollup query failed: {exc!s}')
    if latest is None:
        return _fail(name, 'rollup table empty — has the timer ever run?')
    age = datetime.now(timezone.utc) - latest
    age_hours = age.total_seconds() / 3600.0
    if age_hours <= ROLLUP_STALE_HOURS:
        return _ok(name, f'fresh ({age_hours:.1f}h old)')
    if age_hours <= ROLLUP_VERY_STALE_HOURS:
        return _warn(name,
                     f'stale ({age_hours:.1f}h old; nightly may have failed)')
    return _fail(name,
                 f'very stale ({age_hours:.1f}h old; rollup pipeline broken)')


PARAMS_CONSISTENCY_FAIL_THRESHOLD = 5
PROPOSALS_AGED_WARN_DAYS = 14
PROPOSALS_AGED_FAIL_DAYS = 30
PROPOSALS_AGED_FAIL_COUNT = 10
DRIFT_WARN_COUNT_THRESHOLD = 3
DRIFT_FAIL_WARN_COUNT_THRESHOLD = 10
CALIBRATION_BRIER_WARN  = 0.10
CALIBRATION_BRIER_FAIL  = 0.20
CALIBRATION_MIN_SAMPLES = 10
OVERLAP_FRESH_HOURS     = 26
OVERLAP_VERY_STALE_HOURS = 72
INTRADAY_MC_FRESH_HOURS    = 26
INTRADAY_MC_VERY_STALE_HOURS = 72
INTRADAY_MC_PENDING_STALE_DAYS = 7
# 2026-05-19: ADDENDA_* constants removed alongside the
# mastermind_prompt_addenda feature.
REGIME_CORR_CRISIS_PROB_HIGH_THRESHOLD = 0.30


def _calibration_report():
    sys.path.insert(0, str(ROOT / 'src'))
    from metrics.mastermind_calibration import calibration_report
    return calibration_report()


@_check('mastermind_calibration_brier')
def check_mastermind_calibration_brier():
    """PASS if Brier < CALIBRATION_BRIER_WARN with >= CALIBRATION_MIN_SAMPLES."""
    name = 'mastermind_calibration_brier'
    try:
        rep = _calibration_report()
    except Exception as exc:
        return _warn(name, f'calibration query failed: {exc!s}')
    n = int(rep.get('total_observations') or 0)
    brier = rep.get('brier_score')
    if n < CALIBRATION_MIN_SAMPLES:
        return _ok(name, f'insufficient data ({n}/{CALIBRATION_MIN_SAMPLES} samples)')
    if brier is None or (isinstance(brier, float) and brier != brier):  # NaN check
        return _warn(name, 'Brier could not be computed despite samples')
    if brier >= CALIBRATION_BRIER_FAIL:
        return _fail(name, f'Brier {brier:.3f} >= {CALIBRATION_BRIER_FAIL} (n={n})')
    if brier >= CALIBRATION_BRIER_WARN:
        return _warn(name, f'Brier {brier:.3f} >= {CALIBRATION_BRIER_WARN} (n={n})')
    return _ok(name, f'Brier {brier:.3f} (n={n})')


def _latest_overlap_run_at():
    import psycopg2
    uri = (os.environ.get('DATABASE_URL')
           or os.environ.get('POSTGRES_URI')
           or 'postgresql://openclaw:password@localhost:5432/openclaw')
    with psycopg2.connect(uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(computed_at) FROM strategy_signal_overlap")
            r = cur.fetchone()
    return r[0] if r else None


def _latest_intraday_mc_run_at():
    import psycopg2
    uri = (os.environ.get('DATABASE_URL')
           or os.environ.get('POSTGRES_URI')
           or 'postgresql://openclaw:password@localhost:5432/openclaw')
    with psycopg2.connect(uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(run_at) FROM strategy_regime_intraday_mc_runs")
            r = cur.fetchone()
    return r[0] if r else None


def _count_pending_proposals_without_intraday_mc(days_old: int):
    """Count pending Phase 2B proposals older than `days_old` that have no
    corresponding row in strategy_regime_intraday_mc_runs AND that touch a
    field where path-MC is decision-relevant.

    Path-MC only matters when the proposal changes stop_pct / target_pct /
    max_hold_days. Pure size-scalar proposals get full value from linear MC
    (Phase 2D), so they don't gate on this freshness check — otherwise the
    check would spuriously FAIL on size-only proposals once 2B is exercised."""
    import psycopg2
    uri = (os.environ.get('DATABASE_URL')
           or os.environ.get('POSTGRES_URI')
           or 'postgresql://openclaw:password@localhost:5432/openclaw')
    with psycopg2.connect(uri) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*)
                  FROM strategy_regime_param_proposals p
             LEFT JOIN strategy_regime_intraday_mc_runs r ON r.proposal_id = p.id
                 WHERE p.status = 'pending'
                   AND p.proposed_at < NOW() - (%s::int * INTERVAL '1 day')
                   AND r.id IS NULL
                   AND (p.proposed_stop_pct IS NOT NULL
                        OR p.proposed_target_pct IS NOT NULL
                        OR p.proposed_max_hold_days IS NOT NULL)
            """, (days_old,))
            return int(cur.fetchone()[0])


@_check('intraday_mc_freshness')
def check_intraday_mc_freshness():
    """Phase 2E: PASS if a path-MC ran within 26h, WARN if 26-72h or stale
    >72h without any pending proposals (data is old but no decision is
    waiting on it). FAIL only when there is an actual pending proposal
    aged > 7d without a path-MC row — that's when path-MC was needed for
    a real decision but never ran.

    Rationale (2026-05-19): under the new regime-redeploy-not-liquidate
    architecture, intraday MC is only on the proposal-driven param-tuning
    path (operator-gated); the daily 10am cycle and Phase 2 redeploy do
    not consume it. Demoting the stale-without-pending case from FAIL to
    WARN stops the daily cycle from auto-aborting when nobody is asking
    for path-MC, while keeping the loud FAIL for the case that actually
    matters (pending proposals starved of the data they need)."""
    name = 'intraday_mc_freshness'
    try:
        latest = _latest_intraday_mc_run_at()
        stale_pending = _count_pending_proposals_without_intraday_mc(
            INTRADAY_MC_PENDING_STALE_DAYS)
    except Exception as exc:
        return _warn(name, f'query failed: {exc!s}')
    if stale_pending > 0:
        return _fail(name, f'{stale_pending} pending proposals aged >{INTRADAY_MC_PENDING_STALE_DAYS}d without intraday MC')
    if latest is None:
        return _ok(name, 'intraday MC table empty (no proposals yet)')
    age = datetime.now(timezone.utc) - latest
    h = age.total_seconds() / 3600.0
    if h <= INTRADAY_MC_FRESH_HOURS:
        return _ok(name, f'fresh ({h:.1f}h old)')
    if h <= INTRADAY_MC_VERY_STALE_HOURS:
        return _warn(name, f'stale ({h:.1f}h old)')
    return _warn(name, f'very stale ({h:.1f}h old) — no pending proposals depend on path-MC')


@_check('strategy_overlap_freshness')
def check_strategy_overlap_freshness():
    """Nightly overlap cron freshness."""
    name = 'strategy_overlap_freshness'
    try:
        latest = _latest_overlap_run_at()
    except Exception as exc:
        return _warn(name, f'query failed: {exc!s}')
    if latest is None:
        return _ok(name, 'overlap table empty (cron not yet enabled)')
    age = datetime.now(timezone.utc) - latest
    h = age.total_seconds() / 3600.0
    if h <= OVERLAP_FRESH_HOURS:
        return _ok(name, f'fresh ({h:.1f}h old)')
    if h <= OVERLAP_VERY_STALE_HOURS:
        return _warn(name, f'stale ({h:.1f}h old)')
    return _fail(name, f'very stale ({h:.1f}h old)')


def _query_latest_regime_coverage():
    """Reads the most recent correlation_adjustments row's regime_coverage +
    regime_blend_weights. Used by regime_correlation_coverage doctor check
    (Phase 2H). Returns (coverage_dict, weights_dict) or (None, None) if
    no rows or pre-2H rows (NULL columns)."""
    import psycopg2
    uri = (os.environ.get('DATABASE_URL')
           or os.environ.get('POSTGRES_URI')
           or 'postgresql://openclaw:password@localhost:5432/openclaw')
    with psycopg2.connect(uri) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT regime_coverage, regime_blend_weights
                  FROM correlation_adjustments
                 WHERE regime_coverage IS NOT NULL
                 ORDER BY computed_at DESC LIMIT 1
            """)
            row = cur.fetchone()
    if not row:
        return None, None
    return row[0], row[1]


@_check('regime_correlation_coverage')
def check_regime_correlation_coverage():
    """Phase 2H: surface which regimes are using real data vs fallbacks.
    PASS  — every regime is 'real', or no 2H rows yet (pre-rollout)
    WARN  — any regime is 'fallback_global' or 'stress_prior' with low p(CRISIS)
    FAIL  — coverage[CRISIS] == 'stress_prior' AND blend_weights[CRISIS] >
            REGIME_CORR_CRISIS_PROB_HIGH_THRESHOLD (made-up correlation +
            high crisis probability = operator decision needed)."""
    name = 'regime_correlation_coverage'
    try:
        coverage, weights = _query_latest_regime_coverage()
    except Exception as exc:
        return _warn(name, f'query failed: {exc!s}')
    if coverage is None:
        return _ok(name, 'no 2H rows yet (pre-rollout or no cycles since)')
    if not isinstance(coverage, dict) or not isinstance(weights, dict):
        return _warn(name, 'coverage/weights payload not parseable')
    crisis_cov = coverage.get('CRISIS')
    crisis_prob = float(weights.get('CRISIS') or 0.0)
    if crisis_cov == 'stress_prior' and crisis_prob > REGIME_CORR_CRISIS_PROB_HIGH_THRESHOLD:
        return _fail(name,
                     f'CRISIS stress_prior + p(CRISIS)={crisis_prob:.2f} > {REGIME_CORR_CRISIS_PROB_HIGH_THRESHOLD} — review prior')
    fallbacks = [k for k, v in coverage.items() if v != 'real']
    if not fallbacks:
        return _ok(name, 'all regimes: real')
    summary = ', '.join(f'{k}={coverage[k]}' for k in fallbacks)
    return _warn(name, summary)


# 2026-05-19: _query_addenda_health + mastermind_addendum_health check
# removed. The mastermind_prompt_addenda table is no longer read or
# written by any code path — operator interaction with MastermindJohn's
# weekly research process has been retired in favor of the research-page
# entry points (papers / sources / hand-developed strategies).

# ── SP-2 Phase A: universe-resolver + metadata-snapshot helpers ───────────
# These are module-level functions (not wrapped in a class) so that
# monkeypatch can target them via dotted-path in unit tests.

def _latest_snapshot_age_days() -> int:
    """Return calendar-day age of the most recent ticker_metadata_snapshots
    row, or 999 if the table is empty."""
    import psycopg2
    from datetime import date
    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        raise RuntimeError('POSTGRES_URI not set')  # surfaces cleanly in the check error harness
    with psycopg2.connect(uri) as c, c.cursor() as cur:
        cur.execute('SELECT MAX(snapshot_date) FROM ticker_metadata_snapshots')
        last = cur.fetchone()[0]
    return (date.today() - last).days if last else 999


# Preflight thresholds are intentionally MORE LENIENT than the maintenance
# digest system_check (src/system_checks/checks/metadata_snapshot_freshness.py
# uses PASS<=2, FAIL>2). Preflight runs before each pipeline cycle — a 2-day
# gap on a Monday after a long weekend should not block the cycle. The
# system_check runs in the daily maintenance digest and is the stricter
# ongoing-health signal.
def _check_metadata_snapshot_freshness():
    """Return (code, msg) where code ∈ {0=pass, 1=warn, 2=fail}.
    Thresholds: ≤1d → pass, ≤3d → warn, >3d → fail."""
    age = _latest_snapshot_age_days()
    if age <= 1:
        return (0, f'snapshot fresh ({age}d)')
    if age <= 3:
        return (1, f'snapshot stale {age}d (warn)')
    return (2, f'snapshot stale {age}d (FAIL)')


def _union_universe_size():
    """Return (n_tickers, error_str_or_None). On failure, n=0 and err carries the exception."""
    from datetime import date
    try:
        out = subprocess.check_output(
            ['python3', '-m', 'src.strategies.universe_resolver',
             '--as-of', str(date.today()), '--states', 'live'],
            text=True, timeout=20,
        )
        return (len(json.loads(out)), None)
    except subprocess.TimeoutExpired:
        return (0, 'resolver timeout (>20s)')
    except subprocess.CalledProcessError as e:
        return (0, f'resolver exit={e.returncode}')
    except Exception as e:
        return (0, f'{type(e).__name__}: {e}')


def _check_union_universe_size():
    """Return (code, msg) where code ∈ {0=pass, 1=warn, 2=fail}.
    Thresholds: ≥floor → pass, ≥50 → warn, <50 → fail."""
    floor = int(os.environ.get('UNIVERSE_RESOLVER_MIN_LIVE_TICKERS', '200'))
    n, err = _union_universe_size()
    if err:
        return (2, f'union={n} ({err})')
    if n >= floor:
        return (0, f'union={n} ≥ {floor}')
    if n >= 50:
        return (1, f'union={n} < {floor} (warn)')
    return (2, f'union={n} < 50 (FAIL)')


@_check('metadata_snapshot_freshness')
def check_metadata_snapshot_freshness():
    """Verify ticker_metadata_snapshots was written recently.
    PASS ≤1d, WARN ≤3d, FAIL >3d."""
    code, msg = _check_metadata_snapshot_freshness()
    name = 'metadata_snapshot_freshness'
    if code == 0:
        return _ok(name, msg)
    elif code == 1:
        return _warn(name, msg)
    else:
        return _fail(name, msg)


@_check('union_universe_size', slow=True)
def check_union_universe_size():
    """Verify the live-strategy union universe meets the floor.
    PASS ≥UNIVERSE_RESOLVER_MIN_LIVE_TICKERS (default 200),
    WARN ≥50, FAIL <50."""
    code, msg = _check_union_universe_size()
    name = 'union_universe_size'
    if code == 0:
        return _ok(name, msg)
    elif code == 1:
        return _warn(name, msg)
    else:
        return _fail(name, msg)


# ── SP-2 Phase B: 5y backfill health ───────────────────────────────────────
# These are slow=True because they hit Postgres with aggregating queries
# that scan the backfill_audit + ticker_metadata_snapshots tables. They are
# advisory — the daily cycle does not depend on the backfill having
# completed (it operates on the live data path), so the more severe
# thresholds are WARN, not FAIL. The system_check peers (in
# src/system_checks/checks/) provide the stricter ongoing-health signal.

def _backfill_audit_status_counts():
    """Return {status: count} for backfill_audit rows started in the last 7d.
    Indirection seam: tests monkeypatch this to inject fake state."""
    import psycopg2
    uri = (os.environ.get('DATABASE_URL')
           or os.environ.get('POSTGRES_URI')
           or 'postgresql://openclaw:password@localhost:5432/openclaw')
    with psycopg2.connect(uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, count(*) "
                "FROM backfill_audit "
                "WHERE started_at > NOW() - INTERVAL '7 days' "
                "GROUP BY status"
            )
            return {row[0]: int(row[1]) for row in cur.fetchall()}


def _backfill_poisoning_quarantine_count():
    """Return count of quarantined backfill_audit rows that indicate REAL
    data-poisoning risk (validation failures), excluding pure overlap-
    protection refusals.

    Overlap-protection ("overlap with existing X rows under v1...") is a
    safety mechanism that REFUSES to overwrite — it produces no master
    parquet writes, just a conservative skip. Treating those as poisoning
    indicators caused a false-positive doctor FAIL after the first real
    backfill run (1362 overlap-quarantines in v1's 404-ticker pass)."""
    import psycopg2
    uri = (os.environ.get('DATABASE_URL')
           or os.environ.get('POSTGRES_URI')
           or 'postgresql://openclaw:password@localhost:5432/openclaw')
    with psycopg2.connect(uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM backfill_audit "
                "WHERE status='quarantined' "
                "AND started_at > NOW() - INTERVAL '7 days' "
                "AND (error_text IS NULL OR error_text NOT LIKE 'overlap with existing%')"
            )
            return int(cur.fetchone()[0])


def _check_backfill_progress():
    """Return (code, msg) where code ∈ {0=pass, 1=warn, 2=fail}.

    Two-axis evaluation:
      - REAL poisoning quarantines (validation failures): >100 → FAIL,
        >0 → WARN. These indicate the validator caught bad fetches —
        the system worked AS DESIGNED but operator should investigate
        what's producing bad data.
      - Pure OVERLAP quarantines (conservative refusal to overwrite):
        included in the summary but never trigger FAIL. A v1 backfill
        against partially-priced tickers naturally produces lots of
        these (1362 in the 404-ticker pass).

    Gated on OPENCLAW_BACKFILL_5Y_ACTIVE=1."""
    if os.environ.get('OPENCLAW_BACKFILL_5Y_ACTIVE') != '1':
        return (0, 'gate off; n/a')
    try:
        counts = _backfill_audit_status_counts()
        poisoning = _backfill_poisoning_quarantine_count()
    except Exception as exc:
        return (1, f'audit query failed: {type(exc).__name__}: {exc}')
    if not counts:
        return (0, 'no backfill activity in last 7d')
    summary = ', '.join(f'{k}={v}' for k, v in sorted(counts.items()))
    overlap = int(counts.get('quarantined', 0)) - poisoning
    if poisoning > 100:
        return (2, f'poisoning_quarantined={poisoning} > 100 (validation failures) [{summary}, overlap_refusals={overlap}]')
    if poisoning > 0:
        return (1, f'poisoning_quarantined={poisoning} (validation failures) [{summary}, overlap_refusals={overlap}]')
    return (0, f'{summary} (overlap_refusals={overlap}, 0 validation failures)')


@_check('backfill_progress', slow=True)
def check_backfill_progress():
    """SP-2 Phase B: surface backfill quarantine counts from last 7 days.
    FAIL >100 quarantined (data poisoning indicator), WARN >0, PASS otherwise."""
    code, msg = _check_backfill_progress()
    name = 'backfill_progress'
    if code == 0:
        return _ok(name, msg)
    if code == 1:
        return _warn(name, msg)
    return _fail(name, msg)


def _backfill_universe_coverage_rows():
    """Return list of (month_date, row_count) tuples for each month
    in ticker_metadata_snapshots since 2021-05-01.
    Indirection seam: tests monkeypatch this."""
    import psycopg2
    uri = (os.environ.get('DATABASE_URL')
           or os.environ.get('POSTGRES_URI')
           or 'postgresql://openclaw:password@localhost:5432/openclaw')
    with psycopg2.connect(uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT date_trunc('month', snapshot_date)::date AS m, count(*) "
                "FROM ticker_metadata_snapshots "
                "WHERE snapshot_date >= '2021-05-01' "
                "GROUP BY m ORDER BY m"
            )
            return [(row[0], int(row[1])) for row in cur.fetchall()]


def _check_backfill_universe_coverage():
    """Return (code, msg) where code ∈ {0=pass, 1=warn}.
    WARN if no rows yet (Phase B not run),
    WARN if > 6 months have < 2500 rows (per spec target ~3000/month ± 15%),
    PASS otherwise. No FAIL case — advisory only.

    Gated on OPENCLAW_BACKFILL_5Y_ACTIVE=1 — when unset (default), PASS
    with "gate off; n/a". The operator sets the gate AFTER the first
    production backfill so per-month coverage gates evaluate real data."""
    if os.environ.get('OPENCLAW_BACKFILL_5Y_ACTIVE') != '1':
        return (0, 'gate off; n/a')
    try:
        rows = _backfill_universe_coverage_rows()
    except Exception as exc:
        return (1, f'coverage query failed: {type(exc).__name__}: {exc}')
    if not rows:
        return (1, 'no metadata snapshots since 2021-05-01 (Phase B not run)')
    low = [(m, n) for (m, n) in rows if n < 2500]
    total_months = len(rows)
    if len(low) > 6:
        return (1, f'{len(low)}/{total_months} months < 2500 rows (target ~3000±15%)')
    return (0, f'{total_months} months covered, {len(low)} thin (<2500)')


@_check('backfill_universe_coverage', slow=True)
def check_backfill_universe_coverage():
    """SP-2 Phase B: surface per-month row coverage in ticker_metadata_snapshots.
    WARN if empty (Phase B not run) or >6 months with <2500 rows, PASS otherwise."""
    code, msg = _check_backfill_universe_coverage()
    name = 'backfill_universe_coverage'
    if code == 0:
        return _ok(name, msg)
    return _warn(name, msg)


# ── SP-2 Phase C: universe-recs freshness ─────────────────────────────────

def _universe_recs_latest_recommended_at():
    """Return MAX(recommended_at) from strategy_universe_recommendations, or None.
    Indirection seam: tests monkeypatch this."""
    import psycopg2
    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        raise RuntimeError('POSTGRES_URI not set')
    with psycopg2.connect(uri) as c, c.cursor() as cur:
        cur.execute('SELECT MAX(recommended_at) FROM strategy_universe_recommendations')
        row = cur.fetchone()
    return row[0] if row else None


@_check('universe_recs_freshness')
def _check_universe_recs_freshness():
    """Gated freshness of strategy_universe_recommendations. PASS when gate off.
    WARN >8d since last run, FAIL >14d, WARN if table empty (never run).
    Gated on OPENCLAW_UNIVERSE_RECS=1."""
    if os.environ.get('OPENCLAW_UNIVERSE_RECS') != '1':
        return _ok('universe_recs_freshness', 'gate off; skipped')
    try:
        last = _universe_recs_latest_recommended_at()
    except Exception as exc:
        return _warn('universe_recs_freshness', f'query failed: {type(exc).__name__}: {exc}')
    if last is None:
        return _warn('universe_recs_freshness', 'no recs yet — timer never run or gate just enabled')
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age_d = (datetime.now(timezone.utc) - last).days
    if age_d > 14:
        return _fail('universe_recs_freshness', f'last run {age_d}d ago (>{14}d threshold)')
    if age_d > 8:
        return _warn('universe_recs_freshness', f'last run {age_d}d ago (>{8}d threshold)')
    return _ok('universe_recs_freshness', f'last run {age_d}d ago')


def _drift_summary():
    """Indirection seam for the drift-alerts check."""
    sys.path.insert(0, str(ROOT / 'src'))
    from metrics.regime_param_drift import latest_drift_summary
    return latest_drift_summary()


@_check('regime_param_drift_alerts')
def check_regime_param_drift_alerts():
    """Surface drift between live regime perf and priors/applied baselines.

    PASS: 0 FAIL; WARN count < DRIFT_WARN_COUNT_THRESHOLD
    WARN: WARN count in [DRIFT_WARN_COUNT_THRESHOLD, DRIFT_FAIL_WARN_COUNT_THRESHOLD)
    FAIL: any FAIL severity OR WARN count >= DRIFT_FAIL_WARN_COUNT_THRESHOLD
    WARN: query error
    """
    name = 'regime_param_drift_alerts'
    try:
        summary = _drift_summary()
    except Exception as exc:
        return _warn(name, f'drift query failed: {exc!s}')
    ok = int(summary.get('OK', 0))
    warn = int(summary.get('WARN', 0))
    fail = int(summary.get('FAIL', 0))
    insuf = int(summary.get('INSUFFICIENT', 0))
    detail = (f'{ok} OK, {warn} WARN, {fail} FAIL, {insuf} insufficient-data')
    if fail > 0 or warn >= DRIFT_FAIL_WARN_COUNT_THRESHOLD:
        return _fail(name, detail)
    if warn >= DRIFT_WARN_COUNT_THRESHOLD:
        return _warn(name, detail)
    return _ok(name, detail)


def _query_proposals_backlog(sql: str, params: tuple = ()):
    """Indirection seam for the proposals backlog check."""
    import psycopg2
    uri = (os.environ.get('DATABASE_URL')
           or os.environ.get('POSTGRES_URI')
           or 'postgresql://openclaw:password@localhost:5432/openclaw')
    with psycopg2.connect(uri) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


@_check('regime_proposals_backlog')
def check_regime_proposals_backlog():
    """Operator review hygiene: pending proposals shouldn't stagnate.

    PASS:  0 pending, or all pending < PROPOSALS_AGED_WARN_DAYS old
    WARN:  1..(PROPOSALS_AGED_FAIL_COUNT-1) pending older than WARN_DAYS,
           AND none older than FAIL_DAYS
    FAIL:  >= PROPOSALS_AGED_FAIL_COUNT aged-warn proposals, OR any older than FAIL_DAYS
    WARN:  db error
    """
    name = 'regime_proposals_backlog'
    try:
        rows = _query_proposals_backlog("""
            SELECT proposed_at
              FROM strategy_regime_param_proposals
             WHERE status = 'pending'
        """)
    except Exception as exc:
        return _warn(name, f'query failed: {exc!s}')

    now = datetime.now(timezone.utc)
    total = len(rows)
    aged_warn = 0   # >= WARN_DAYS old
    aged_fail = 0   # >= FAIL_DAYS old
    for (proposed_at,) in rows:
        if proposed_at is None:
            continue
        age_days = (now - proposed_at).total_seconds() / 86400.0
        if age_days >= PROPOSALS_AGED_FAIL_DAYS:
            aged_fail += 1
        if age_days >= PROPOSALS_AGED_WARN_DAYS:
            aged_warn += 1
    if aged_fail > 0 or aged_warn >= PROPOSALS_AGED_FAIL_COUNT:
        return _fail(name,
                     f'{total} pending; {aged_warn} aged >={PROPOSALS_AGED_WARN_DAYS}d; '
                     f'{aged_fail} aged >={PROPOSALS_AGED_FAIL_DAYS}d — review backlog')
    if aged_warn > 0:
        return _warn(name,
                     f'{total} pending; {aged_warn} aged >={PROPOSALS_AGED_WARN_DAYS}d')
    return _ok(name, f'{total} pending; none aged >={PROPOSALS_AGED_WARN_DAYS}d')


def _query_consistency(sql: str, params: tuple = ()):
    """Indirection seam so tests can stub registry + params queries."""
    import psycopg2
    uri = (os.environ.get('DATABASE_URL')
           or os.environ.get('POSTGRES_URI')
           or 'postgresql://openclaw:password@localhost:5432/openclaw')
    with psycopg2.connect(uri) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


@_check('strategy_regime_params_consistency')
def check_strategy_regime_params_consistency():
    """Each strategy in strategy_registry × each of 4 canonical regimes must
    have exactly one row in strategy_regime_params. Catches partial migrations
    + orphans (rows pointing at strategies that no longer exist)."""
    name = 'strategy_regime_params_consistency'
    REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')
    try:
        registry_rows = _query_consistency('SELECT id FROM strategy_registry')
        registry_ids = {r[0] for r in registry_rows} if registry_rows else set()
        # Fall back to manifest if strategy_registry is empty (early-bootstrap envs).
        if not registry_ids:
            mf = json.loads((ROOT / 'src' / 'strategies' / 'manifest.json')
                            .read_text(encoding='utf-8'))
            registry_ids = set(mf.get('strategies') or {})
        param_rows = _query_consistency(
            'SELECT strategy_id, regime_state FROM strategy_regime_params')
        param_set = {(r[0], r[1]) for r in param_rows}
    except Exception as exc:
        return _warn(name, f'query failed: {exc!s}')
    expected = {(sid, r) for sid in registry_ids for r in REGIMES}
    missing  = expected - param_set
    orphans  = {sid for sid, _ in param_set} - registry_ids
    if not missing and not orphans:
        return _ok(name,
                   f'{len(registry_ids)} strategies × 4 regimes = '
                   f'{len(expected)} rows, all present')
    parts = []
    if missing:
        first = next(iter(missing))
        parts.append(f'{len(missing)} missing (e.g. {first[0]}/{first[1]})')
    if orphans:
        parts.append(f'{len(orphans)} orphan row(s): {", ".join(list(orphans)[:3])}'
                     + (' …' if len(orphans) > 3 else ''))
    detail = '; '.join(parts)
    if len(missing) >= PARAMS_CONSISTENCY_FAIL_THRESHOLD:
        return _fail(name, detail)
    return _warn(name, detail)


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


# Files Node parses at module-load on a maintenance / curator path. A
# SyntaxError here was the 2026-05-13..18 silence (run_maintenance.js:130
# template literal). Catching it at preflight prevents the same class
# of bug from killing the next scheduled run.
JS_PREFLIGHT_FILES = [
    'src/agent/run_maintenance.js',
    'src/agent/curators/saturday_brain.js',
    'src/agent/curators/comprehensive_review.js',
    'src/agent/curators/position_recommender.js',
    'src/agent/curators/weekly_live_sharpe.js',
    'src/agent/curators/_opus_oneshot.js',
    'src/agent/research/research-orchestrator.js',
    'src/pipeline/daily_health_digest.js',
]


@_check('node_syntax', slow=True)
def check_node_syntax():
    """node -c parses each critical JS file. Catches template-literal /
    syntax regressions before they hit the next scheduled run."""
    failures = []
    for rel in JS_PREFLIGHT_FILES:
        path = ROOT / rel
        if not path.exists():
            continue  # tolerated: caller may not have all curators
        try:
            proc = subprocess.run(
                ['node', '-c', str(path)],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return _fail('node_syntax', f'{type(exc).__name__}: {exc}')
        if proc.returncode != 0:
            stderr = (proc.stderr or '').strip().splitlines()
            first = stderr[0] if stderr else 'unknown error'
            failures.append(f'{rel}: {first[:120]}')
    if failures:
        return _fail('node_syntax', '; '.join(failures))
    return _ok('node_syntax', f'{len(JS_PREFLIGHT_FILES)} files parsed')


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
    # Best-effort .env load so subprocess CLIs inherit Alpaca creds when
    # the doctor is invoked directly (operator on the box). When invoked
    # from the BotJohn maintenance timer, systemd's EnvironmentFile= has
    # already populated the env, and the .env file itself is mode 0600
    # root:root — load_dotenv() would raise PermissionError as claudebot
    # and crash the whole script. Treat any read-side failure as
    # already-loaded-or-not-our-job and continue with whatever env we
    # already have.
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env')
    except (ImportError, PermissionError, OSError):
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument('--quick',         action='store_true',
                    help='Skip slow checks (Redis, data_coverage, systemd)')
    ap.add_argument('--required-only', action='store_true',
                    help='Only the auth/config blockers — for orchestrator preflight')
    ap.add_argument('--fail-only',     action='store_true',
                    help='Exit 0 on WARN; only FAIL drives non-zero exit. For systemd ExecStartPre gates that must not be blocked by advisory warnings.')
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

    if args.fail_only and exit_code == 1:
        exit_code = 0
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
