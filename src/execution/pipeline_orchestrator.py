#!/usr/bin/env python3
"""
pipeline_orchestrator.py — 10am daily cycle (Phase 2).

Runs after every data collection cycle (spawned by collector.js).
Agents communicate DIRECTLY — Discord posts are write-only for human visibility only.

Pipeline (direct agent-to-agent, no Discord round-trip):
  (queue_drain removed 2026-04-28 — fused-staging-approval handles backfills inline)
  2. run_collector_once.js → one collector cycle (prices/options/fundamentals/news)
  3. engine.py             → run strategies → execution_signals (zero-LLM)
  4. trade_handoff_builder → HV/beta/momentum/EV per signal → structured JSON
  5. trade_agent_llm.py    → TradeJohn Claude → sized orders handoff
  6. alpaca_executor.py    → Alpaca paper bracket orders
  7. send_report.py        → greenlist → #trade-signals, veto digest → #trade-reports

Budget check before steps 1 and 3 (step 3 invokes TradeJohn Claude LLM).

Usage:
  python3 src/execution/pipeline_orchestrator.py [--date YYYY-MM-DD] [--force-resume]
"""

import os, sys, json, subprocess, time, requests
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# ── Config ────────────────────────────────────────────────────────────────────

CRITICAL_PCT    = 0.10   # pause if token budget drops below 10% remaining
PAUSE_ON_RED    = True   # pause if budget:mode == RED (USD spend too high)
CHECKPOINT_KEY  = 'pipeline:resume_checkpoint'
LOCK_KEY        = 'pipeline:running'
LOCK_TTL        = 7200   # 2 hour lock TTL — covers worst-case collector
                         # (45min) + slow TradeJohn (27min on 04-24's
                         # third attempt) + buffer. Was 3600s, but a 14:00
                         # cron failure followed by 17:37 retry beat that
                         # window and produced two concurrent runs.
COMPLETED_KEY   = 'pipeline:completed'     # idempotency sentinel; set when all 5 steps done
COMPLETED_TTL   = 86400  # 24h — covers full-day re-trigger window

# Ordered pipeline steps: key → script name (without .py)
STEPS = [
    # queue_drain removed 2026-04-28: the fused-staging-approval worker now
    # backfills required columns inline at approval time, so the daily cycle
    # has nothing to drain. No replacement needed — staging approvals run
    # their own backfill against the master parquets directly.
    ('collect',     'run_collector_once'),    # Node wrapper: one cycle of collector.js (parquet-primary)
    ('signals',     'engine'),                # zero-LLM strategy executor → execution_signals
    ('handoff',     'trade_handoff_builder'), # deterministic features → handoff:{date}:structured
    ('trade',       'trade_agent_llm'),       # Deterministic sizer (LLM bypassed by default)
    ('alpaca',      'alpaca_executor'),       # Auto-submit sized orders to Alpaca paper
    ('reconcile',   'alpaca_reconcile'),      # Reconcile alpaca_submissions vs broker FILL activities
    ('report',      'send_report'),           # Greenlist → #trade-signals, veto digest → #trade-reports
    ('health',      'daily_health_digest'),   # End-of-cycle: build + post daily health digest to #pipeline-feed
]

# Budget check required before LLM-adjacent steps. `trade` is the only Claude
# call in the 10am cycle now — all other steps are deterministic / zero-token.
BUDGET_CHECK_BEFORE = {'trade'}

# Operator-facing notifications (pause, resume, step failure). Phase 4
# rewiring: strategy-memos is now MastermindJohn-weekly-only, so alerts
# that used to post there are redirected into #pipeline-feed via the
# DATABOT_TOKEN path below. The legacy webhook is kept as a last-ditch
# fallback if the bot token path is unavailable.
NOTIFY_WEBHOOK = os.environ.get('ORCHESTRATOR_NOTIFY_WEBHOOK', '')

# Step → failure-alert channel. Data-pipeline step failures surface in
# #data-alerts; trade-pipeline step failures surface in #trade-reports.
# Phase boundaries (▶️/✅/❌) continue to go to #pipeline-feed for every step.
STEP_FAILURE_CHANNEL = {
    'collect':     'data-alerts',
    'signals':     'data-alerts',
    'handoff':     'trade-reports',
    'trade':       'trade-reports',
    'alpaca':      'trade-reports',
    'report':      'trade-reports',
    'health':      'pipeline-feed',
}


# ── Agent status (replicates agentPersonas.setStatus from Node.js) ───────────

