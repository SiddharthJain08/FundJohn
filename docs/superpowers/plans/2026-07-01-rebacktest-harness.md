# Re-backtest Harness (Phase 1b) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** `scripts/rebacktest_runner.py` — a resumable, memory-safe, per-strategy re-backtest driver that runs each live-relevant strategy (with an existing `primary_window` backtest, excluding deprecated) as its own memory-capped `systemd-run` unit with `OPENCLAW_TRUE_MTM_MARKS=1`, sequential, resumable via `run_at`, with an OK/FAIL/SKIP log and `--dry-run`.

**Architecture:** Pure, unit-tested helpers (`build_worklist`, `is_done`, `build_systemd_cmd`, `summarize`) + a thin `main` driver (DB enumerate → manifest classify → work-list → per-strategy launch loop with RAM gate + run_at resume). Spec: `docs/superpowers/specs/2026-07-01-rebacktest-harness-design.md`.

**Tech Stack:** Python 3, psycopg2, systemd-run, `unittest`.

## Global Constraints

- PATH-SCOPED commit: stage EXACTLY `scripts/rebacktest_runner.py` and `tests/test_rebacktest_runner.py`. NEVER `git add -A`/`.`. Live tree has UNRECOVERABLE WIP (`src/strategies/manifest.json`, `registry.py`, untracked `S_*`, `scripts/first_wide_fill_watcher.py`) — do not stage/touch. Verify staged set.
- Do NOT push, restart, run any backtest, or touch the live DB. The script is INERT until invoked; tests mock the DB/manifest and never launch a real unit (use `--dry-run` paths). Do NOT actually run `systemd-run` in a test.
- The re-backtest flag is exactly `OPENCLAW_TRUE_MTM_MARKS=1`. Default `MemoryMax=4G`, `--watchdog-min` default 90. Log-dir default `/var/log/openclaw/rebacktest/`.
- Resume is authoritative via `run_at > start_ts` on a `primary_window=true` row — NOT log-grepping.
- Commit footer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Work from `/root/openclaw`.

---

### Task 1: `scripts/rebacktest_runner.py` + tests

**Files:**
- Create: `scripts/rebacktest_runner.py`
- Test: `tests/test_rebacktest_runner.py`

**Interfaces (pure helpers, unit-tested):**
- `build_worklist(primary_sids, manifest, *, include_deprecated=False, only=None, exclude=None) -> list[dict]` — each item `{'sid': str, 'mode': 'strategy-id'|'strategy-file'}`.
- `is_done(latest_run_at, start_ts) -> bool` — `latest_run_at` is the strategy's latest `primary_window=true` `run_at` (datetime or None); `start_ts` datetime.
- `build_systemd_cmd(item, *, memory_max_g, watchdog_sec, log_path, repo='/root/openclaw') -> list[str]`.
- `summarize(results) -> dict` — `results` is a list of `{'sid','status'}` with status in {'ok','fail','skip'}.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rebacktest_runner.py`:
```python
"""tests/test_rebacktest_runner.py — re-backtest harness pure helpers (Phase 1b)."""
from __future__ import annotations
import sys, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'scripts'))
import rebacktest_runner as rr  # noqa: E402

MANIFEST = {'strategies': {
    'S_live_a': {'state': 'live'}, 'S_cand_b': {'state': 'candidate'},
    'S_dep_c': {'state': 'deprecated'},
    # S_orphan_d intentionally absent from the manifest
}}
PRIMARY = {'S_live_a', 'S_cand_b', 'S_dep_c', 'S_orphan_d'}


