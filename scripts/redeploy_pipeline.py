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
# This script is spawned BY PATH from scripts/run_intraday_market_state.py
# (sys.executable scripts/redeploy_pipeline.py), so sys.path[0] is scripts/,
# not the repo root. Mirror scripts/refetch_prices.py and put ROOT on the
# path before the first project import.
sys.path.insert(0, str(ROOT))

from src.execution import intraday_prefetch as _pf   # noqa: E402

ALPACA_CLI = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')

# TTLs
COOLDOWN_TTL_S = 3600     # 60 min — blocks back-to-back redeploys
SENTINEL_TTL_S = 86400    # 24h — same-transition idempotency
REASON_MAX_LEN = 80
ORCHESTRATOR_TIMEOUT_S = 1800   # 30 min

# ── tick-3 data-ready gate (OPENCLAW_INTRADAY_15MIN_PREFETCH) ─────────────────
GATE_TIMEOUT_S = 1200   # 20 min — bounded wait for the tick-1 prefetch
GATE_POLL_S = 30


def _freshness_ok(date: str) -> bool:
    from scripts.refetch_prices import _freshness_ok as _f
    ok, _ = _f(date)
    return ok


def _sync_refetch(date: str, episode: str | None = None) -> int:
    from scripts.refetch_prices import run as _run
    return _run(date, episode=episode)


def _data_ready_gate(date, expected_episode=None):
    """'proceed'|'abort'. Proceeds ONLY on a done+fresh sentinel for THIS
    episode; otherwise sync-refetches fresh (stamped to this episode). When
    expected_episode is None (legacy/tests), episode-matching is a no-op.

    CORRECTNESS lives here: a stale `done` sentinel from a PRIOR intraday
    episode (still within the 6h sentinel TTL) must NOT satisfy this gate.
    Binding proceed/abort to `episode == expected_episode` closes that hole;
    the tick-3 streak-start reconstruction is only a fast-path to reuse the
    tick-1 prefetch — a mismatch here just sync-refetches fresh, never
    proceeds on the wrong episode."""
    r = _redis()
    def _matches(s):
        return bool(s) and (expected_episode is None or s.get('episode') == expected_episode)
    s = _pf.read_prefetch(r, date)
    # No in-flight or done prefetch for THIS episode → fetch fresh now.
    if not (_matches(s) and s.get('status') in ('running', 'done')):
        _sync_refetch(date, expected_episode)
    waited = 0
    while True:
        s = _pf.read_prefetch(r, date)
        if _matches(s) and s.get('status') == 'done' and _freshness_ok(date):
            return 'proceed'
        if _matches(s) and s.get('status') == 'failed':
            return 'abort'
        if waited >= GATE_TIMEOUT_S:
            return 'abort'
        time.sleep(GATE_POLL_S)
        waited += GATE_POLL_S


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


def _close_execute_inflight(r, run_date) -> bool:
    """True if the daily close-execute phase holds the in-flight lock for
    run_date. The 3:55pm execute sets `execute:close:inflight:{date}` (TTL ~5m);
    a redeploy must defer rather than race the market-into-close submission."""
    if r is None:
        return False
    try:
        return bool(r.get(f'execute:close:inflight:{run_date}'))
    except Exception:
        return False


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


def _report_completed(reason: str, duration_s: float, dry_run: bool) -> None:
    """Shared success reporting — byte-identical for the flag-ON and
    flag-OFF paths so both emit the same action JSON + Discord post."""
    _print_action({
        'action':       'completed',
        'reason':       reason,
        'duration_s':   duration_s,
        'exit_code':    0,
    })
    dry_tag = ' [DRY-RUN]' if dry_run else ''
    _post_webhook(
        'intraday-regime',
        f':rocket: **Pipeline redeploy complete**{dry_tag} | '
        f'reason={reason} | duration={duration_s}s | '
        f'steps={REDEPLOY_STEPS}',
    )


def _report_failed(reason: str, rc: int, duration_s: float,
                   run_date: str, dry_run: bool) -> None:
    """Shared failure reporting — byte-identical for both paths."""
    log_hint = f'logs/redeploy_pipeline_{run_date}.log'
    _print_action({
        'action':    'failed',
        'reason':    reason,
        'exit_code': rc,
    })
    dry_tag = ' [DRY-RUN]' if dry_run else ''
    _post_webhook(
        'intraday-regime',
        f':warning: **Pipeline redeploy FAILED**{dry_tag} | '
        f'reason={reason} | exit_code={rc} | duration={duration_s}s | '
        f'see {log_hint}',
    )


# ── Steps ────────────────────────────────────────────────────────────────────

REDEPLOY_STEPS = 'signals,handoff,trade,alpaca,reconcile'

