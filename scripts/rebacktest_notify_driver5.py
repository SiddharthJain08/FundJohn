#!/usr/bin/env python3
"""Durable completion notifier for the S_tr_03 re-backtest re-run
(rebacktest-driver5.service, 25h watchdog). Waits for the driver to exit,
then posts the corrected metric (or the watchdog-fail outcome) to
#botjohn-log. Its own MAX_S ceiling (26h) sits just above the driver's
watchdog so it reports the terminal state, not a premature timeout.
Throwaway ops script for the 2026-07-06 §7 Phase 1e S_tr_03 straggler re-run."""
import json, os, subprocess, sys, time, urllib.request

DRIVER = 'rebacktest-driver5.service'
SID = 'S_tr_03_bocpd_change_point'
POLL_S = 120
MAX_S = 26 * 3600
START = '2026-07-06T13:20:00Z'


def _is_active(unit):
    return subprocess.run(['systemctl', 'is-active', unit],
                          capture_output=True, text=True).stdout.strip() == 'active'


def _db():
    import psycopg2
    return psycopg2.connect(os.environ['POSTGRES_URI'])


def _row(conn):
    cur = conn.cursor()
    cur.execute("""SELECT run_at::timestamp(0), total_sharpe, total_max_dd_pct, total_trades
                   FROM strategy_backtest_runs WHERE strategy_id=%s AND primary_window=true
                   AND run_at > %s ORDER BY run_at DESC LIMIT 1""", (SID, START))
    return cur.fetchone()


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
                                          'User-Agent': 'fundjohn-rebacktest-driver5/1.0'})
    urllib.request.urlopen(req, timeout=8).read()


def main():
    waited = 0
    while _is_active(DRIVER) and waited < MAX_S:
        time.sleep(POLL_S)
        waited += POLL_S

    conn = _db()
    row = _row(conn)
    still_active = _is_active(DRIVER)
    if still_active:
        head = f'⏳ §7 S_tr_03 re-backtest: notifier hit its {MAX_S//3600}h ceiling while driver still running'
    elif row:
        head = '✅ §7 S_tr_03 re-backtest COMPLETE (corrected true-MTM + adverse slippage)'
    else:
        head = '⚠️ §7 S_tr_03 re-backtest FINISHED but did NOT land (watchdog again — needs optimization, not brute force)'

    lines = [head]
    if row:
        lines.append(f'• {SID}: Sharpe={float(row[1]):+.2f} DD={float(row[2]):.1f}% trades={row[3]} run_at={row[0]}')
        lines.append('→ 3 of 4 stale strategies now corrected. Only S_pairs (intraday) remains stale — '
                     'pending operator decision (optimize vs set-dormant). Then the gated Phase 1e reweight.')
    else:
        lines.append(f'• {SID}: NOT landed (no corrected primary_window run after {START}).')
        lines.append('→ Daily BOCPD still blew the 25h watchdog — treat like S_pairs: optimize or set-dormant. '
                     'Log: /var/log/openclaw/rebacktest-tr03/')

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
