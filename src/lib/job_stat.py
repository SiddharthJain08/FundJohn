"""job_stat — run accounting for systemd timers / crons (table scheduled_job_runs, mig 167).

Every public function NEVER raises; failures print a '[job_stat] ...' line.
Kill switch: OPENCLAW_JOB_STAT=0.  CLI (systemd ExecStartPre/ExecStopPost):
    job_stat.py start <unit> | job_stat.py finish <unit>     (always exits 0)
"""
from __future__ import annotations

import os
import socket
import sys
from typing import Mapping


def _disabled() -> bool:
    if os.environ.get('OPENCLAW_JOB_STAT') == '0':
        print('[job_stat] disabled')
        return True
    return False


def _connect():
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        print('[job_stat] skipped: POSTGRES_URI unset')
        return None
    import psycopg2
    conn = psycopg2.connect(uri, connect_timeout=5)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout='5s'")
    return conn


def record_start(job, *, source='cron', invocation_id=None, pid=None, note=None):
    try:
        if _disabled():
            return None
        conn = _connect()
        if conn is None:
            return None
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO scheduled_job_runs "
                    "(job_name, source, host, pid, invocation_id, note) "
                    "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                    (job, source, socket.gethostname(), pid, invocation_id, note))
                row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        print(f'[job_stat] {job} start write failed: {type(e).__name__}: {e}')
        return None


def record_finish(job, rc, *, source='cron', success=None, invocation_id=None, note=None):
    try:
        if _disabled():
            return False
        conn = _connect()
        if conn is None:
            return False
        if success is None:
            success = rc == 0
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE scheduled_job_runs SET finished_at=NOW(), success=%s, rc=%s, "
                    "note=COALESCE(%s, note) WHERE id = ("
                    " SELECT id FROM scheduled_job_runs WHERE job_name=%s AND source=%s "
                    " AND finished_at IS NULL AND invocation_id IS NOT DISTINCT FROM %s "
                    " ORDER BY started_at DESC LIMIT 1) RETURNING id",
                    (success, rc, note, job, source, invocation_id))
                if not cur.fetchall():
                    cur.execute(
                        "INSERT INTO scheduled_job_runs (job_name, source, started_at, "
                        "finished_at, success, rc, host, invocation_id, note) "
                        "VALUES (%s,%s,NOW(),NOW(),%s,%s,%s,%s,%s)",
                        (job, source, success, rc, socket.gethostname(), invocation_id,
                         'no-start-row; ' + (note or '')))
            return True
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        print(f'[job_stat] {job} finish write failed: {type(e).__name__}: {e}')
        return False


def systemd_outcome(env: Mapping[str, str]):
    result = env.get('SERVICE_RESULT', '')
    code = env.get('EXIT_CODE', '')
    status = env.get('EXIT_STATUS', '')
    rc = int(status) if code == 'exited' and status.lstrip('-').isdigit() else None
    note = f'result={result} exit_code={code} exit_status={status}'
    return rc, result == 'success', note


def main(argv=None) -> int:
    try:
        argv = sys.argv[1:] if argv is None else argv
        if len(argv) < 2 or argv[0] not in ('start', 'finish'):
            print('[job_stat] usage: job_stat.py start|finish <unit>')
            return 0
        cmd, unit = argv[0], argv[1]
        inv = os.environ.get('INVOCATION_ID')
        if cmd == 'start':
            record_start(unit, source='systemd', invocation_id=inv, pid=None)
        else:
            rc, ok, note = systemd_outcome(os.environ)
            record_finish(unit, rc, source='systemd', success=ok, invocation_id=inv, note=note)
    except Exception as e:  # noqa: BLE001
        print(f'[job_stat] main failed: {type(e).__name__}: {e}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
