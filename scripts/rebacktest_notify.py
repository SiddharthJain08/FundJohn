#!/usr/bin/env python3
"""Watch rebacktest-driver.service; when it exits (COMPLETE or INTERRUPTED),
post a one-shot summary to #botjohn-log. Detached, fail-soft, never raises into
the run. Completion is judged by the driver's own 'DONE ok=' line in driver.log
(the driver runs --collect so its systemd unit is gone post-exit) + the
authoritative DB run_at count. Webhook pattern mirrors regime_blended_sizer.py."""
import json, os, re, subprocess, sys, time, urllib.request
from pathlib import Path

DRIVER = 'rebacktest-driver.service'
LOGDIR = Path(os.environ.get('REBACKTEST_LOGDIR', '/var/log/openclaw/rebacktest'))
STATE = LOGDIR / 'state.json'
DRIVER_LOG = LOGDIR / 'driver.log'
POLL_S = 60
MAX_S = 30 * 3600  # 30h safety cap


def _is_active(unit):
    return subprocess.run(['systemctl', 'is-active', unit],
                          capture_output=True, text=True).stdout.strip() == 'active'


def _start_ts():
    return json.loads(STATE.read_text())['start_ts']


def _webhook_and_count(start_ts):
    import psycopg2
    with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM strategy_backtest_runs "
                    "WHERE primary_window=true AND run_at > %s", (start_ts,))
        done = cur.fetchone()[0]
        cur.execute("SELECT webhook_urls->>'botjohn-log' FROM agent_registry "
                    "WHERE webhook_urls->>'botjohn-log' IS NOT NULL LIMIT 1")
        r = cur.fetchone()
    return (r[0] if r else None), done


def _post(url, content):
    data = json.dumps({'content': content[:1900]}).encode()
    req = urllib.request.Request(url, data=data, method='POST',
                                 headers={'Content-Type': 'application/json',
                                          'User-Agent': 'fundjohn-rebacktest/1.0'})
    urllib.request.urlopen(req, timeout=8).read()


def _log_lines():
    try:
        return DRIVER_LOG.read_text().splitlines()
    except Exception:
        return []


def _target(lines):
    for line in lines:
        m = re.search(r'work-list:\s*(\d+)\s+strategies', line)
        if m:
            return int(m.group(1))
    return None


def _summary(lines):
    done_line = next((l for l in reversed(lines) if l.startswith('DONE ok=')), None)
    failed_line = next((l for l in reversed(lines) if l.startswith('FAILED:')), None)
    return done_line, failed_line


def main():
    arm = len(sys.argv) > 1 and sys.argv[1] == '--arm-test'
    if arm:
        try:
            url, _ = _webhook_and_count(_start_ts())
            if url:
                _post(url, '🧪 Re-backtest completion notifier ARMED — will post here '
                           'when rebacktest-driver finishes or is interrupted.')
                print('armed post sent')
            else:
                print('no webhook url')
        except Exception as e:
            print('arm-test failed:', e)
            return 1
        return 0

    waited = 0
    while _is_active(DRIVER) and waited < MAX_S:
        time.sleep(POLL_S)
        waited += POLL_S

    try:
        start_ts = _start_ts()
        url, done = _webhook_and_count(start_ts)
    except Exception as e:
        print('db error:', e)
        return 1
    lines = _log_lines()
    target = _target(lines)
    done_line, failed_line = _summary(lines)

    if _is_active(DRIVER):  # hit MAX_S while still running
        icon, status = '⏳', f'STILL RUNNING after {MAX_S//3600}h (watcher timed out)'
        complete = False
    else:
        complete = done_line is not None  # driver printed its DONE summary => clean exit
        icon = '✅' if complete else '⚠️'
        status = 'COMPLETE' if complete else 'INTERRUPTED (no DONE line — killed/crashed/OOM/timeout)'

    tgt = f'/{target}' if target else ''
    msg = (f'{icon} Re-backtest (true-MTM, OPENCLAW_TRUE_MTM_MARKS=1) {status}\n'
           f'• {done}{tgt} strategies re-backtested (fresh primary_window rows, run_at > {start_ts})\n')
    if done_line:
        msg += f'• {done_line}\n'
    if failed_line:
        msg += f'• {failed_line}\n'
    if complete:
        msg += ('• Next: run the 2 excluded outliers (S_tr_03_bocpd_change_point, '
                'S_pairs_trading_jump_diffusion_intraday), restart the stopped timers, then Phase 1e cascade.')
    else:
        msg += ('• Resumable: re-launch scripts/rebacktest_runner.py (skips already-done via run_at). '
                'Check /var/log/openclaw/rebacktest/driver.log.')
    try:
        if url:
            _post(url, msg)
            print('posted to #botjohn-log')
        else:
            print('no webhook url; message was:\n' + msg)
    except Exception as e:
        print('post failed:', e)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
