#!/usr/bin/env python3
"""
pipeline_orchestrator.py — 10am daily cycle (Phase 2).

Runs after every data collection cycle (spawned by collector.js).
Agents communicate DIRECTLY — Discord posts are write-only for human visibility only.

Pipeline (direct agent-to-agent, no Discord round-trip):
  1. queue_drain.py        → backfill approved columns, prune deprecated ones
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
LOCK_TTL        = 3600   # 1 hour lock TTL (prevents double-run)
COMPLETED_KEY   = 'pipeline:completed'     # idempotency sentinel; set when all 5 steps done
COMPLETED_TTL   = 86400  # 24h — covers full-day re-trigger window

# Ordered pipeline steps: key → script name (without .py)
STEPS = [
    ('queue_drain', 'queue_drain'),           # src/pipeline/queue_drain.py — backfill + deprecate
    ('collect',     'run_collector_once'),    # Node wrapper: one cycle of collector.js (parquet-primary)
    ('signals',     'engine'),                # zero-LLM strategy executor → execution_signals
    ('handoff',     'trade_handoff_builder'), # deterministic features → handoff:{date}:structured
    ('trade',       'trade_agent_llm'),       # Claude LLM: TradeJohn sizing + signal generation
    ('alpaca',      'alpaca_executor'),       # Auto-submit sized orders to Alpaca paper
    ('report',      'send_report'),           # Greenlist → #trade-signals, veto digest → #trade-reports
]

# Budget check required before LLM-adjacent steps. `trade` is the only Claude
# call in the 10am cycle now — all other steps are deterministic / zero-token.
BUDGET_CHECK_BEFORE = {'trade'}

# Notify here on pause/resume/error (DataBot → #strategy-memos)
NOTIFY_WEBHOOK = 'https://discord.com/api/webhooks/1492623936247300186/BFUwcy91xaIzq_GwP_YvON9-N9HhSilx-wDQ6MhISRYoSx9LrNYyXsDQeaSzxfwimEBi'


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


def mark_completed(r, run_date):
    """Set the once-per-day sentinel so repeat triggers exit early."""
    try:
        r.set(f'{COMPLETED_KEY}:{run_date}', '1', ex=COMPLETED_TTL)
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


def notify(msg):
    try:
        r = requests.post(NOTIFY_WEBHOOK, json={'content': msg}, timeout=10)
        if not r.ok:
            log(f'Notify failed: {r.status_code}')
    except Exception as e:
        log(f'Notify error: {e}')


# ── Pipeline-feed channel posts (Phase 4) ────────────────────────────────────
# Concise phase-boundary pings for operator visibility without per-signal noise.
# Uses the DataBot REST API (same pattern as send_report.py). Channel ID is
# discovered once per process and cached.

_PIPELINE_FEED_CID: str | None = None

def _pipeline_feed_channel_id():
    global _PIPELINE_FEED_CID
    if _PIPELINE_FEED_CID is not None:
        return _PIPELINE_FEED_CID
    token = os.environ.get('DATABOT_TOKEN') or os.environ.get('BOT_TOKEN')
    if not token:
        _PIPELINE_FEED_CID = ''
        return ''
    try:
        headers = {'Authorization': f'Bot {token}'}
        r = requests.get('https://discord.com/api/v10/users/@me/guilds', headers=headers, timeout=5)
        if not r.ok:
            _PIPELINE_FEED_CID = ''
            return ''
        for g in r.json():
            rc = requests.get(f"https://discord.com/api/v10/guilds/{g['id']}/channels",
                              headers=headers, timeout=5)
            if not rc.ok:
                continue
            for ch in rc.json():
                if ch.get('name') == 'pipeline-feed' and ch.get('type') == 0:
                    _PIPELINE_FEED_CID = ch['id']
                    return _PIPELINE_FEED_CID
    except Exception as e:
        log(f'pipeline-feed lookup failed: {e}')
    _PIPELINE_FEED_CID = ''
    return ''


def pipeline_feed(msg):
    """Post a concise one-liner to #pipeline-feed. Non-blocking; failures
    never fail the pipeline — they just get logged."""
    cid = _pipeline_feed_channel_id()
    if not cid:
        return
    token = os.environ.get('DATABOT_TOKEN') or os.environ.get('BOT_TOKEN')
    try:
        requests.post(
            f'https://discord.com/api/v10/channels/{cid}/messages',
            headers={'Authorization': f'Bot {token}', 'Content-Type': 'application/json'},
            json={'content': msg[:1900]},
            timeout=5,
        )
    except Exception as e:
        log(f'pipeline_feed post failed: {e}')


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
    py_exec = ROOT / 'src' / 'execution' / f'{script}'
    py_pipe = ROOT / 'src' / 'pipeline'  / f'{script}'
    js_pipe = ROOT / 'src' / 'pipeline'  / f'{script}.js'

    if py_pipe.exists():
        return (['python3', str(py_pipe), '--date', run_date], 600)
    if js_pipe.exists():
        # Collector: historically the longest step; allow up to 25 min for a
        # cold-cache cycle against the full universe. The collector reads
        # today's date itself and ignores any --date arg we might pass.
        return (['node', str(js_pipe)], 1500)
    # default: src/execution/<script>.py
    timeout = 720 if script == 'trade_agent_llm' else 300
    return (['python3', str(py_exec), '--date', run_date], timeout)


def run_step(script, run_date, env):
    """
    Spawn a pipeline script. Returns True on success.
    Scripts are run as subprocesses so they have their own context.
    """
    cmd, timeout = _resolve_script(script, run_date)
    log(f'Starting {script} timeout={timeout}s (cmd: {" ".join(cmd)})...')
    try:
        result = subprocess.run(
            cmd, cwd=str(ROOT), env=env,
            capture_output=True, text=True, timeout=timeout,
        )
        # Echo output for journal
        for line in (result.stdout + result.stderr).splitlines():
            if line.strip():
                print(f'  [{script}] {line}', flush=True)
        if result.returncode != 0:
            log(f'{script} exited {result.returncode}')
            return False
        log(f'{script} done.')
        return True
    except subprocess.TimeoutExpired:
        log(f'{script} timed out after 300s')
        return False
    except Exception as e:
        log(f'{script} error: {e}')
        return False


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
        sys.exit(1)

    env = {**os.environ, 'PYTHONPATH': str(ROOT)}

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
        'queue_drain': ('databot',      f'Draining data queue: {run_date}',         None),
        'collect':     ('databot',      f'Collecting data: {run_date}',             None),
        'signals':     ('databot',      f'Running strategy signals: {run_date}',    None),
        'handoff':     ('researchdesk', f'Building TradeJohn handoff: {run_date}',  None),
        'trade':       ('tradedesk',    f'TradeJohn signal generation: {run_date}', None),
        'alpaca':      ('tradedesk',    f'Submitting Alpaca orders: {run_date}',    None),
        'report':      ('tradedesk',    f'Daily report: {run_date}',                'Steady-state — awaiting next cycle'),
    }

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
            ok = run_step(script, run_date, env)

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
                log(f'Step {script} failed — aborting pipeline')
                notify(
                    f'❌ **Pipeline step failed: {script}** | {run_date}\n'
                    f'Completed: {sorted(completed) or "none"}\n'
                    f'Check systemd journal: `journalctl -u johnbot.service -n 50`'
                )
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