class TestWorklist(unittest.TestCase):
    def test_default_excludes_deprecated_and_orphans(self):
        wl = rr.build_worklist(PRIMARY, MANIFEST)
        self.assertEqual({w['sid'] for w in wl}, {'S_live_a', 'S_cand_b'})
        self.assertTrue(all(w['mode'] == 'strategy-id' for w in wl))

    def test_include_deprecated_adds_them(self):
        wl = rr.build_worklist(PRIMARY, MANIFEST, include_deprecated=True)
        self.assertEqual({w['sid'] for w in wl}, {'S_live_a', 'S_cand_b', 'S_dep_c', 'S_orphan_d'})
        modes = {w['sid']: w['mode'] for w in wl}
        self.assertEqual(modes['S_orphan_d'], 'strategy-file')  # orphan -> file mode
        self.assertEqual(modes['S_dep_c'], 'strategy-id')

    def test_only_and_exclude(self):
        self.assertEqual({w['sid'] for w in rr.build_worklist(PRIMARY, MANIFEST, only=['S_live_a'])},
                         {'S_live_a'})
        self.assertEqual({w['sid'] for w in rr.build_worklist(PRIMARY, MANIFEST, exclude=['S_live_a'])},
                         {'S_cand_b'})


class TestIsDone(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    def test_fresh_run_is_done(self):
        self.assertTrue(rr.is_done(self.start + timedelta(hours=1), self.start))
    def test_stale_run_not_done(self):
        self.assertFalse(rr.is_done(self.start - timedelta(hours=1), self.start))
    def test_missing_run_not_done(self):
        self.assertFalse(rr.is_done(None, self.start))


class TestCmd(unittest.TestCase):
    def test_cmd_has_flag_limits_and_sid(self):
        cmd = rr.build_systemd_cmd({'sid': 'S_live_a', 'mode': 'strategy-id'},
                                   memory_max_g=4, watchdog_sec=5400, log_path='/tmp/x.log')
        j = ' '.join(cmd)
        self.assertIn('OPENCLAW_TRUE_MTM_MARKS=1', j)
        self.assertIn('MemoryMax=4G', j)
        self.assertIn('RuntimeMaxSec=5400', j)
        self.assertIn('--strategy-id', cmd); self.assertIn('S_live_a', cmd)
    def test_orphan_uses_strategy_file(self):
        cmd = rr.build_systemd_cmd({'sid': 'S_orphan_d', 'mode': 'strategy-file'},
                                   memory_max_g=4, watchdog_sec=5400, log_path='/tmp/x.log')
        self.assertIn('--strategy-file', cmd)
        self.assertTrue(any('S_orphan_d.py' in c for c in cmd))


class TestSummarize(unittest.TestCase):
    def test_tally(self):
        s = rr.summarize([{'sid': 'a', 'status': 'ok'}, {'sid': 'b', 'status': 'fail'},
                          {'sid': 'c', 'status': 'ok'}, {'sid': 'd', 'status': 'skip'}])
        self.assertEqual((s['ok'], s['fail'], s['skip']), (2, 1, 1))
        self.assertEqual(s['failed_sids'], ['b'])


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/openclaw && python3 tests/test_rebacktest_runner.py`
Expected: FAIL — `ModuleNotFoundError: rebacktest_runner` (script not created yet).

- [ ] **Step 3: Implement the pure helpers**

Create `scripts/rebacktest_runner.py` with the helpers:
```python
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
        '-p', 'Environment=OPENCLAW_TRUE_MTM_MARKS=1',
        '-p', f'Environment=PYTHONPATH={repo}/src',
        '-p', f'WorkingDirectory={repo}',
        '-p', f'MemoryMax={memory_max_g}G',
        '-p', f'RuntimeMaxSec={watchdog_sec}',
        '-p', 'Nice=19',
        '-p', f'StandardOutput=append:{log_path}',
        '-p', f'StandardError=append:{log_path}',
        'python3', '-m', 'backtest.unified_backtest', *target,
    ]


def summarize(results):
    ok = sum(1 for r in results if r['status'] == 'ok')
    fail = sum(1 for r in results if r['status'] == 'fail')
    skip = sum(1 for r in results if r['status'] == 'skip')
    return {'ok': ok, 'fail': fail, 'skip': skip,
            'failed_sids': [r['sid'] for r in results if r['status'] == 'fail']}
