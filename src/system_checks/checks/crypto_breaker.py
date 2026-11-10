"""Broker-tagged check: the crypto drawdown breaker is actually evaluating.

The breaker tick (src/execution/crypto_breaker.py, `crypto-breaker-tick`, every
5 min) writes one `crypto_breaker_evals` row per tick whenever crypto is held.
A tick that cannot reach Redis AND Postgres skips as `no_state_store` and
writes nothing; a tick that dies before the DB write leaves no row either. So
"tick enabled + crypto held + no eval row in 30 min" means the risk control is
blind. Precedence: tick flag not '1' -> SKIP; DB or alpaca CLI unavailable ->
SKIP (fail-open, informational: no INFO status exists); no crypto positions ->
PASS; newest eval missing/older than MAX_AGE_MIN -> WARN; else PASS.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess

from ..registry import check
from ..types import Status

MAX_AGE_MIN = 30


def _newest_eval_ts():
    """Newest crypto_breaker_evals.ts_utc (aware UTC) or None; raises on DB error."""
    import psycopg2
    with psycopg2.connect(os.environ['POSTGRES_URI'], connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT MAX(ts_utc) FROM crypto_breaker_evals')
            row = cur.fetchone()
    return row[0] if row else None


def _crypto_position_count() -> int:
    """Crypto rows in `alpaca position list`; raises when the CLI is unavailable."""
    cli = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')
    pr = subprocess.run([cli, 'position', 'list'], capture_output=True, text=True, timeout=15)
    if pr.returncode != 0:
        raise RuntimeError(f'position list rc={pr.returncode}: {pr.stderr.strip()[:120]}')
    rows = json.loads(pr.stdout or '[]') or []
    return sum(1 for p in rows if isinstance(p, dict) and p.get('asset_class') == 'crypto')


@check(name='crypto_breaker_evaluating', tags=['broker'], requires=[])
def _crypto_breaker_evaluating():
    if os.environ.get('OPENCLAW_CRYPTO_BREAKER_TICK') != '1':
        return Status.SKIP, 'OPENCLAW_CRYPTO_BREAKER_TICK != 1 (breaker tick off)'
    try:
        n = _crypto_position_count()
    except Exception as e:  # noqa: BLE001 - fail-open
        return Status.SKIP, f'info: alpaca CLI unavailable ({type(e).__name__}: {str(e)[:100]})'
    if n == 0:
        return Status.PASS, 'no crypto positions (breaker writes no eval rows when flat)'
    try:
        ts = _newest_eval_ts()
    except Exception as e:  # noqa: BLE001 - fail-open
        return Status.SKIP, f'info: DB unavailable ({type(e).__name__}: {str(e)[:100]})'
    if ts is None:
        return Status.WARN, f'{n} crypto positions held but crypto_breaker_evals is empty'
    if isinstance(ts, str):
        ts = dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    age_min = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 60.0
    if age_min > MAX_AGE_MIN:
        return Status.WARN, (f'{n} crypto positions held but newest breaker eval is '
                             f'{age_min:.0f} min old (> {MAX_AGE_MIN}): breaker blind '
                             '(no_state_store / crash?) — check logs/crypto_breaker_*.log')
    return Status.PASS, f'newest breaker eval {age_min:.0f} min old; {n} crypto positions'
