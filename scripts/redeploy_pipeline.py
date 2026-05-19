#!/usr/bin/env python3
"""redeploy_pipeline.py — Phase 2 intraday pipeline redeploy wrapper.

Why this exists: confirmed intraday regime transitions used to fire a
full-portfolio liquidation. As of 2026-05-19 (Phase 1 of the
redeploy-not-liquidate plan), they instead rebalance via a delta-based
pipeline redeploy. This script is the canonical entry point.

Flow:
  1. Parse --reason (required, clamped to 80 chars), --date (default today),
     and --dry-run.
  2. Check Redis cooldown (`redeploy:cooldown:{date}`) and sentinel
     (`redeploy:fired:{date}:{reason}`); exit 0 with `action=blocked` if
     either is present. Cooldown gating is expected behavior, not an error.
  3. Check alpaca clock — outside RTH with `OPENCLAW_REDEPLOY_EXTENDED_HOURS`
     unset, exit 0 blocked. Extended-hours executor support lands in Phase 3;
     until then we ship-safe by refusing to spawn after-hours redeploys.
  4. Spawn `pipeline_orchestrator.py --steps signals,handoff,trade,alpaca,
     reconcile --reason <reason> --date <date>` synchronously
     (timeout=1800s). The CALLER (intraday detector) is the one that runs
     this script detached; the orchestrator subprocess here can block.
  5. On orchestrator success: SET both Redis keys with their TTLs, post
     success summary to #intraday-regime.
  6. On orchestrator non-zero: leave Redis keys untouched (caller may
     retry), post failure summary, propagate the exit code.

Exit codes (mirror orchestrator):
  0 — success OR cooldown-blocked OR extended-hours-blocked (not errors)
  1 — orchestrator partial failure
  2 — orchestrator fatal-config (or POSTGRES_URI missing)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import date as _date, datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)sZ [%(levelname)s] %(message)s',
)
logger = logging.getLogger('redeploy_pipeline')

ROOT = Path(__file__).resolve().parents[1]

ALPACA_CLI = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')

# TTLs
COOLDOWN_TTL_S = 3600     # 60 min — blocks back-to-back redeploys
SENTINEL_TTL_S = 86400    # 24h — same-transition idempotency
REASON_MAX_LEN = 80
ORCHESTRATOR_TIMEOUT_S = 1800   # 30 min


# ── Helpers ──────────────────────────────────────────────────────────────────

def _redis():
    """Lazy Redis client. Returns None on any connect failure (callers must
    treat None as 'cooldown can't be checked — proceed' but practically the
    intraday detector also gates on its own _redis() check before spawning,
    so collisions are still unlikely)."""
    try:
        import redis
        url = os.environ.get('REDIS_URL', 'redis://localhost:6379')
        r = redis.from_url(url, socket_connect_timeout=3, decode_responses=True)
        r.ping()
        return r
    except Exception as e:
        logger.warning('redis unavailable: %s', e)
        return None


def _alpaca_clock_is_rth() -> tuple[bool, bool]:
    """Returns (call_succeeded, is_open). Fails safe: any CLI/parse error
    returns (False, False) so the caller blocks the spawn. The Phase 3
    after-hours executor will replace this with session-aware logic; until
    then, anything-but-RTH means refuse."""
    try:
        proc = subprocess.run(
            [ALPACA_CLI, 'clock'],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if proc.returncode != 0:
            logger.warning('alpaca clock exit=%s stderr=%s',
                           proc.returncode, proc.stderr[:200])
            return False, False
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            logger.warning('alpaca clock returned non-JSON: %s',
                           proc.stdout[:200])
            return False, False
        return True, bool(payload.get('is_open'))
    except FileNotFoundError:
        logger.warning('alpaca CLI not found at %s', ALPACA_CLI)
        return False, False
    except subprocess.TimeoutExpired:
        logger.warning('alpaca clock timed out (>10s)')
        return False, False
    except Exception as e:
        logger.warning('alpaca clock failed: %s', e)
        return False, False


def _post_webhook(channel: str, msg: str) -> bool:
    """Same shape as scripts/run_intraday_market_state.py:_post_webhook —
    look up agent_registry.webhook_urls and POST. Inlined to avoid pulling
    in the broader execution package for a one-off post."""
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        return False
    try:
        import psycopg2
        import requests
    except ImportError:
        return False
    url = None
    try:
        conn = psycopg2.connect(uri, connect_timeout=5)
        cur = conn.cursor()
        cur.execute(
            "SELECT webhook_urls FROM agent_registry WHERE webhook_urls IS NOT NULL"
        )
        for (hooks,) in cur.fetchall():
            if hooks and channel in hooks:
                url = hooks[channel]
                break
        cur.close()
        conn.close()
    except Exception as e:
        logger.warning('webhook lookup failed: %s', e)
        return False
    if not url:
        logger.info('no webhook registered for #%s', channel)
        return False
    try:
        r = requests.post(url, json={'content': msg[:1900]}, timeout=10)
        return bool(r.ok)
    except Exception as e:
        logger.warning('webhook post failed: %s', e)
        return False


def _print_action(payload: dict) -> None:
    """Emit one JSON-line action result on stdout. Caller (intraday cron
    log or operator tail) greps this."""
    print(json.dumps(payload, default=str))


# ── Steps ────────────────────────────────────────────────────────────────────

REDEPLOY_STEPS = 'signals,handoff,trade,alpaca,reconcile'


def _spawn_orchestrator(reason: str, run_date: str, dry_run: bool) -> int:
    """Run the orchestrator subset synchronously. Returns the orchestrator's
    exit code. We're already in a detached process (spawned by the intraday
    cron), so it's safe to block here for up to 30 min."""
    cmd = [
        sys.executable,
        str(ROOT / 'src' / 'execution' / 'pipeline_orchestrator.py'),
        '--steps', REDEPLOY_STEPS,
        '--reason', reason,
        '--date', run_date,
    ]
    env = {**os.environ}
    if dry_run:
        env['PIPELINE_DRY_RUN'] = '1'
    logger.info('spawning orchestrator (dry_run=%s): %s', dry_run, ' '.join(cmd))
    try:
        proc = subprocess.run(
            cmd, env=env, timeout=ORCHESTRATOR_TIMEOUT_S, check=False,
        )
        return proc.returncode
    except subprocess.TimeoutExpired:
        logger.error('orchestrator timed out after %ss', ORCHESTRATOR_TIMEOUT_S)
        return 1
    except Exception as e:
        logger.error('orchestrator spawn error: %s', e)
        return 1