```

- [ ] **Step 4: Implement the DB helpers + driver `main`**

Add below the pure helpers:
```python
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
        return 'active' in out.split()
    except Exception:
        return False


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
    primary = _primary_sids(conn)
    manifest = json.loads(Path(f'{REPO}/src/strategies/manifest.json').read_text())
    worklist = build_worklist(primary, manifest, include_deprecated=a.include_deprecated,
                              only=only, exclude=exclude)

    # start_ts: reuse persisted (resume) or stamp now
    if state_path.exists():
        start_ts_str = json.loads(state_path.read_text())['start_ts']
    else:
        start_ts_str = datetime.now(timezone.utc).isoformat()
        state_path.write_text(json.dumps({'start_ts': start_ts_str}))
    start_ts = datetime.fromisoformat(start_ts_str)

    print(f'work-list: {len(worklist)} strategies | start_ts={start_ts_str} | '
          f'MemoryMax={a.memory_max_g}G watchdog={a.watchdog_min}min log_dir={log_dir}')
    results = []
    for w in worklist:
        sid = w['sid']
        if is_done(_latest_primary_run_at(conn, sid), start_ts):
            print(f'SKIP {sid} done'); results.append({'sid': sid, 'status': 'skip'}); continue
        cmd = build_systemd_cmd(w, memory_max_g=a.memory_max_g, watchdog_sec=watchdog_sec,
                                log_path=str(log_dir / f'{sid}.log'))
        if a.dry_run:
            print('DRY-RUN', ' '.join(cmd)); results.append({'sid': sid, 'status': 'skip'}); continue
        waited = 0
        while _mem_available_gb() < ram_floor and waited < 1800:
            time.sleep(30); waited += 30
        t0 = time.time()
        rc = subprocess.run(cmd).returncode
        conn.rollback()  # drop any aborted txn state before the verify query
        done = is_done(_latest_primary_run_at(conn, sid), start_ts)
        status = 'ok' if done else 'fail'
        print(f'{status.upper()} {sid} {int(time.time()-t0)}s rc={rc}')
        results.append({'sid': sid, 'status': status})
    s = summarize(results)
    print(f'DONE ok={s["ok"]} fail={s["fail"]} skip={s["skip"]}')
    if s['failed_sids']:
        print('FAILED:', ','.join(s['failed_sids']))
    conn.close()
    return 0 if not s['fail'] else 1


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /root/openclaw && python3 tests/test_rebacktest_runner.py`
Expected: `OK` (10 tests). Also `python3 -c "import ast; ast.parse(open('scripts/rebacktest_runner.py').read())"` → no output.

- [ ] **Step 6: Smoke the `--dry-run` path (no unit launched)**

Run (read-only, prints commands, launches nothing — but it DOES connect to the DB to enumerate; if `POSTGRES_URI` is unset it will exit cleanly). To avoid needing DB creds in this step, only assert the module imports + tests pass; do NOT run the driver against the live DB (that's Phase 1c/1d, operator-gated). Report that Step 6 is import + ast only.

- [ ] **Step 7: Commit (path-scoped)**

```bash
cd /root/openclaw
git add scripts/rebacktest_runner.py tests/test_rebacktest_runner.py
git status --porcelain   # MUST show ONLY those two paths
git commit -m "feat(ops): resumable memory-safe re-backtest harness (Phase 1b)

Per-strategy systemd-run MemoryMax units with OPENCLAW_TRUE_MTM_MARKS=1,
run_at-resume, RAM-floor gate, watchdog, competing-timer refusal, --dry-run.
Work-list from primary_window rows excl. deprecated by default. Inert until
invoked (Phase 1d). 10 tests.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:** work-list/deprecated/orphan/only/exclude → `build_worklist` (Step 3 + tests). Resume → `is_done` + `_latest_primary_run_at` + state file (Steps 3/4 + tests). Per-strategy memory-capped unit + flag → `build_systemd_cmd` (Step 3 + tests). RAM gate, competing-timer refusal, watchdog, OK/FAIL/SKIP tally, --dry-run → `main` (Step 4). Authoritative `run_at` outcome check → `main` verify. Testing → Step 1. ✓
**Placeholder scan:** none — all code concrete and runnable. ✓
**Type consistency:** `build_worklist` returns `[{'sid','mode'}]`; `build_systemd_cmd` consumes `item['mode']`; `is_done(datetime|None, datetime)`; `summarize` consumes `[{'sid','status'}]`. Consistent across helpers and `main`. ✓
