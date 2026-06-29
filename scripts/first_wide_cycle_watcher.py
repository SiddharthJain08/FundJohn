#!/usr/bin/env python3
"""SP-7 §6 — first-wide-cycle watcher.

One-shot: after the §6 clamp deletion (2026-06-28), the first weekday 16:15 ET
EOD compute runs the engine on the now-UNCLAMPED per-strategy universe (union
620 -> ~5180). This watcher waits for that EOD compute's `eod_compute_health`
sentinel, builds a summary (universe widening, signals-step health, signal
count, memory/OOM health, fail-opens), and posts it to #botjohn-log.

Trigger: systemd timer at the target date's 20:15 UTC (= 16:15 ET). The service
POLLS for the sentinel (run_at >= 20:00 UTC isolates the EOD lane from intraday
redeploys), so timing slop is irrelevant. On a successful post it self-removes
its own unit files (project one-shot convention).

--dry-run: target the most-recent existing eod_compute_health row instead, PRINT
the summary, do NOT post and do NOT self-clean. Used to validate before arming.
"""
import os
import sys
import time
import json
import subprocess
from datetime import date, datetime, timezone

sys.path.insert(0, '/root/openclaw')
sys.path.insert(0, '/root/openclaw/src')

import psycopg2

TARGET_RUN_DATE = os.environ.get('OPENCLAW_FWC_TARGET_DATE', '2026-06-29')  # first weekday post-§6
EOD_LANE_MIN_UTC = f"{TARGET_RUN_DATE} 20:00:00+00"   # 16:15 ET EOD compute window start
POLL_INTERVAL_S = 90
POLL_MAX_MIN = 55
CHANNEL = os.environ.get('OPENCLAW_FWC_DISCORD_WEBHOOK_NAME', 'botjohn-log')
DRY_RUN = '--dry-run' in sys.argv


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def log(m):
    print(f"[fwc-watch {_now()}] {m}", flush=True)


def _conn():
    return psycopg2.connect(os.environ['POSTGRES_URI'])


def fetch_eod_row(cur):
    """The EOD-compute eod_compute_health row for the target date (latest in the
    20:00+ UTC window). In --dry-run, the most-recent row regardless of date."""
    if DRY_RUN:
        cur.execute("""
            SELECT run_date, run_at, rc, n_strategies_ok, n_strategies_total,
                   regime_ok, universe_size, healthy, detail, panel_max_date
            FROM eod_compute_health ORDER BY run_at DESC LIMIT 1
        """)
    else:
        cur.execute("""
            SELECT run_date, run_at, rc, n_strategies_ok, n_strategies_total,
                   regime_ok, universe_size, healthy, detail, panel_max_date
            FROM eod_compute_health
            WHERE run_date = %s AND run_at >= %s
            ORDER BY run_at DESC LIMIT 1
        """, (TARGET_RUN_DATE, EOD_LANE_MIN_UTC))
    return cur.fetchone()


def fetch_prior_universe(cur, run_date):
    cur.execute("""
        SELECT run_date, universe_size FROM eod_compute_health
        WHERE run_date < %s ORDER BY run_date DESC, run_at DESC LIMIT 1
    """, (str(run_date),))
    return cur.fetchone()


def fetch_signal_counts(cur, run_date):
    try:
        cur.execute("SELECT count(*), count(DISTINCT ticker) FROM execution_signals "
                    "WHERE created_at::date = %s", (str(run_date),))
        return cur.fetchone()
    except Exception:
        cur.connection.rollback()
        return (None, None)


def journal_facts(run_date):
    """Best-effort: pull the engine's own live-universe line + warning flags from
    the johnbot journal for the EOD window."""
    facts = {'union': None, 'fail_open': None, 'empty_warn': 0, 'oom': 0, 'build_failed': 0}
    try:
        out = subprocess.run(
            ['journalctl', '--user', '-u', 'johnbot.service',
             '--since', f'{run_date} 19:55:00 UTC', '--no-pager', '-o', 'cat'],
            capture_output=True, text=True, timeout=30).stdout
    except Exception as e:
        log(f"journal read failed (non-fatal): {e}")
        return facts
    for line in out.splitlines():
        if 'live-universe ON: union' in line:
            # "live-universe ON: union 5180 tickers, 76 strategies, 0 fail-open"
            try:
                facts['union'] = int(line.split('union')[1].split('tickers')[0].strip())
                facts['fail_open'] = int(line.split(',')[-1].strip().split()[0])
            except Exception:
                pass
        if 'empty universe' in line.lower() or 'empty-universe' in line.lower():
            facts['empty_warn'] += 1
        if 'rc=137' in line or 'oom' in line.lower() or 'killed' in line.lower():
            facts['oom'] += 1
        if 'live-universe build failed' in line:
            facts['build_failed'] += 1
    return facts


def johnbot_restarts():
    try:
        out = subprocess.run(['systemctl', '--user', 'show', 'johnbot.service',
                              '-p', 'NRestarts', '--value'], capture_output=True, text=True, timeout=10).stdout.strip()
        return int(out or '0')
    except Exception:
        return None