# The redeploy REGENERATES signals in its `signals` step, so — unlike the daily
# cycle, which is news-gated by the 9:15 ET pre-market gate before its trade step —
# the redeploy has no upstream news gate. The inline TradeJohn sizer confirmer used
# to provide that intraday news-veto, but it was retired 2026-07-20. To keep the
# protection, split the redeploy around the pre-market gate: generate signals, run
# the news gate over them, THEN handoff→trade→… so news-rejected signals never reach
# the sizer. (run_gate updates execution_signals.lifecycle_state, exactly as it does
# for the daily flow.)
_REDEPLOY_STEPS_PRE_GATE = 'signals'
_REDEPLOY_STEPS_POST_GATE = 'handoff,trade,alpaca,reconcile'


def _run_intraday_news_gate(run_date: str, dry_run: bool) -> None:
    """Run the pre-market news gate over the freshly-regenerated redeploy signals,
    before they reach the sizer. Gated on OPENCLAW_EOD_PREMARKET_GATE (same flag as
    the daily gate). FAIL-OPEN — a gate error must never block the redeploy (news
    protection is best-effort, mirroring the daily gate's own fail-open posture)."""
    if os.environ.get('OPENCLAW_EOD_PREMARKET_GATE') != '1':
        logger.info('intraday news gate skipped (OPENCLAW_EOD_PREMARKET_GATE!=1)')
        return
    if dry_run:
        logger.info('[DRY-RUN] intraday news gate skipped')
        return
    try:
        sys.path.insert(0, str(ROOT / 'src'))
        from execution.premarket_gate import run_gate
        result = run_gate()
        logger.info('intraday news gate ran: %s', result)
    except Exception as e:
        logger.warning('intraday news gate failed (fail-open, continuing): %s', e)


def _run_redeploy(reason: str, run_date: str, dry_run: bool) -> int:
    """Full redeploy sequence: signal-gen → news-gate → trade→…. Returns the last
    orchestrator exit code (or the pre-gate code if signal-gen failed). Splitting the
    orchestrator subset around the news gate is what closes the intraday news-veto gap
    left by retiring the inline sizer confirmer (2026-07-20)."""
    rc = _spawn_orchestrator(reason, run_date, dry_run, steps=_REDEPLOY_STEPS_PRE_GATE)
    if rc != 0:
        return rc
    _run_intraday_news_gate(run_date, dry_run)
    return _spawn_orchestrator(reason, run_date, dry_run, steps=_REDEPLOY_STEPS_POST_GATE)


