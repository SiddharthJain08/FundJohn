#!/usr/bin/env python3
"""
pipeline_orchestrator.py — Full post-collection signal pipeline.

Runs after every data collection cycle (spawned by collector.js).
Agents communicate DIRECTLY — Discord posts are write-only for human visibility only.

Pipeline (direct agent-to-agent, no Discord round-trip):
  1. post_memos.py      → run engine → post strategy memo to #strategy-memos (DataBot)
  2. research_report.py → signal enrichment (HV/beta/EV, pure Python, no LLM)
  3. trade_agent_llm.py → TradeJohn Claude → sizing + signals to #trade-signals (TradeDesk)
  4. portfolio_report.py → portfolio metrics to #trade-reports (TradeDesk)

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

# Ordered pipeline steps: key → script name (without .py)
STEPS = [
    ('memos',    'post_memos'),
    ('research', 'research_report'),   # pure Python: HV/beta/EV signal enrichment
    ('trade',    'trade_agent_llm'),   # Claude LLM: TradeJohn sizing + signal generation
    ('report',   'portfolio_report'),
]

# Budget check required before these steps
# memos triggers engine run; trade now invokes TradeJohn Claude (LLM tokens)
BUDGET_CHECK_BEFORE = {'memos', 'trade'}

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


SIGNAL_QUALITY_THRESHOLDS = {
    'min_avg_ev':      -0.5,   # avg EV must be > -0.5% to allow trade step
    'min_green_count':  1,     # at least 1 green signal required
}

def check_signal_quality(run_date):
    """
    Read signal_patterns.md from workspace memory to determine if avg EV is
    too negative to justify running the trade step.
    Returns (ok: bool, reason: str).
    """
    mem_path = ROOT / 'workspaces' / 'default' / 'memory' / 'signal_patterns.md'
    try:
        text = mem_path.read_text()
        import re
        # Read the LAST entry (most recent run), not the first.
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
            return False, f'avg EV={avg_ev:+.2f}% ({green_cnt} green signals) — below quality threshold'
        return True, f'avg EV={avg_ev:+.2f}%, green={green_cnt}'
    except Exception as e:
        log(f'Signal quality check failed ({e}) — allowing trade step')
        return True, 'check error'


def run_step(script, run_date, env):
    """
    Spawn a pipeline script. Returns True on success.
    Scripts are run as subprocesses so they have their own context.
    """
    path    = ROOT / 'src' / 'execution' / f'{script}.py'
    cmd     = ['python3', str(path), '--date', run_date]
    # TradeJohn Claude invocation needs more time than pure-Python steps
    timeout = 420 if script == 'trade_agent_llm' else 300
    log(f'Starting {script}.py (timeout={timeout}s)...')
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
            log(f'{script}.py exited {result.returncode}')
            return False
        log(f'{script}.py done.')
        return True
    except subprocess.TimeoutExpired:
        log(f'{script}.py timed out after 300s')
        return False
    except Exception as e:
        log(f'{script}.py error: {e}')
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

    # Agent status mapping: step_key → (agent_id, busy_task, idle_task)
    STEP_AGENTS = {
        'memos':    ('databot',      f'Generating signals: {run_date}',          'Steady-state — awaiting next cycle'),
        'research': ('researchdesk', f'Signal enrichment (HV/beta/EV): {run_date}', None),
        'trade':    ('tradedesk',    f'TradeJohn signal generation: {run_date}', None),
        'report':   ('tradedesk',    f'Portfolio report: {run_date}',            None),
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

            # Run the step
            ok = run_step(script, run_date, env)

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
        log(f'Pipeline complete for {run_date} — all {len(STEPS)} steps done.')

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