def build_message(row, prior, sig_counts, jf, nrestarts):
    (run_date, run_at, rc, ok, total, regime_ok, usize, healthy, detail, pmax) = row
    if isinstance(detail, str):
        detail = json.loads(detail)
    detail = detail or {}
    prior_u = prior[1] if prior else None
    n_sig, n_tk = sig_counts
    wide = usize and usize > 1000           # §6 widening took effect
    mem_ok = (jf['oom'] == 0) and (nrestarts is not None)
    ok_all = (rc == 0 and healthy and wide and jf['empty_warn'] == 0
              and (jf['fail_open'] in (0, None)) and jf['oom'] == 0)
    icon = '🟢' if ok_all else ('🟡' if (rc == 0 and healthy) else '🔴')

    arrow = f"{prior_u} → {usize}" if prior_u else f"{usize}"
    lines = [
        f"{icon} **SP-7 §6 — FIRST WIDE CYCLE COMPLETE** (EOD compute {run_date})",
        f"the post-clamp-deletion universe is now live in the daily signals lane.",
        "",
        f"• **universe**: {arrow} tickers"
        + (f"  (resolver union; §6 widening {'CONFIRMED ✅' if wide else 'NOT seen ⚠️'})"),
        f"• **signals step**: rc={rc} · strategies {ok}/{total} ok · regime_ok={regime_ok}"
        + (f" · panel_fresh={detail.get('panel_ok')}" if 'panel_ok' in detail else "")
        + f" · healthy={healthy}",
    ]
    if jf['union'] is not None:
        lines.append(f"• **engine log**: live-universe union={jf['union']}, fail-open={jf['fail_open']}")
    if n_sig is not None:
        lines.append(f"• **signals written**: {n_sig} ({n_tk} distinct tickers)")
    lines.append(
        f"• **memory/OOM**: johnbot NRestarts={nrestarts} · OOM/rc=137 in window={jf['oom']} · "
        f"empty-universe warns={jf['empty_warn']} · build-failed={jf['build_failed']}"
    )
    lines.append(f"   (pre-flip dry-run validated +203MB peak: 3078→3281MB at the 5180 union)")
    lines.append(f"• **run_at**: {run_at}")
    if not ok_all:
        lines.append("")
        lines.append("⚠️ **degraded** — review johnbot journal. ROLLBACK (3 parts): "
                     "`git revert 1896baf` + re-add `OPENCLAW_ENGINE_UNIVERSE_CLAMP=sp500` to .env + restart johnbot.")
    return "\n".join(lines), ok_all


def post(msg):
    if DRY_RUN:
        log("DRY-RUN — would post to #%s:\n%s" % (CHANNEL, msg))
        return True
    try:
        from src.execution.pipeline_orchestrator import post_channel
        return post_channel(CHANNEL, msg)
    except Exception as e:
        log(f"post failed: {e}")
        return False


def self_remove():
    # No shell: fixed argv lists + os.remove (no user input anywhere here).
    if DRY_RUN:
        return
    try:
        unit = os.path.expanduser('~/.config/systemd/user')
        subprocess.run(['systemctl', '--user', 'disable', 'first-wide-cycle-watch.timer'],
                       capture_output=True, timeout=10)
        for f in ('first-wide-cycle-watch.timer', 'first-wide-cycle-watch.service'):
            try:
                os.remove(os.path.join(unit, f))
            except FileNotFoundError:
                pass
        subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True, timeout=10)
        log("self-removed timer+service units")
    except Exception as e:
        log(f"self-remove failed (non-fatal): {e}")


def main():
    log(f"start — target={TARGET_RUN_DATE} dry_run={DRY_RUN} poll<= {POLL_MAX_MIN}min")
    deadline = time.time() + POLL_MAX_MIN * 60
    row = None
    while True:
        conn = _conn()
        try:
            with conn.cursor() as cur:
                row = fetch_eod_row(cur)
        finally:
            conn.close()
        if row:
            break
        if time.time() >= deadline:
            log("DEADLINE — no EOD sentinel found")
            post(f"🔴 **SP-7 §6 — FIRST WIDE CYCLE NOT DETECTED** by deadline for {TARGET_RUN_DATE}. "
                 f"The 16:15 ET EOD compute's eod_compute_health row never appeared — it may be "
                 f"delayed, failed, or OOM'd. Check `journalctl --user -u johnbot.service` and the "
                 f"#botjohn-log abort alert. (Watcher left in place for re-check.)")
            return 2
        log(f"sentinel not yet present — sleeping {POLL_INTERVAL_S}s")
        time.sleep(POLL_INTERVAL_S)

    run_date = row[0]
    log(f"sentinel found: run_date={run_date} run_at={row[1]} universe={row[6]} rc={row[2]} healthy={row[7]}")
    conn = _conn()
    try:
        with conn.cursor() as cur:
            prior = fetch_prior_universe(cur, run_date)
            sig_counts = fetch_signal_counts(cur, run_date)
    finally:
        conn.close()
    jf = journal_facts(run_date)
    nr = johnbot_restarts()
    msg, ok_all = build_message(row, prior, sig_counts, jf, nr)
    posted = post(msg)
    log(f"posted={posted} ok_all={ok_all}")
    if posted and not DRY_RUN:
        self_remove()
    return 0 if posted else 1


if __name__ == '__main__':
    sys.exit(main())
