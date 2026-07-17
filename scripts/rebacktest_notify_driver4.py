#!/usr/bin/env python3
"""Durable completion notifier for the 4-stale-strategy re-backtest
(rebacktest-driver4.service). Waits for the driver to exit, then posts a
full corrected-metrics summary of all 4 strategies to #botjohn-log. Runs as
its own systemd unit so it survives controller-session interruptions.
Throwaway ops script for the 2026-07-05 §7 Phase 1e stale-strategy re-run."""
import json, os, subprocess, sys, time, urllib.request

DRIVER = 'rebacktest-driver4.service'
POLL_S = 60
MAX_S = 8 * 3600
FOUR = ['S_ivol_mispricing_asymmetry', 'S_tr_02_hurst_regime_flip',
        'S_pairs_trading_jump_diffusion_intraday', 'S_tr_03_bocpd_change_point']
START = '2026-07-05T06:50:00Z'


def _is_active(unit):
    return subprocess.run(['systemctl', 'is-active', unit],
                          capture_output=True, text=True).stdout.strip() == 'active'


def _db():
    import psycopg2
    return psycopg2.connect(os.environ['POSTGRES_URI'])


def _rows(conn):
    cur = conn.cursor()
    out = {}
    for sid in FOUR:
        cur.execute("""SELECT run_at::timestamp(0), total_sharpe, total_max_dd_pct, total_trades
                       FROM strategy_backtest_runs WHERE strategy_id=%s AND primary_window=true
                       AND run_at > %s ORDER BY run_at DESC LIMIT 1""", (sid, START))
        out[sid] = cur.fetchone()
    return out


def _webhook(conn):
    cur = conn.cursor()
    cur.execute("SELECT webhook_urls->>'botjohn-log' FROM agent_registry "
                "WHERE webhook_urls->>'botjohn-log' IS NOT NULL LIMIT 1")
    r = cur.fetchone()
    return r[0] if r else None


def _post(url, content):
    data = json.dumps({'content': content[:1900]}).encode()
    req = urllib.request.Request(url, data=data, method='POST',
                                 headers={'Content-Type': 'application/json',
                                          'User-Agent': 'fundjohn-rebacktest-driver4/1.0'})
    urllib.request.urlopen(req, timeout=8).read()


def main():
    waited = 0
    while _is_active(DRIVER) and waited < MAX_S:
        time.sleep(POLL_S)
        waited += POLL_S

    conn = _db()
    rows = _rows(conn)
    landed = [s for s in FOUR if rows[s] is not None]
    still_active = _is_active(DRIVER)
    if still_active:
        icon, status = '⏳', f'STILL RUNNING after {MAX_S//3600}h (notifier timed out)'
    elif len(landed) == 4:
        icon, status = '✅', 'COMPLETE — all 4 stale sized strategies re-backtested'
    else:
        icon, status = '⚠️', f'FINISHED but only {len(landed)}/4 landed (watchdog/crash on the rest)'

    lines = [f'{icon} §7 Phase 1e stale-strategy re-backtest: {status}',
             '(true-MTM + adverse slippage; corrected primary_window rows)']
    for sid in FOUR:
        r = rows[sid]
        if r:
            lines.append(f'• {sid}: Sharpe={float(r[1]):.2f} DD={float(r[2]):.1f}% trades={r[3]} ✓')
        else:
            lines.append(f'• {sid}: NOT landed ✗')
    if len(landed) == 4:
        lines.append('→ All 49 sized strategies now on corrected metrics. Ready for the '
                     'controlled Phase 1e reweight (clamp→5.0, activation slider @0.5, rebuild).')
    else:
        lines.append('→ Re-run the missing via rebacktest_runner --only <sid> --watchdog-min 480 '
                     '(resumable). Check /var/log/openclaw/rebacktest-4stale/driver.log.')

    url = _webhook(conn)
    if url:
        _post(url, '\n'.join(lines))
        print('posted to #botjohn-log')
    else:
        print('no webhook url found')
    print('\n'.join(lines))
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
