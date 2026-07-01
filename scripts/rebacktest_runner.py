#!/usr/bin/env python3
"""Resumable, memory-safe, per-strategy re-backtest driver (Phase 1b).
Runs each live-relevant strategy as its own MemoryMax-capped systemd-run unit
with OPENCLAW_TRUE_MTM_MARKS=1, sequential, resumable via run_at. See
docs/superpowers/specs/2026-07-01-rebacktest-harness-design.md."""
import argparse, json, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

REPO = '/root/openclaw'
IMPL_DIR = 'src/strategies/implementations'
DEFAULT_LOG_DIR = '/var/log/openclaw/rebacktest'
COMPETING_UNITS = ['openclaw-weekend-saturday.service', 'openclaw-strategy-backtest-refresh.service']


def build_worklist(primary_sids, manifest, *, include_deprecated=False, only=None, exclude=None):
    strats = manifest.get('strategies', {}) if manifest else {}
    items = []
    for sid in sorted(primary_sids):
        rec = strats.get(sid)
        is_orphan = rec is None
        is_dep = (not is_orphan) and rec.get('state') == 'deprecated'
        if (is_dep or is_orphan) and not include_deprecated:
            continue
        mode = 'strategy-file' if is_orphan else 'strategy-id'
        items.append({'sid': sid, 'mode': mode})
    if only:
        keep = set(only); items = [w for w in items if w['sid'] in keep]
    if exclude:
        drop = set(exclude); items = [w for w in items if w['sid'] not in drop]
    return items


def is_done(latest_run_at, start_ts):
    return latest_run_at is not None and latest_run_at > start_ts


def build_systemd_cmd(item, *, memory_max_g, watchdog_sec, log_path, repo=REPO):
    sid = item['sid']
    if item['mode'] == 'strategy-file':
        target = ['--strategy-file', f'{repo}/{IMPL_DIR}/{sid}.py']
    else:
        target = ['--strategy-id', sid]
    return [
        'systemd-run', '--quiet', '--collect', f'--unit=rebacktest-{sid}', '--wait',
        '-p', f'EnvironmentFile={repo}/.env',
        '-p', f'WorkingDirectory={repo}',
        '-p', f'MemoryMax={memory_max_g}G',
        '-p', f'RuntimeMaxSec={watchdog_sec}',
        '-p', 'Nice=19',
        '-p', f'StandardOutput=append:{log_path}',
        '-p', f'StandardError=append:{log_path}',
        # `/usr/bin/env VAR=val` sets these in the python process itself, which
        # WINS over the unit's EnvironmentFile(.env) regardless of systemd's
        # EnvironmentFile-vs-Environment= precedence. This guarantees the two
        # §7 correction flags are ON even if .env ever gains a conflicting line —
        # the whole point of this harness is the fully-corrected re-backtest
        # (true daily mark-to-market + always-adverse slippage). (We cannot read
        # the 0600-root .env to assert no conflict, so we override at the boundary.)
        '/usr/bin/env', 'OPENCLAW_TRUE_MTM_MARKS=1', 'OPENCLAW_BACKTEST_SLIPPAGE=1',
        f'PYTHONPATH={repo}/src',
        'python3', '-m', 'backtest.unified_backtest', *target,
    ]


def summarize(results):
    ok = sum(1 for r in results if r['status'] == 'ok')
    fail = sum(1 for r in results if r['status'] == 'fail')
    skip = sum(1 for r in results if r['status'] == 'skip')
    return {'ok': ok, 'fail': fail, 'skip': skip,
            'failed_sids': [r['sid'] for r in results if r['status'] == 'fail']}


def _connect():
    import psycopg2
    dsn = os.environ.get('POSTGRES_URI')
    if not dsn:
        print('ERROR: POSTGRES_URI not set', file=sys.stderr); sys.exit(1)
    return psycopg2.connect(dsn)


def _primary_sids(conn):
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT strategy_id FROM strategy_backtest_runs WHERE primary_window=true")
    return {r[0] for r in cur.fetchall()}


def _latest_primary_run_at(conn, sid):
    cur = conn.cursor()
    cur.execute("""SELECT run_at FROM strategy_backtest_runs
                   WHERE strategy_id=%s AND primary_window=true
                   ORDER BY run_at DESC LIMIT 1""", (sid,))
    row = cur.fetchone()
    return row[0] if row else None


def _mem_available_gb():
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) / (1024 * 1024)  # kB -> GB
    return 0.0


def _competing_active():
    try:
        out = subprocess.run(['systemctl', 'is-active', *COMPETING_UNITS],
                             capture_output=True, text=True).stdout
        states = set(out.split())
        return bool(states & {'active', 'activating', 'reloading'})
    except Exception:
        return False


def _load_manifest():
    return json.loads(Path(f'{REPO}/src/strategies/manifest.json').read_text())