# ── Main ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='Phase 2 intraday pipeline redeploy wrapper.',
    )
    ap.add_argument(
        '--reason', required=True,
        help='Free-text label (clamped to 80 chars) — e.g. '
             '"INTRADAY_HMM_LOW_VOL_HIGH_VOL". Becomes part of the Redis '
             'sentinel key and the Discord summary.',
    )
    ap.add_argument(
        '--date', default=str(_date.today()),
        help='ISO date (default: today). Used in cooldown + sentinel keys.',
    )
    ap.add_argument(
        '--dry-run', action='store_true',
        help='Propagate PIPELINE_DRY_RUN=1 to the orchestrator (no broker '
             'submission). Cooldown/sentinel keys still get set on success.',
    )
    args = ap.parse_args(argv)

    # Single source of truth for clamped reason — used in keys + webhook.
    reason = (args.reason or '').strip()[:REASON_MAX_LEN]
    if not reason:
        logger.error('--reason must be a non-empty string')
        return 2
    run_date = args.date

    # POSTGRES_URI is required by the orchestrator and by the webhook
    # lookup. Failing early avoids a 30s orchestrator startup for nothing.
    if not os.environ.get('POSTGRES_URI'):
        logger.error('POSTGRES_URI not set — aborting')
        _print_action({
            'action': 'failed', 'reason': reason,
            'exit_code': 2, 'error': 'POSTGRES_URI missing',
        })
        return 2

    cooldown_key = f'redeploy:cooldown:{run_date}'
    sentinel_key = f'redeploy:fired:{run_date}:{reason}'

    # ── Cooldown / sentinel gate ─────────────────────────────────────────
    r = _redis()
    if r is not None:
        try:
            if r.get(cooldown_key):
                ttl = r.ttl(cooldown_key)
                _print_action({
                    'action': 'blocked',
                    'reason': 'cooldown_active',
                    'cooldown_remaining_s': int(ttl) if ttl is not None else None,
                })
                return 0
            if r.get(sentinel_key):
                _print_action({
                    'action': 'blocked',
                    'reason': 'same_transition_already_fired',
                })
                return 0
        except Exception as e:
            logger.warning('redis gate check failed: %s', e)

    # ── Extended-hours gate (Phase-3-deferred) ───────────────────────────
    # Until alpaca_executor lands extended-hours support (Phase 3), we
    # refuse after-hours spawns by default; flip OPENCLAW_REDEPLOY_EXTENDED_HOURS=1
    # only when Phase 3 is live.
    ok, is_open = _alpaca_clock_is_rth()
    ext_enabled = os.environ.get('OPENCLAW_REDEPLOY_EXTENDED_HOURS') == '1'
    if not ok or not is_open:
        if not ext_enabled:
            _print_action({
                'action': 'blocked',
                'reason': 'extended_hours_disabled_until_phase3',
            })
            return 0

    # ── Spawn the orchestrator subset ────────────────────────────────────
    started = time.time()
    rc = _spawn_orchestrator(reason, run_date, dry_run=args.dry_run)
    duration_s = round(time.time() - started, 1)

    if rc == 0:
        # Success — set cooldown + sentinel
        if r is not None:
            try:
                r.set(cooldown_key, '1', ex=COOLDOWN_TTL_S)
                r.set(sentinel_key, '1', ex=SENTINEL_TTL_S)
            except Exception as e:
                logger.warning('failed to set cooldown/sentinel keys: %s', e)

        _print_action({
            'action':       'completed',
            'reason':       reason,
            'duration_s':   duration_s,
            'exit_code':    0,
        })
        dry_tag = ' [DRY-RUN]' if args.dry_run else ''
        _post_webhook(
            'intraday-regime',
            f':rocket: **Pipeline redeploy complete**{dry_tag} | '
            f'reason={reason} | duration={duration_s}s | '
            f'steps={REDEPLOY_STEPS}',
        )
        return 0

    # ── Failure: do NOT set cooldown/sentinel; let caller retry ──────────
    log_hint = f'logs/redeploy_pipeline_{run_date}.log'
    _print_action({
        'action':    'failed',
        'reason':    reason,
        'exit_code': rc,
    })
    dry_tag = ' [DRY-RUN]' if args.dry_run else ''
    _post_webhook(
        'intraday-regime',
        f':warning: **Pipeline redeploy FAILED**{dry_tag} | '
        f'reason={reason} | exit_code={rc} | duration={duration_s}s | '
        f'see {log_hint}',
    )
    return rc


if __name__ == '__main__':
    sys.exit(main())