def _spawn_orchestrator(reason: str, run_date: str, dry_run: bool,
                        steps: str = REDEPLOY_STEPS) -> int:
    """Run the orchestrator subset synchronously. Returns the orchestrator's
    exit code. We're already in a detached process (spawned by the intraday
    cron), so it's safe to block here for up to 30 min."""
    # --force-resume is required: the daily 10am pipeline sets
    # `pipeline:completed:{date}` after its final step, which would otherwise
    # block every intraday redeploy for the rest of the trading day. The
    # actual idempotency layer for redeploys is *this* script's
    # `redeploy:cooldown:{date}` (60min) + `redeploy:fired:{date}:{reason}`
    # (24h) gates checked above — so bypassing the orchestrator's daily
    # sentinel is safe, and necessary for redeploys to fire at all post-10am.
    if os.environ.get('OPENCLAW_LANGGRAPH_ORCHESTRATOR') == '1':
        # New path: invoke the LangGraph daily-cycle graph with subset filter.
        run_graph_js = ROOT / 'bin' / 'run-graph.js'
        payload = {
            'runDate':        run_date,
            'reason':         reason,
            'requestedSteps': steps.split(','),
        }
        cmd = ['node', str(run_graph_js), 'daily-cycle', json.dumps(payload)]
        logger.info('spawning LangGraph daily-cycle (dry_run=%s): %s', dry_run, ' '.join(cmd))
    else:
        # Legacy path: pipeline_orchestrator.py --steps --reason --force-resume
        cmd = [
            sys.executable,
            str(ROOT / 'src' / 'execution' / 'pipeline_orchestrator.py'),
            '--steps', steps,
            '--reason', reason,
            '--date', run_date,
            '--force-resume',
        ]
        logger.info('spawning orchestrator (dry_run=%s): %s', dry_run, ' '.join(cmd))

    env = {**os.environ}
    if dry_run:
        env['PIPELINE_DRY_RUN'] = '1'
    # Redeploys are *expected* to rewrite the sized handoff — the morning
    # cycle has already populated `alpaca_submissions` for today, and
    # sized_handoff.finalize_sized_payload would otherwise refuse to
    # overwrite. The delta sizer (target - current_broker_position) nets
    # against existing positions and the executor's per-order
    # already_executed() guard prevents resubmitting filled orders, so
    # opting into FORCE_RESIZE here is safe and necessary.
    env['OPENCLAW_FORCE_RESIZE'] = '1'
    # Signal to the sizer that this is an intraday redeploy — _load_lambda
    # will read position_sizing_lambda_intraday (default 1× NAV) instead of
    # the overnight position_sizing_lambda (typically 1.85×). This prevents
    # over-leveraging on intraday regime-change redeploys.
    env['OPENCLAW_INTRADAY_REDEPLOY'] = '1'
    try:
        proc = subprocess.run(cmd, env=env, timeout=ORCHESTRATOR_TIMEOUT_S, check=False)
        return proc.returncode
    except subprocess.TimeoutExpired:
        logger.error('pipeline subprocess timed out after %ss', ORCHESTRATOR_TIMEOUT_S)
        return 1
    except Exception as e:
        logger.error('pipeline subprocess spawn error: %s', e)
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
    ap.add_argument(
        '--episode', default=None,
        help='Tick-3 expected transition-episode key. When set, the data-ready '
             'gate proceeds ONLY on a prefetch sentinel stamped to THIS episode '
             '(else it sync-refetches fresh) — closes the stale-`done` hole. '
             'Absent (legacy/flag-OFF) leaves episode-matching a no-op.',
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
    # Flag ON: the 60-min `redeploy:cooldown` is DROPPED (the in-flight lock,
    # acquired by the detector and released by this script, is the only
    # back-to-back guard). The sentinel + close-execute-inflight blocks stay.
    # Flag OFF: all three checks unchanged. On the flag-ON path the detector
    # already holds `intraday:redeploy:inflight`, so any early-return here
    # must RELEASE it (release is an idempotent delete — safe; deferring SHOULD
    # let the next attempt re-acquire rather than block for the full TTL).
    r = _redis()
    if r is not None:
        try:
            if not _pf.prefetch_enabled() and r.get(cooldown_key):
                ttl = r.ttl(cooldown_key)
                _print_action({
                    'action': 'blocked',
                    'reason': 'cooldown_active',
                    'cooldown_remaining_s': int(ttl) if ttl is not None else None,
                })
                return 0
            if r.get(sentinel_key):
                if _pf.prefetch_enabled():
                    _pf.release_inflight(r)
                _print_action({
                    'action': 'blocked',
                    'reason': 'same_transition_already_fired',
                })
                return 0
            if _close_execute_inflight(r, run_date):
                if _pf.prefetch_enabled():
                    _pf.release_inflight(r)
                _print_action({
                    'action': 'blocked',
                    'reason': 'close_execute_inflight',
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
            if _pf.prefetch_enabled():
                _pf.release_inflight(r)
            _print_action({
                'action': 'blocked',
                'reason': 'extended_hours_disabled_until_phase3',
            })
            return 0

    # ── Spawn the orchestrator subset ────────────────────────────────────
    if _pf.prefetch_enabled():
        # Flag ON: WAIT for the tick-1 prices-only prefetch (data-ready gate)
        # BEFORE running any trading steps; ABORT (no signals/orders) if it
        # failed/timed-out/stale. The in-flight lock (held by the detector) is
        # RELEASED in `finally` so it never outlives this redeploy — covers
        # abort, success, and orchestrator failure. Cooldown is NOT set on
        # success (dropped under this flag); sentinel still is.
        try:
            if _data_ready_gate(run_date, args.episode) == 'abort':
                _print_action({
                    'action': 'aborted',
                    'reason': reason,
                    'detail': 'ingestion_not_ready',
                })
                _post_webhook(
                    'intraday-regime',
                    f'⛔ redeploy aborted | reason={reason} — price '
                    f'ingestion failed/timeout; regime row updated, no '
                    f'signals/orders submitted',
                )
                return 0

            started = time.time()
            rc = _run_redeploy(reason, run_date, dry_run=args.dry_run)
            duration_s = round(time.time() - started, 1)

            if rc == 0:
                # Success — set sentinel ONLY (no cooldown under this flag).
                if r is not None:
                    try:
                        r.set(sentinel_key, '1', ex=SENTINEL_TTL_S)
                    except Exception as e:
                        logger.warning('failed to set sentinel key: %s', e)
                _report_completed(reason, duration_s, args.dry_run)
                return 0

            # Failure: do NOT set sentinel; let the caller retry.
            _report_failed(reason, rc, duration_s, run_date, args.dry_run)
            return rc
        finally:
            _pf.release_inflight(_redis())

    # ── Legacy path (flag OFF): byte-identical to pre-Task-7 behavior ─────
    started = time.time()
    rc = _run_redeploy(reason, run_date, dry_run=args.dry_run)
    duration_s = round(time.time() - started, 1)

    if rc == 0:
        # Success — set cooldown + sentinel
        if r is not None:
            try:
                r.set(cooldown_key, '1', ex=COOLDOWN_TTL_S)
                r.set(sentinel_key, '1', ex=SENTINEL_TTL_S)
            except Exception as e:
                logger.warning('failed to set cooldown/sentinel keys: %s', e)

        _report_completed(reason, duration_s, args.dry_run)
        return 0

    # ── Failure: do NOT set cooldown/sentinel; let caller retry ──────────
    _report_failed(reason, rc, duration_s, run_date, args.dry_run)
    return rc


if __name__ == '__main__':
    sys.exit(main())