def set_agent_status(r, agent_id, status, current_task=None):
    """
    Update agent status in Redis + DB so Discord presence dots update.
    Mirrors agentPersonas.setStatus() from agent-personas.js.
    """
    postgres_uri = os.environ.get('POSTGRES_URI', '')
    if postgres_uri:
        try:
            import psycopg2
            conn = psycopg2.connect(postgres_uri)
            cur  = conn.cursor()
            cur.execute(
                'UPDATE agent_registry SET status=%s, current_task=%s, last_seen_at=NOW() WHERE id=%s',
                [status, current_task, agent_id]
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log(f'Status DB update failed ({agent_id}): {e}')

    emoji_map = {'online': '', 'busy': '⚙️ ', 'idle': '💤 ', 'offline': ''}
    prefix    = emoji_map.get(status, '')
    activity  = f'{prefix}{current_task}'.strip() if current_task else agent_id
    try:
        r.setex(f'agent_status:{agent_id}', 300,
                json.dumps({'status': status, 'currentTask': current_task,
                            'updatedAt': datetime.now(timezone.utc).isoformat()}))
        r.publish('agent:presence', json.dumps({'agentId': agent_id, 'status': status, 'activity': activity}))
    except Exception as e:
        log(f'Status Redis update failed ({agent_id}): {e}')


# ── Redis helpers ─────────────────────────────────────────────────────────────

def get_redis():
    import redis as _redis
    url = os.environ.get('REDIS_URL', 'redis://localhost:6379')
    return _redis.from_url(url, decode_responses=True)


def acquire_lock(r, run_date):
    """Returns True if lock acquired (no other run in progress)."""
    key = f'{LOCK_KEY}:{run_date}'
    return bool(r.set(key, '1', nx=True, ex=LOCK_TTL))


def release_lock(r, run_date):
    r.delete(f'{LOCK_KEY}:{run_date}')


def read_checkpoint(r):
    try:
        raw = r.get(CHECKPOINT_KEY)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def write_checkpoint(r, completed, run_date, reason):
    data = {
        'completed_steps': sorted(completed),
        'run_date':        run_date,
        'paused_at':       datetime.now(timezone.utc).isoformat(),
        'reason':          reason,
    }
    r.set(CHECKPOINT_KEY, json.dumps(data), ex=86400)
    log(f'Checkpoint saved — completed: {sorted(completed)}, reason: {reason}')


def clear_checkpoint(r):
    r.delete(CHECKPOINT_KEY)


def is_completed_today(r, run_date):
    """Return True if the pipeline already finished all 5 steps for run_date."""
    try:
        return bool(r.get(f'{COMPLETED_KEY}:{run_date}'))
    except Exception:
        return False


def mark_completed(r, run_date, status='1'):
    """Set the once-per-day sentinel so repeat triggers exit early.

    The default status='1' preserves legacy behavior. Tier 3 callers may
    pass status='aborted_auth' so the next-day cron + dashboard can
    distinguish a clean cycle complete from a credentials-revoked abort
    (the value lands in the same Redis key under COMPLETED_KEY).
    """
    try:
        r.set(f'{COMPLETED_KEY}:{run_date}', str(status), ex=COMPLETED_TTL)
    except Exception as e:
        log(f'mark_completed failed: {e}')


# ── Budget check ──────────────────────────────────────────────────────────────

def check_budget(r):
    """
    Returns (ok: bool, reason: str).
    ok=False means pause — either budget:mode RED or token budget critical.
    """
    # USD spend budget (set by enforcer.js)
    try:
        mode = r.get('budget:mode') or 'GREEN'
        if PAUSE_ON_RED and mode == 'RED':
            daily  = r.get('budget:daily_usd') or '?'
            return False, f'budget:mode=RED (daily spend ${daily})'
    except Exception:
        pass

    # Token budget (set by token-budget.js)
    try:
        workspace_id = os.environ.get('WORKSPACE_ID', 'default')
        today        = date.today().isoformat()
        usage_key    = f'token_usage:{workspace_id}:{today}'
        used_raw     = r.get(usage_key) or '0'
        used         = int(used_raw)

        # Read daily limit from preferences.json
        prefs_paths = [
            ROOT / 'workspaces' / workspace_id / '.agents' / 'user' / 'preferences.json',
            ROOT / 'workspaces' / 'default' / '.agents' / 'user' / 'preferences.json',
        ]
        limit = 100_000
        for p in prefs_paths:
            if p.exists():
                try:
                    prefs = json.loads(p.read_text())
                    limit = prefs.get('token_budget', {}).get('daily_limit', 100_000)
                    break
                except Exception:
                    pass

        pct_remaining = max(0.0, (limit - used) / limit) if limit > 0 else 1.0
        if pct_remaining < CRITICAL_PCT:
            return False, f'token budget critical ({pct_remaining*100:.1f}% remaining, {used:,}/{limit:,} used)'
    except Exception:
        pass

    return True, 'ok'


# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now(timezone.utc).strftime('%H:%M:%S')
    print(f'[{ts}] [orchestrator] {msg}', flush=True)


def notify(msg, channel='pipeline-feed'):
    """Operator alert. Posts to the named Discord channel via the DataBot
    token. `channel` can be 'pipeline-feed', 'data-alerts', 'trade-reports'.
    Only falls back to ORCHESTRATOR_NOTIFY_WEBHOOK if set and bot posting
    fails — no hardcoded legacy webhook."""
    ok = post_channel(channel, msg)
    if not ok and NOTIFY_WEBHOOK:
        try:
            r = requests.post(NOTIFY_WEBHOOK, json={'content': msg}, timeout=10)
            if not r.ok:
                log(f'Notify webhook failed: {r.status_code}')
        except Exception as e:
            log(f'Notify webhook error: {e}')


def _emit_maintenance_report(run_date, *, failed_step=None,
                              completed_steps=None, error_msg=None):
    """Fire the end-of-cycle daily-health digest. Called on BOTH success
    and failure paths so the operator gets a maintenance report at every
    cycle exit. When a failure is being reported the failed-step +
    completed-steps + error are passed as CLI args so the digest can
    flag the abort explicitly.

    Best-effort — never raises; never blocks the orchestrator's own exit.
    """
    import subprocess as _sp
    script = ROOT / 'src' / 'pipeline' / 'daily_health_digest.js'
    if not script.exists():
        log('maintenance report skipped: daily_health_digest.js missing')
        return
    argv = ['node', str(script)]
    if failed_step:
        argv += ['--failed-step', str(failed_step)]
    if completed_steps:
        argv += ['--completed', ','.join(sorted(set(completed_steps)))]
    if error_msg:
        argv += ['--error', str(error_msg)[:300]]
    try:
        _sp.run(argv, cwd=str(ROOT), timeout=60, check=False)
    except Exception as exc:
        log(f'maintenance report invocation failed (non-fatal): {exc}')


# ── Pipeline-feed channel posts (Phase 4) ────────────────────────────────────
# Concise phase-boundary pings for operator visibility without per-signal noise.
# Uses the DataBot REST API (same pattern as send_report.py). Channel ID is
# discovered once per process and cached.

_CHANNEL_WEBHOOK_CACHE: dict[str, str] | None = None


def _load_channel_webhooks() -> dict[str, str]:
    """Scan agent_registry.webhook_urls and build {channel_name: webhook_url}.
    Posting via webhook URL bypasses Discord bot role permissions — the
    DataBot / TradeDesk bot accounts can't POST to messages endpoints on
    these channels (403 Missing Permissions) but the persisted webhooks do."""
    global _CHANNEL_WEBHOOK_CACHE
    if _CHANNEL_WEBHOOK_CACHE is not None:
        return _CHANNEL_WEBHOOK_CACHE
    out: dict[str, str] = {}
    try:
        import psycopg2
        conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        cur = conn.cursor()
        cur.execute("SELECT id, webhook_urls FROM agent_registry WHERE webhook_urls IS NOT NULL")
        for agent_id, hooks in cur.fetchall():
            for ch_name, url in (hooks or {}).items():
                # First seen wins; databot owns data-alerts/pipeline-feed,
                # tradedesk owns trade-*, etc. — the table is already
                # de-duplicated via agent_registry so conflicts are rare.
                out.setdefault(ch_name, url)
        conn.close()
    except Exception as e:
        log(f'webhook cache load failed: {e}')
    _CHANNEL_WEBHOOK_CACHE = out
    return out


def post_channel(channel_name: str, msg: str) -> bool:
    """Post to a Discord channel via the persona's persisted webhook URL.
    Non-blocking: failures never raise, they log and return False.
    Splits at 1900 chars if the message is longer."""
    hooks = _load_channel_webhooks()
    url = hooks.get(channel_name)
    if not url:
        log(f'no webhook for #{channel_name} — skipping')
        return False
    remaining = msg
    while remaining:
        chunk = remaining[:1900]
        remaining = remaining[1900:]
        for _attempt in range(3):
            try:
                r = requests.post(url, json={'content': chunk}, timeout=10)
            except Exception as e:
                log(f'webhook post exception ({channel_name}): {e}')
                return False
            if r.ok:
                break
            if r.status_code == 429:
                try:
                    wait = float(r.headers.get('Retry-After') or r.json().get('retry_after') or 2)
                except Exception:
                    wait = 2.0
                time.sleep(min(wait + 0.5, 10))
                continue
            log(f'webhook post failed ({channel_name}): {r.status_code}')
            return False
        else:
            return False
    return True


def pipeline_feed(msg):
    """Post a concise one-liner to #pipeline-feed. Non-blocking."""
    post_channel('pipeline-feed', msg)


def broadcast_dashboard_refresh(run_date):
    """POST to the dashboard's internal SSE broadcast so every open browser
    tab auto-refreshes once the pipeline finishes. Contract: fires market_update
    after all steps complete and all DB writes have committed."""
    import http.client
    port = int(os.environ.get('DASHBOARD_PORT') or 3000)
    body = json.dumps({'type': 'market_update', 'source': 'pipeline_orchestrator', 'run_date': run_date}).encode()
    conn = http.client.HTTPConnection('localhost', port, timeout=5)
    try:
        conn.request('POST', '/api/events/data-updated',
                     body=body, headers={'Content-Type': 'application/json'})
        resp = conn.getresponse()
        resp.read()
    finally:
        conn.close()


SIGNAL_QUALITY_THRESHOLDS = {
    'min_avg_ev':      -0.5,   # avg EV must be > -0.5% to allow trade step
    'min_green_count':  1,     # at least 1 green signal required
}

def check_signal_quality(run_date):
    """
    Decide whether the trade step should run. Source-of-truth is the
    daily_signal_summary table (migration 040). Fall back to parsing
    signal_patterns.md only if the table read fails — covers first-run
    state before the writer lands in production.

    Returns (ok: bool, reason: str).
    """
    # Primary: structured DB row
    try:
        import psycopg2
        uri = os.environ.get('POSTGRES_URI', '')
        if uri:
            conn = psycopg2.connect(uri)
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT avg_ev, ev_pos, n_signals, run_date
                        FROM daily_signal_summary
                        ORDER BY run_date DESC, created_at DESC
                        LIMIT 1
                    """)
                    row = cur.fetchone()
            finally:
                conn.close()
            if row:
                avg_ev_frac, ev_pos, n_signals, row_date = row
                avg_ev_pct = float(avg_ev_frac) * 100 if avg_ev_frac is not None else 0.0
                green_cnt  = int(ev_pos or 0)
                if avg_ev_pct < SIGNAL_QUALITY_THRESHOLDS['min_avg_ev'] and green_cnt < SIGNAL_QUALITY_THRESHOLDS['min_green_count']:
                    return False, f'avg EV={avg_ev_pct:+.2f}% ({green_cnt} green, {n_signals} signals, {row_date}) — below quality threshold'
                return True, f'avg EV={avg_ev_pct:+.2f}%, green={green_cnt}, {n_signals} signals ({row_date})'
    except Exception as e:
        log(f'Signal quality DB read failed ({e}) — falling back to memo file')

    # Fallback: parse signal_patterns.md (legacy path; will be removed after one full cycle)
    mem_path = ROOT / 'workspaces' / 'default' / 'memory' / 'signal_patterns.md'
    try:
        text = mem_path.read_text()
        import re
        entry_lines = [ln for ln in text.splitlines() if 'avgEV=' in ln]
        if not entry_lines:
            return True, 'no signal pattern data — allowing trade step'
        last = entry_lines[-1]
        ev_match    = re.search(r'avgEV=([+-]?\d+\.?\d*)%', last)
        green_match = re.search(r'EV\+=(\d+)', last)
        if not ev_match:
            return True, 'no signal pattern data — allowing trade step'
        avg_ev    = float(ev_match.group(1))
        green_cnt = int(green_match.group(1)) if green_match else 0
        if avg_ev < SIGNAL_QUALITY_THRESHOLDS['min_avg_ev'] and green_cnt < SIGNAL_QUALITY_THRESHOLDS['min_green_count']:
            return False, f'avg EV={avg_ev:+.2f}% ({green_cnt} green signals) — below quality threshold (memo fallback)'
        return True, f'avg EV={avg_ev:+.2f}%, green={green_cnt} (memo fallback)'
    except Exception as e:
        log(f'Signal quality check failed ({e}) — allowing trade step')
        return True, 'check error'


def _resolve_script(script: str, run_date: str) -> tuple[list[str], int]:
    """Map step script-name → (argv, timeout_seconds).

    The 10am cycle mixes Python (execution engine, handoff builder, trade
    agent, alpaca executor, report emitter, queue drain) with Node (the
    collector wrapper). The dispatcher keeps scripts in the module that
    owns them instead of forcing everything into src/execution/.
    """
    py_exec = ROOT / 'src' / 'execution' / f'{script}.py'
    py_pipe = ROOT / 'src' / 'pipeline'  / f'{script}.py'
    js_pipe = ROOT / 'src' / 'pipeline'  / f'{script}.js'

    # PIPELINE_DRY_RUN=1 (Tier 3) → append --dry-run to every step. Each
    # script's --dry-run handler is responsible for skipping its own
    # external writes (DB, parquet, Discord) while still running enough
    # logic to validate the cycle's plumbing. PIPELINE_ALPACA_DRY_RUN=1
    # is preserved as the legacy alpaca-only switch.
    full_dry = os.environ.get('PIPELINE_DRY_RUN') == '1'
    alpaca_dry = os.environ.get('PIPELINE_ALPACA_DRY_RUN') == '1'

    def _maybe_dry(argv):
        if full_dry:
            argv.append('--dry-run')
        elif alpaca_dry and script == 'alpaca_executor':
            argv.append('--dry-run')
        return argv

    if py_pipe.exists():
        return (_maybe_dry(['python3', str(py_pipe), '--date', run_date]), 600)
    if js_pipe.exists():
        # Collector: the longest step. A warm-cache cycle is minutes; a cold
        # cycle with wide fundamentals/options gaps has hit the 60 min cap
        # multiple times this week (3 of 4 runs timed out at 3600s on the
        # options-chain phase with 401 tickers). Bumped to 90 min as a
        # buffer — the LLM trade step is now deterministic (~1s) so the
        # total cycle budget is dominated by collect; we have plenty of
        # headroom before market close. Re-evaluate if the 90 min cap is
        # also chronically hit.
        return (_maybe_dry(['node', str(js_pipe)]), 5400)
    # default: src/execution/<script>.py
    timeout = 1620 if script == 'trade_agent_llm' else 300
    return (_maybe_dry(['python3', str(py_exec), '--date', run_date]), timeout)


class CycleAbort(Exception):
    """Raised when a step exits with code 2 (auth/config) under strict
    exit-code discipline. Distinct from a regular step failure: the
    orchestrator does NOT retry, write a checkpoint, or attempt the next
    step — the cycle exits with the daily completion sentinel set to
    `aborted_auth` so tomorrow's cron starts clean rather than retrying
    into the same auth wall."""
    def __init__(self, step, rc, detail=''):
        super().__init__(f'{step} exited {rc} (auth/config) — cycle aborted')
        self.step = step
        self.rc = rc
        self.detail = detail


def run_step(script, run_date, env):
    """
    Spawn a pipeline script. Returns (ok, rc) so callers can route on
    exit-code discipline (Tier 3): 0=success, 1=transient/data error
    (existing retry/skip path), 2=auth/config error (raise CycleAbort).

    Backwards-compat: the old caller path used `ok = run_step(...)` —
    truthy == success — and that still works because the bool() of a
    tuple is True iff non-empty. Callers that care about rc must
    unpack explicitly; everywhere else `if ok:` continues to work.
    """
    import threading
    cmd, timeout = _resolve_script(script, run_date)
    # Stdout-idle watchdog: if the subprocess emits nothing for this many
    # seconds we treat it as wedged and SIGTERM it. The 2026-04-29 cycle
    # got stuck in collector Phase 3 (options) for 30+ minutes with zero
    # stdout — a half-open Polygon TCP stream wedged the await. The
    # underlying httpGet bug is fixed in collector.js, but this watchdog
    # is the belt-and-suspenders defense for any future silent stall.
    # Override per-step via STEP_STDOUT_IDLE_MAX_S env (default 600s).
    stdout_idle_max_s = int(os.environ.get('STEP_STDOUT_IDLE_MAX_S', '600'))
    log(f'Starting {script} timeout={timeout}s stdout_idle_max={stdout_idle_max_s}s (cmd: {" ".join(cmd)})...')
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        last_output_ts = [time.time()]
        def _pump():
            for line in proc.stdout:
                last_output_ts[0] = time.time()
                line = line.rstrip()
                if line:
                    print(f'  [{script}] {line}', flush=True)
        t = threading.Thread(target=_pump, daemon=True)
        t.start()
        deadline = time.time() + timeout
        wedged = False
        try:
            while True:
                # Poll on a 30s grain — light on CPU, fast enough to detect
                # both hard-timeout and stdout-idle stalls.
                try:
                    rc = proc.wait(timeout=30)
                    break
                except subprocess.TimeoutExpired:
                    pass
                now = time.time()
                if now >= deadline:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                idle_s = now - last_output_ts[0]
                if idle_s > stdout_idle_max_s:
                    log(f'{script} stdout idle {int(idle_s)}s > {stdout_idle_max_s}s — wedge detected, SIGTERM')
                    wedged = True
                    proc.terminate()
                    try: proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill(); proc.wait()
                    t.join(timeout=5)
                    return (False, -2)   # rc=-2 distinguishes wedge from hard-timeout
        except subprocess.TimeoutExpired:
            log(f'{script} timed out after {timeout}s — SIGTERM')
            proc.terminate()
            try: proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
            t.join(timeout=5)
            return (False, -1)   # treat timeout as a regular failure (rc=-1)
        if wedged:
            return (False, -2)
        t.join(timeout=5)
        if rc != 0:
            log(f'{script} exited {rc}')
            return (False, rc)
        log(f'{script} done.')
        return (True, 0)
    except Exception as e:
        log(f'{script} error: {e}')
        return (False, -1)


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--date',         default=str(date.today()))
    parser.add_argument('--force-resume', action='store_true',
                        help='Ignore existing lock and resume from checkpoint')
    args = parser.parse_args()

    run_date     = args.date
    postgres_uri = os.environ.get('POSTGRES_URI', '')
    if not postgres_uri:
        log('POSTGRES_URI not set — aborting')
        sys.exit(2)   # auth/config error per Tier 3 exit-code discipline

    # ── Pre-flight: doctor --required-only ──────────────────────────────────
    # Exit 2 from the doctor means a critical dependency is broken (Alpaca
    # auth failed, Postgres unreachable, env vars missing). Burning the
    # collect step on a broken setup just produces a partial cycle that has
    # to be cleaned up later — abort up front instead.
    try:
        doctor_proc = subprocess.run(
            [sys.executable, str(ROOT / 'src' / 'maintenance' / 'doctor.py'),
             '--required-only', '--json'],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if doctor_proc.returncode == 2:
            try:
                payload = json.loads(doctor_proc.stdout)
                fails = [c for c in payload.get('checks', []) if c.get('severity') == 'fail']
                detail = '; '.join(f'{c["name"]}: {c["detail"]}' for c in fails) or 'see doctor output'
            except json.JSONDecodeError:
                detail = doctor_proc.stdout[:200]
            log(f'doctor pre-flight FAILED — aborting cycle: {detail}')
            sys.exit(2)
        elif doctor_proc.returncode == 1:
            log('doctor pre-flight reports warnings (continuing)')
        else:
            log('doctor pre-flight OK')
    except subprocess.TimeoutExpired:
        log('doctor pre-flight timed out (>30s) — continuing without it')
    except Exception as exc:
        log(f'doctor pre-flight error: {type(exc).__name__}: {exc} — continuing')

    # engine.py, alpaca_executor.py etc import `strategies.xxx` / `database.xxx`
    # as top-level packages — those live under src/, so PYTHONPATH needs both
    # ROOT (for `src.xxx` imports) and ROOT/src (for bare-package imports).
    _pp_parts = [str(ROOT), str(ROOT / 'src')]
    if os.environ.get('PYTHONPATH'):
        _pp_parts.append(os.environ['PYTHONPATH'])
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(_pp_parts)}

    r = get_redis()

    # ── Idempotency: skip if pipeline already finished today ──────────────────
    # Set by mark_completed() at the end of a successful run. Covers both the
    # external 21:30 UTC cron and collector auto-spawn chains — whichever wins
    # the race completes all 5 steps, and subsequent triggers exit cleanly.
    if not args.force_resume and is_completed_today(r, run_date):
        log(f'Pipeline already completed for {run_date} — skipping (use --force-resume to re-run)')
        sys.exit(0)

    # ── Checkpoint / resume ───────────────────────────────────────────────────
    checkpoint  = read_checkpoint(r)
    completed   = set()

    if checkpoint:
        cp_date = checkpoint.get('run_date')
        if cp_date == run_date:
            completed = set(checkpoint['completed_steps'])
            paused_at = checkpoint.get('paused_at', '')
            log(f'Resume detected — date={cp_date}, completed={sorted(completed)}, paused_at={paused_at}')
            notify(
                f'⚡ **Pipeline resuming** — budget recovered | {run_date}\n'
                f'Picking up after: **{sorted(completed)[-1] if completed else "start"}** | '
                f'Remaining: {[s for k, s in STEPS if k not in completed]}'
            )
        else:
            log(f'Checkpoint is for {cp_date}, not {run_date} — starting fresh')
            clear_checkpoint(r)

    # ── Prevent concurrent runs ───────────────────────────────────────────────
    if not args.force_resume:
        if not acquire_lock(r, run_date):
            log(f'Pipeline already running for {run_date} — exiting (use --force-resume to override)')
            sys.exit(0)
    else:
        # Force resume: refresh lock
        r.set(f'{LOCK_KEY}:{run_date}', '1', ex=LOCK_TTL)

    log(f'Pipeline starting for {run_date} | steps: {[k for k, _ in STEPS]}')

    # Agent status mapping: step_key → (agent_id, busy_task, idle_task).
    # Covers the 10am cycle step list.
    STEP_AGENTS = {
        'collect':     ('databot',      f'Collecting data: {run_date}',             None),
        'signals':     ('databot',      f'Running strategy signals: {run_date}',    None),
        'handoff':     ('researchdesk', f'Building TradeJohn handoff: {run_date}',  None),
        'trade':       ('tradedesk',    f'TradeJohn signal generation: {run_date}', None),
        'alpaca':      ('tradedesk',    f'Submitting Alpaca orders: {run_date}',    None),
        'report':      ('tradedesk',    f'Daily report: {run_date}',                'Steady-state — awaiting next cycle'),
    }

    try:
      try:
        for step_key, script in STEPS:

            # Skip already-completed steps
            if step_key in completed:
                log(f'Skipping {script} (already done)')
                continue

            # Signal quality gate before trade step
            if step_key == 'trade':
                sq_ok, sq_reason = check_signal_quality(run_date)
                if not sq_ok:
                    log(f'Signal quality gate blocked trade step: {sq_reason}')
                    notify(
                        f'⚠️ **Trade step skipped — signal quality gate** | {run_date}\n'
                        f'Reason: {sq_reason}\n'
                        f'No positions will be opened this cycle. Review strategy parameters or regime alignment.'
                    )
                    release_lock(r, run_date)
                    sys.exit(0)

            # Budget check before LLM-adjacent steps
            if step_key in BUDGET_CHECK_BEFORE:
                ok, reason = check_budget(r)
                if not ok:
                    log(f'Budget check failed before {script}: {reason}')
                    write_checkpoint(r, completed, run_date, reason)
                    notify(
                        f'⏸️ **Pipeline paused — {reason}** | {run_date}\n'
                        f'Completed: {sorted(completed) or "none"} | Waiting on: **{script}** + remaining steps\n'
                        f'Will auto-resume when budget recovers (checked every 30 min).'
                    )
                    release_lock(r, run_date)
                    sys.exit(0)

            # Update agent status → busy
            agent_info = STEP_AGENTS.get(step_key)
            if agent_info:
                set_agent_status(r, agent_info[0], 'busy', agent_info[1])

            # #pipeline-feed: phase boundary START
            _t0 = time.time()
            pipeline_feed(f'▶️ `{step_key}` starting ({run_date})')

            # Run the step
            ok, rc = run_step(script, run_date, env)

            # Tier 3 exit-code discipline (gated): rc == 2 means auth/config
            # error — abort the cycle without retrying. Behind the
            # OPENCLAW_STRICT_EXIT_CODES feature flag for one cycle so we
            # can validate the routing before flipping default-on.
            if rc == 2 and os.environ.get('OPENCLAW_STRICT_EXIT_CODES') == '1':
                log(f'Step {script} exited 2 (auth/config) — raising CycleAbort')
                fail_channel = STEP_FAILURE_CHANNEL.get(step_key, 'pipeline-feed')
                notify(
                    f'🚨 **Pipeline AUTH/CONFIG ABORT: {script}** | {run_date}\n'
                    f'Exit code 2 — credentials revoked or required env var '
                    f'missing. Cycle will NOT retry.\n'
                    f'Completed: {sorted(completed) or "none"}\n'
                    f'Run `python3 src/maintenance/doctor.py` to diagnose.',
                    channel=fail_channel,
                )
                _emit_maintenance_report(run_date, failed_step=step_key,
                                          completed_steps=sorted(completed),
                                          error_msg=f'{script} exited 2 (auth/config)')
                release_lock(r, run_date)
                # Mark cycle aborted so tomorrow's cron sees a clean slate.
                # We re-use mark_completed with a sentinel value rather than
                # leaving the lock open.
                try: mark_completed(r, run_date, status='aborted_auth')
                except TypeError:
                    # Older mark_completed signature without status kwarg
                    mark_completed(r, run_date)
                raise CycleAbort(step_key, rc, detail=f'{script} returned exit 2')

            # #pipeline-feed: phase boundary END
            dt = int(time.time() - _t0)
            icon = '✅' if ok else '❌'
            pipeline_feed(f'{icon} `{step_key}` {"done" if ok else "FAILED"} in {dt}s ({run_date})')

            # Update agent status → idle
            if agent_info:
                set_agent_status(r, agent_info[0], 'idle', agent_info[2])

            if ok:
                completed.add(step_key)
                # Update checkpoint after each success so resume works if we crash mid-pipeline
                write_checkpoint(r, completed, run_date, 'in_progress')
            else:
                log(f'Step {script} failed (rc={rc}) — aborting pipeline')
                fail_channel = STEP_FAILURE_CHANNEL.get(step_key, 'pipeline-feed')
                notify(
                    f'❌ **Pipeline step failed: {script}** (rc={rc}) | {run_date}\n'
                    f'Completed: {sorted(completed) or "none"}\n'
                    f'Log: `logs/pipeline_orchestrator_{run_date}.log`',
                    channel=fail_channel,
                )
                # Always-emit maintenance report — fires even on abort so the
                # operator gets the same daily digest shape every day, with
                # the failure flagged explicitly rather than silently absent.
                _emit_maintenance_report(run_date, failed_step=step_key,
                                          completed_steps=sorted(completed),
                                          error_msg=f'{script} exited {rc}')
                release_lock(r, run_date)
                sys.exit(1)

        # All steps done
        clear_checkpoint(r)
        mark_completed(r, run_date)
        log(f'Pipeline complete for {run_date} — all {len(STEPS)} steps done.')
        pipeline_feed(f'🏁 **Cycle complete** — {run_date} · {len(STEPS)}/{len(STEPS)} steps ok')

        # Final action: nudge the dashboard to re-render against the fresh DB
        # state. Non-blocking; failure here never fails the pipeline.
        try:
            broadcast_dashboard_refresh(run_date)
            log('Dashboard broadcast fired — UI will auto-refresh.')
        except Exception as e:
            log(f'Dashboard broadcast failed ({e}) — pipeline still OK.')

      except CycleAbort as exc:
        # Exit-code-2 abort path: the strict-exit-codes branch above already
        # marked completion=aborted_auth and posted the alert. Surface the
        # exit non-zero (1) so external cron sees the cycle didn't finish.
        log(f'Cycle aborted (auth/config): {exc}')
        sys.exit(1)
    finally:
        release_lock(r, run_date)

def refresh_earnings_calendar():
    """
    Refresh earnings.parquet with upcoming earnings (next 90 days).
    Uses FMP stable/earnings-calendar endpoint.
    Merges into data/master/earnings.parquet.
    """
    import asyncio, os
    import aiohttp
    import pandas as pd
    from datetime import date, timedelta
    from pathlib import Path

    FMP_KEY  = os.environ.get("FMP_API_KEY", "")
    MASTER   = Path(__file__).resolve().parent.parent.parent / "data" / "master"
    FMP_BASE = "https://financialmodelingprep.com/stable"

    if not FMP_KEY:
        log("[earnings] FMP_API_KEY not set — skipping")
        return

    async def _fetch():
        from_d = date.today().isoformat()
        to_d   = (date.today() + timedelta(days=90)).isoformat()
        url    = f"{FMP_BASE}/earnings-calendar"
        params = {"from": from_d, "to": to_d, "apikey": FMP_KEY}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status == 200:
                    return await r.json()
                log(f"[earnings] HTTP {r.status}")
                return []

    try:
        data = asyncio.run(_fetch())
        if not data:
            log("[earnings] No upcoming earnings returned")
            return

        df = pd.DataFrame(data).rename(columns={
            "symbol":           "ticker",
            "epsActual":        "eps_actual",
            "epsEstimated":     "eps_estimated",
            "revenueActual":    "revenue_actual",
            "revenueEstimated": "revenue_estimated",
            "lastUpdated":      "last_updated",
        })
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date", "ticker"])

        out = MASTER / "earnings.parquet"
        if out.exists():
            existing = pd.read_parquet(out)
            existing["date"] = pd.to_datetime(existing["date"], errors="coerce")
            df = pd.concat([existing, df]).drop_duplicates(subset=["ticker","date"]).sort_values(["ticker","date"])

        df.to_parquet(out, index=False)
        upcoming = df[df["date"] >= pd.Timestamp.today()]
        log(f"[earnings] Refreshed: {len(upcoming)} upcoming events across {upcoming['ticker'].nunique()} tickers")

    except Exception as e:
        log(f"[earnings] Refresh failed: {e}")