def _read_state(state_path):
    """Return the persisted start_ts ISO string, or None if absent/corrupt."""
    try:
        return json.loads(state_path.read_text())['start_ts']
    except Exception:
        return None


def _write_state(state_path, start_ts_str):
    """Atomic write (temp + rename) so a crash mid-write can't corrupt state."""
    tmp = state_path.with_suffix('.tmp')
    tmp.write_text(json.dumps({'start_ts': start_ts_str}))
    tmp.replace(state_path)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--include-deprecated', action='store_true')
    ap.add_argument('--only'); ap.add_argument('--exclude')
    ap.add_argument('--memory-max-g', type=int, default=4)
    ap.add_argument('--watchdog-min', type=int, default=90)
    ap.add_argument('--log-dir', default=DEFAULT_LOG_DIR)
    ap.add_argument('--ram-floor-g', type=float, default=None)  # default = memory_max + 0.5
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args(argv)

    log_dir = Path(a.log_dir); log_dir.mkdir(parents=True, exist_ok=True)
    state_path = log_dir / 'state.json'
    only = a.only.split(',') if a.only else None
    exclude = a.exclude.split(',') if a.exclude else None
    watchdog_sec = a.watchdog_min * 60
    ram_floor = a.ram_floor_g if a.ram_floor_g is not None else a.memory_max_g + 0.5

    if not a.dry_run and _competing_active():
        print('REFUSING: a competing heavy backtest unit is active. Mask its timer first.', file=sys.stderr)
        sys.exit(2)

    conn = _connect()
    conn.autocommit = True  # runner only READS; avoid holding a txn open across each subprocess wait
    primary = _primary_sids(conn)
    manifest = _load_manifest()
    worklist = build_worklist(primary, manifest, include_deprecated=a.include_deprecated,
                              only=only, exclude=exclude)

    # start_ts: dry-run uses a throwaway (never persisted, so it can't poison a
    # later real run's resume timestamp); a real run reuses the persisted value.
    if a.dry_run:
        start_ts_str = datetime.now(timezone.utc).isoformat()
    else:
        start_ts_str = _read_state(state_path)  # None if absent/corrupt
        if start_ts_str is None:
            start_ts_str = datetime.now(timezone.utc).isoformat()
            _write_state(state_path, start_ts_str)
    start_ts = datetime.fromisoformat(start_ts_str)

    print(f'work-list: {len(worklist)} strategies | start_ts={start_ts_str} | '
          f'MemoryMax={a.memory_max_g}G watchdog={a.watchdog_min}min log_dir={log_dir}')
    results = []
    for w in worklist:
        sid = w['sid']
        try:
            if is_done(_latest_primary_run_at(conn, sid), start_ts):
                print(f'SKIP {sid} done'); results.append({'sid': sid, 'status': 'skip'}); continue
            cmd = build_systemd_cmd(w, memory_max_g=a.memory_max_g, watchdog_sec=watchdog_sec,
                                    log_path=str(log_dir / f'{sid}.log'))
            if a.dry_run:
                print('DRY-RUN', ' '.join(cmd)); results.append({'sid': sid, 'status': 'skip'}); continue
            waited = 0
            while _mem_available_gb() < ram_floor and waited < 1800:
                time.sleep(30); waited += 30
            if waited >= 1800:
                print(f'WARN {sid}: RAM floor {ram_floor}G not met after {waited}s; '
                      f'proceeding (MemoryMax={a.memory_max_g}G still contains it)', file=sys.stderr)
            t0 = time.time()
            rc = subprocess.run(cmd).returncode
            done = is_done(_latest_primary_run_at(conn, sid), start_ts)
            status = 'ok' if done else 'fail'
            print(f'{status.upper()} {sid} {int(time.time()-t0)}s rc={rc}')
            results.append({'sid': sid, 'status': status})
        except Exception as e:
            # One strategy's failure (subprocess error, dead DB conn, etc.) must
            # never abort the whole multi-hour run — log, mark FAIL, reconnect, continue.
            print(f'FAIL {sid} runner-exception: {e}', file=sys.stderr)
            results.append({'sid': sid, 'status': 'fail'})
            try:
                conn.close()
            except Exception:
                pass
            try:
                conn = _connect(); conn.autocommit = True
            except Exception as e2:
                print(f'FATAL: cannot reconnect ({e2}); aborting remaining strategies '
                      f'(resume via state.json).', file=sys.stderr)
                break
    s = summarize(results)
    print(f'DONE ok={s["ok"]} fail={s["fail"]} skip={s["skip"]}')
    if s['failed_sids']:
        print('FAILED:', ','.join(s['failed_sids']))
    conn.close()
    return 0 if not s['fail'] else 1


if __name__ == '__main__':
    sys.exit(main())
