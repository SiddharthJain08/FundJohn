# Stream E — reliability (run lock, MemoryMax + OnFailure, heartbeat, import allowlist, live KPIs) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Five reliability repairs, none of which changes a signal, a size, or a Sharpe. (E1) The Python orchestrator and the JS daily-cycle graph stop using two different Redis keys and start sharing ONE owned, renewed, PID-aware run lock — a second concurrent run exits `rc=75` with a Discord post instead of `return 0` "success". (E2) Every daily-cycle child is spawned inside a `MemoryMax=` transient scope so an OOM kills the step, not johnbot, and 22 verified systemd units gain `OnFailure=openclaw-failure-notify@%n.service`. (E3) Every long-lived child writes a Redis heartbeat naming itself, a `proc_registry` system check and a doctor `co_tenant_memory` line make co-tenancy visible, and the Python stdout-idle wedge detector is ported to the JS step runner. (E4) LLM-written strategy files are AST-linted against a verified import allowlist BEFORE the first import. (E5) The portfolio page gains profit factor, expectancy, payoff ratio, median MAE, win/lose days and a 12-month return strip.

**Architecture:** Five small, independent libraries under `src/lib/` (three of them twinned Python/JS around one shared JSON constant file), wired into the existing call sites; one new lint module under `src/strategies/`; one new pure-JS KPI helper under `src/channels/api/`. No new services, no new threads, no new packages, no schema change. The systemd work is drop-ins only and is the single OPERATOR-RUN task.

**Tech Stack:** Python 3 (stdlib + `redis`, `psycopg2`), Node 18 (`ioredis`, `express`), pytest, `node --test`. No new dependencies.

**Spec:** `docs/specs/2026-09-12-quantdinger-adoptions-spec.md` §5 (E1–E5). Operator rulings in the spec preamble (R1–R4). Sequencing: §6 item 1 puts E1/E2 first (land on a Sat/Sun so Monday's cycle picks them up); E3–E5 are §6 item 4 (any order).

## Global Constraints

Spec §0, verbatim, one per line:

- Master parquets and canonical Postgres tables are append-only (repo CLAUDE.md). New tables/columns only; never DELETE, never rewrite history.
- Backtest side is AUTHORITATIVE (08-07 ruling). Any live/backtest disagreement is fixed on the live side unless the backtest is provably look-ahead — items 1 and 2 are exactly that case.
- Every new behaviour ships behind an env flag whose unset value is byte-identical to today's behaviour, unless the item is a pure bug fix that the operator has explicitly approved (items 3, 10, 11, 16, and the circuit-breaker regime change).
- 2-core / 8 GB / no swap: never load whole `prices.parquet` or `options_eod.parquet`; slice by date/ticker; no always-on threads; no new packages.
- Production = working tree on `main`; timer-spawned scripts pick up the tree on their next run. Work on a worktree branch; merge to main only when the whole stream is green; never leave main half-edited across a timer boundary.
- Tests on this box reach the REAL DB (`.env` loads at import) — stub gates in fixtures; never run the full suite while the fleet runs; never include `test_regime_stratified_backtest`.
- Every file:line cited below was grep-verified on 2026-09-11 against main `d9dbdf06`; re-verify before editing (lines drift).
- Log to `docs/archive/changelog.md` (newest first) per stream, not to CLAUDE.md.

Stream-E readings of those constraints:

- **Items 10 (E1) and 11 (E2) are operator-approved pure bug fixes** — they are named in the §0 flag exemption, so E1's `rc=75` and E2's MemoryMax cap ship default-ON with no flag. E2's cap value is tunable via `OPENCLAW_STEP_MEMORY_MAX` (`'0'` disables); E3/E4/E5 are additive and observation-only, so they need no flag either. Nothing in this stream changes a signal, a size or a Sharpe.
- **E2 changes a failure mode, deliberately.** Capping converts "the kernel's global OOM killer picks a victim, possibly johnbot" into "this step dies `rc=137` and the cycle aborts at that step". Only `signals` has a bounded retry (`pipeline_orchestrator.py:869-885`), so a capped `collect` (the heaviest step; 9000 s timeout) that OOMs is a NEW hard-abort path. Default `4500M` is the value already proven in production by `capped_spawn.js`.
- **No always-on threads.** E3 heartbeats are written from inside loops that already exist (the 30 s `proc.wait` poll in `run_step`; the pre-spawn point in the fleet driver). Never a `threading.Timer`, never a `setInterval`.

### Test rules (every task)

- Run ONLY the task's own test files plus the touching module's existing tests. **Never the whole suite** — a fleet backtest is running and pytest on this box loads `.env` and reaches the REAL Postgres/Redis.
- Python, from the worktree root:
  ```bash
  cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest -q tests/lib/test_run_lock.py
  ```
  (substitute the task's files; multiple paths space-separated).
- Node, from the worktree root:
  ```bash
  cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/lib/run_lock.test.js
  ```
- **Never `pytest` with no path. Never `npm test`. Never include `test_regime_stratified_backtest`.**
- **Redis is faked.** Every lock/heartbeat test defines a tiny in-memory stub class inside the test file (`FakeRedis`, modelled on `tests/execution/test_close_inflight_lock.py:19-31`) and injects it. No test may call `get_redis()`, `new Redis(...)`, or `redis.from_url`.
- **Never `systemctl` in a test** — not `start`, not `stop`, not `daemon-reload`, not `show`. Task 6 is the only task that touches systemd and it is OPERATOR-RUN.
- `src/lib/capped_spawn.py` and its JS twin must degrade to a plain spawn when `systemd-run` is unavailable; tests exercise BOTH branches by injecting the availability flag, never by probing the host.

## File Structure

| Path | Responsibility |
|---|---|
| `src/lib/run_lock_key.json` | NEW. The single source of truth for the run-lock key prefix + TTL slack, read verbatim by both twins. |
| `src/lib/run_lock.py` | NEW. Owned/renewed/takeover-if-pid-dead run lock (Python side). |
| `src/lib/run_lock.js` | NEW. Byte-compatible JS twin of the above. |
| `src/lib/capped_spawn.py` | NEW. Python twin of `capped_spawn.js`: wraps an argv in `systemd-run --scope -p MemoryMax=`. |
| `src/lib/proc_heartbeat.py` | NEW. Writes `proc:<host>:<pid>` Redis hash (argv, step, rss_mb, started_at, updated_at), TTL 180 s. |
| `src/lib/proc_heartbeat.js` | NEW. JS twin of the above. |
| `src/strategies/strategy_lint.py` | NEW. AST import allowlist + banned-call lint for LLM-written strategy files. |
| `src/channels/api/nav_kpis.js` | NEW. Pure functions: derive trade KPIs from one SQL row; monthly returns + win/lose days from the NAV OHLC store. |
| `src/execution/pipeline_orchestrator.py` | MODIFY. Uses the shared lock; renews per step; `rc=75` on held lock; spawns steps capped; writes heartbeats. |
| `src/agent/graphs/daily-cycle.js` | MODIFY. `_acquireRunLock` switches to the shared key + owned value + value-checked release. |
| `src/agent/graphs/daily_cycle_helpers.js` | MODIFY. `runSubprocess` gains the MemoryMax wrap, the stdout-idle watchdog and the heartbeat. |
| `src/engine/cron-schedule.js` | MODIFY. Reads the shared lock key; spawns the orchestrator capped. |
| `src/maintenance/doctor.py` | MODIFY. `check_orchestrator_lock` scans the real key; new `co_tenant_memory` check. |
| `src/system_checks/checks/agents.py` | MODIFY. New `proc_registry` check. |
| `src/strategies/validate_strategy.py` | MODIFY. Lints before the first `importlib.import_module`. |
| `src/agent/research/research-orchestrator.js` | MODIFY. Pre-flight lint before `_validateFn`; `reasonCode: 'import_violation'`. |
| `src/agent/prompts/subagents/strategycoder.md` | MODIFY. Documents the enforced allowlist. |
| `src/channels/api/server.js` | MODIFY. `/api/portfolio/summary` gains 6 KPIs + `monthly_returns`; the page gains a KPI row + 12-month strip. |
| `scripts/refresh_backtests_resumable.js` | MODIFY. One pre-spawn heartbeat per fleet child. |
| `docs/systemd/openclaw-*.service.d/onfailure.conf` | NEW ×22 (Task 6, OPERATOR-RUN). Repo snapshot of the installed drop-ins. |
| `docs/archive/changelog.md` | MODIFY. One Stream-E entry, newest first. |
| `tests/lib/test_run_lock.py`, `tests/lib/run_lock.test.js`, `tests/lib/test_capped_spawn.py`, `tests/lib/test_proc_heartbeat.py`, `tests/lib/proc_heartbeat.test.js` | NEW. Library unit tests (fake Redis, injected availability). |
| `tests/execution/test_run_lock_wiring.py` | NEW. Orchestrator lock/renew/rc=75 wiring. |
| `tests/test_daily_cycle_run_lock.test.js`, `tests/test_daily_cycle_capped_spawn.test.js`, `tests/test_daily_cycle_idle_watchdog.test.js` | NEW. JS wiring tests. |
| `tests/strategies/test_strategy_lint.py` | NEW. Lint unit tests + "lint the whole fleet = 0 violations". |
| `tests/channels/test_nav_kpis.test.js` | NEW. KPI derivation + NAV monthly strip + SQL-guard assertions. |

---

### Task 1 — E1a: shared key constant + `src/lib/run_lock.py`

**Files:**
- Create: `src/lib/run_lock_key.json`
- Create: `src/lib/run_lock.py`
- Create: `tests/lib/test_run_lock.py`

**Interfaces:**
- Consumes: a Redis-like object supporting `set(key, value, nx=?, ex=?)`, `get(key)`, `delete(key)`, `expire(key, ttl)`.
- Produces:
  - `src/lib/run_lock_key.json` → `{"prefix": "pipeline:run_lock", "ttl_slack_seconds": 120, "min_ttl_seconds": 300}`
  - `KEY_PREFIX: str`, `TTL_SLACK_SECONDS: int`, `MIN_TTL_SECONDS: int`, `LOCK_BUSY_RC: int = 75`
  - `lock_key(run_date) -> str`
  - `make_value(pid=None, host=None, started_at=None) -> str` (`'host:pid:start_iso'`)
  - `parse_value(raw) -> tuple[str, int, str] | None`
  - `ttl_for(step_timeout_s) -> int`
  - `pid_alive(pid) -> bool`
  - `acquire(r, run_date, ttl_s, *, value=None, host=None, log=None) -> tuple[bool, str]`
  - `renew(r, run_date, value, ttl_s, *, log=None) -> bool`
  - `release(r, run_date, value, *, log=None) -> bool`

- [ ] **Step 1: Write the failing test** — create `tests/lib/test_run_lock.py`:

```python
"""tests/lib/test_run_lock.py — one owned, renewed run lock (QD spec §5 E1).

No real Redis: FakeRedis below implements only the four calls run_lock uses.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from lib import run_lock  # noqa: E402


class FakeRedis:
    """Minimal in-memory stub: set(nx/ex), get, delete, expire."""

    def __init__(self, store=None):
        self.store = dict(store or {})
        self.ttls = {}
        self.calls = []

    def set(self, k, v, nx=False, ex=None):
        self.calls.append(('set', k, v, nx, ex))
        if nx and k in self.store:
            return None
        self.store[k] = v
        if ex is not None:
            self.ttls[k] = ex
        return True

    def get(self, k):
        return self.store.get(k)

    def delete(self, k):
        self.calls.append(('delete', k))
        self.store.pop(k, None)
        self.ttls.pop(k, None)

    def expire(self, k, ttl):
        self.calls.append(('expire', k, ttl))
        if k not in self.store:
            return False
        self.ttls[k] = ttl
        return True


DATE = '2026-09-14'
KEY = f'pipeline:run_lock:{DATE}'


class TestKeyAndValue(unittest.TestCase):
    def test_key_prefix_comes_from_the_shared_json_file(self):
        cfg = json.loads((ROOT / 'src' / 'lib' / 'run_lock_key.json').read_text())
        self.assertEqual(cfg['prefix'], 'pipeline:run_lock')
        self.assertEqual(run_lock.KEY_PREFIX, cfg['prefix'])
        self.assertEqual(run_lock.lock_key(DATE), KEY)

    def test_value_is_host_pid_start_iso_and_round_trips(self):
        v = run_lock.make_value(pid=4242, host='vps1', started_at='2026-09-14T10:00:00+00:00')
        self.assertEqual(v, 'vps1:4242:2026-09-14T10:00:00+00:00')
        self.assertEqual(run_lock.parse_value(v), ('vps1', 4242, '2026-09-14T10:00:00+00:00'))

    def test_parse_value_rejects_garbage(self):
        self.assertIsNone(run_lock.parse_value(None))
        self.assertIsNone(run_lock.parse_value(''))
        self.assertIsNone(run_lock.parse_value('1'))
        self.assertIsNone(run_lock.parse_value('host:notapid:2026-09-14T10:00:00+00:00'))

    def test_ttl_is_step_timeout_plus_slack_with_a_floor(self):
        self.assertEqual(run_lock.ttl_for(9000), 9120)
        self.assertEqual(run_lock.ttl_for(300), 420)
        self.assertEqual(run_lock.ttl_for(1), run_lock.MIN_TTL_SECONDS)

    def test_lock_busy_rc_is_75(self):
        self.assertEqual(run_lock.LOCK_BUSY_RC, 75)


class TestAcquire(unittest.TestCase):
    def test_acquires_a_free_lock_and_stores_the_owned_value(self):
        r = FakeRedis()
        ok, value = run_lock.acquire(r, DATE, 420, value='vps1:111:T0')
        self.assertTrue(ok)
        self.assertEqual(value, 'vps1:111:T0')
        self.assertEqual(r.store[KEY], 'vps1:111:T0')
        self.assertEqual(r.ttls[KEY], 420)

    def test_refuses_when_held_by_a_live_pid_and_returns_the_holder(self):
        held = f'vps1:{os.getpid()}:T0'
        r = FakeRedis({KEY: held})
        ok, holder = run_lock.acquire(r, DATE, 420, value='vps1:999:T1', host='vps1')
        self.assertFalse(ok)
        self.assertEqual(holder, held)
        self.assertEqual(r.store[KEY], held)

    def test_takes_over_when_the_holder_pid_is_dead_on_this_host(self):
        logs = []
        r = FakeRedis({KEY: 'vps1:999999:T0'})
        ok, value = run_lock.acquire(r, DATE, 420, value='vps1:111:T1',
                                     host='vps1', log=logs.append)
        self.assertTrue(ok)
        self.assertEqual(r.store[KEY], 'vps1:111:T1')
        self.assertTrue(any('stale lock from pid 999999' in m for m in logs), logs)

    def test_never_takes_over_a_lock_held_by_another_host(self):
        r = FakeRedis({KEY: 'otherbox:999999:T0'})
        ok, holder = run_lock.acquire(r, DATE, 420, value='vps1:111:T1', host='vps1')
        self.assertFalse(ok)
        self.assertEqual(holder, 'otherbox:999999:T0')

    def test_refuses_on_an_unparseable_holder_rather_than_stealing(self):
        r = FakeRedis({KEY: '1'})
        ok, holder = run_lock.acquire(r, DATE, 420, value='vps1:111:T1', host='vps1')
        self.assertFalse(ok)
        self.assertEqual(holder, '1')


class TestRenewAndRelease(unittest.TestCase):
    def test_renew_extends_only_our_own_lock(self):
        r = FakeRedis({KEY: 'vps1:111:T0'})
        self.assertTrue(run_lock.renew(r, DATE, 'vps1:111:T0', 9120))
        self.assertEqual(r.ttls[KEY], 9120)

    def test_renew_refuses_when_another_owner_took_over(self):
        logs = []
        r = FakeRedis({KEY: 'vps1:222:T1'})
        self.assertFalse(run_lock.renew(r, DATE, 'vps1:111:T0', 9120, log=logs.append))
        self.assertEqual(r.ttls, {})
        self.assertTrue(any('held by vps1:222:T1' in m for m in logs), logs)

    def test_renew_re_takes_a_lock_that_expired_under_us(self):
        r = FakeRedis()
        self.assertTrue(run_lock.renew(r, DATE, 'vps1:111:T0', 9120))
        self.assertEqual(r.store[KEY], 'vps1:111:T0')

    def test_release_deletes_only_our_own_lock(self):
        r = FakeRedis({KEY: 'vps1:111:T0'})
        self.assertTrue(run_lock.release(r, DATE, 'vps1:111:T0'))
        self.assertNotIn(KEY, r.store)

    def test_release_never_deletes_a_lock_someone_else_now_owns(self):
        logs = []
        r = FakeRedis({KEY: 'vps1:222:T1'})
        self.assertFalse(run_lock.release(r, DATE, 'vps1:111:T0', log=logs.append))
        self.assertEqual(r.store[KEY], 'vps1:222:T1')
        self.assertNotIn(('delete', KEY), r.calls)

    def test_release_of_an_absent_lock_is_a_no_op(self):
        r = FakeRedis()
        self.assertFalse(run_lock.release(r, DATE, 'vps1:111:T0'))

    def test_release_with_no_value_is_a_no_op(self):
        r = FakeRedis({KEY: 'vps1:111:T0'})
        self.assertFalse(run_lock.release(r, DATE, None))
        self.assertIn(KEY, r.store)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run it — expect failure**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest -q tests/lib/test_run_lock.py
```

Expected: collection error — `ModuleNotFoundError: No module named 'lib.run_lock'`.

- [ ] **Step 3: Write the shared constant** — create `src/lib/run_lock_key.json`:

```json
{
  "prefix": "pipeline:run_lock",
  "ttl_slack_seconds": 120,
  "min_ttl_seconds": 300,
  "_comment": "Single source of truth for the daily-cycle run lock. Read verbatim by src/lib/run_lock.py and src/lib/run_lock.js. Changing the prefix changes BOTH twins at once; that is the point (QD spec 2026-09-12 section 5, E1)."
}
```

- [ ] **Step 4: Write the implementation** — create `src/lib/run_lock.py`:

```python
"""run_lock.py — one owned, renewed daily-cycle run lock (QD spec §5 E1).

Before this module there were TWO locks and neither was owned:
  * Python  `pipeline:running:<date>` = '1'                      (pipeline_orchestrator.py:38)
  * JS      `engine:run_lock:<date>`  = 'daily-cycle:<pid>:<ms>'  (daily-cycle.js:140)
so the orchestrator and the LangGraph cycle could run concurrently, and the
Python side answered a held lock with `return 0` — a silently "successful"
no-op cycle.

This module gives both twins ONE key, `pipeline:run_lock:<date>`, whose value
is `host:pid:start_iso`. That value makes three things possible:

  * takeover — a lock whose holder PID is dead ON THIS HOST is stale and may
    be taken (pattern: src/lib/manifest_lock.js `_isProcessAlive`). A holder
    on another host is never touched: we cannot judge its liveness.
  * renewal — the step runner extends the TTL before each step to that step's
    own timeout + 120 s, so a long collect can no longer outlive its lock.
  * value-checked release — a process whose lock was taken over must NOT
    delete the new owner's key on its way out. Every release compares first.

The compare-then-act pairs are not atomic (no Lua, to keep the fake-Redis test
stub honest and the dependency surface at zero). They do not need to be: the
loser of any race is a process that has already lost the lock, and the worst
outcome is one extra `[lock]` log line.
"""
from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

_CFG = json.loads((Path(__file__).resolve().parent / 'run_lock_key.json').read_text())

KEY_PREFIX        = str(_CFG['prefix'])
TTL_SLACK_SECONDS = int(_CFG['ttl_slack_seconds'])
MIN_TTL_SECONDS   = int(_CFG['min_ttl_seconds'])

# EX_TEMPFAIL. "Another run owns today" is a temporary failure, not success:
# systemd surfaces it, the failure notifier posts it, cron stops pretending.
LOCK_BUSY_RC = 75


def lock_key(run_date) -> str:
    return f'{KEY_PREFIX}:{run_date}'


def _text(raw):
    if isinstance(raw, bytes):
        return raw.decode('utf-8', 'replace')
    return raw


def make_value(pid=None, host=None, started_at=None) -> str:
    host = host or socket.gethostname()
    pid = os.getpid() if pid is None else int(pid)
    started_at = started_at or datetime.now(timezone.utc).isoformat()
    return f'{host}:{pid}:{started_at}'


def parse_value(raw):
    """`'host:pid:start_iso'` -> `(host, pid, start_iso)`, else None.

    Split on the FIRST TWO colons only — the ISO timestamp contains colons.
    """
    raw = _text(raw)
    if not raw:
        return None
    parts = raw.split(':', 2)
    if len(parts) != 3:
        return None
    host, pid_s, started = parts
    try:
        pid = int(pid_s)
    except (TypeError, ValueError):
        return None
    if not host or not started:
        return None
    return (host, pid, started)


def ttl_for(step_timeout_s) -> int:
    """Lock TTL for a step: its own timeout + slack, never below the floor."""
    try:
        base = int(step_timeout_s)
    except (TypeError, ValueError):
        base = 0
    return max(MIN_TTL_SECONDS, base + TTL_SLACK_SECONDS)


def pid_alive(pid) -> bool:
    """signal 0 = existence + permission probe; EPERM means alive but not ours."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def acquire(r, run_date, ttl_s, *, value=None, host=None, log=None):
    """Try to own `run_date`.

    Returns `(True, our_value)` on acquire or takeover, else
    `(False, holder_raw)` where holder_raw is the raw value still in Redis
    (possibly None if it vanished between the SET NX and the GET).
    """
    key = lock_key(run_date)
    me_host = host or socket.gethostname()
    value = value or make_value(host=me_host)
    ttl = int(ttl_s)

    if r.set(key, value, nx=True, ex=ttl):
        return True, value

    holder_raw = _text(r.get(key))
    parsed = parse_value(holder_raw)
    if parsed is None:
        return False, holder_raw

    h_host, h_pid, h_started = parsed
    if h_host == me_host and not pid_alive(h_pid):
        if log:
            log(f'[lock] stale lock from pid {h_pid} (host {h_host}, started {h_started}) '
                f'— taking over {key}')
        r.set(key, value, ex=ttl)
        return True, value

    return False, holder_raw


def renew(r, run_date, value, ttl_s, *, log=None) -> bool:
    """Extend OUR lock's TTL. False (and no write) if someone else owns it."""
    if not value:
        return False
    key = lock_key(run_date)
    ttl = int(ttl_s)
    cur = _text(r.get(key))
    if cur is None:
        # Expired under us (a step outran its own TTL). Re-take it: we are
        # demonstrably still running, and nobody else claimed it.
        r.set(key, value, ex=ttl)
        if log:
            log(f'[lock] {key} had expired — re-taken by {value} (ttl={ttl}s)')
        return True
    if cur != value:
        if log:
            log(f'[lock] renew skipped — {key} held by {cur}, not us ({value})')
        return False
    r.expire(key, ttl)
    return True


def release(r, run_date, value, *, log=None) -> bool:
    """Delete OUR lock. Never deletes a lock another owner took over."""
    if not value:
        return False
    key = lock_key(run_date)
    cur = _text(r.get(key))
    if cur is None:
        return False
    if cur != value:
        if log:
            log(f'[lock] release skipped — {key} held by {cur}, not us ({value})')
        return False
    r.delete(key)
    return True
```

- [ ] **Step 5: Run it — expect PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest -q tests/lib/test_run_lock.py
```

Expected: `17 passed`.

- [ ] **Step 6: Commit**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/lib/run_lock_key.json src/lib/run_lock.py tests/lib/test_run_lock.py
git commit -m "feat(lib): owned/renewed run lock + shared key constant (QD E1)

One key pipeline:run_lock:<date> for both the Python orchestrator and the JS
daily-cycle graph, value host:pid:start_iso. Takeover only when the holder PID
is dead ON THIS HOST; renew and release are value-checked so a taken-over
process can never delete the new owner's lock.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

<!-- END TASK 1 -->

---

### Task 2 — E1b: wire the shared lock into `pipeline_orchestrator.py`

The orchestrator has **five** `release_lock` sites (`:826, :840, :901, :939, :970` — the spec cites only `:826`), a `return 0` "already running" path at `:779-782`, a `--force-resume` branch at `:785` that writes the literal `'1'`, and a comment at `:775-778` claiming the key is `pipeline:lock:{date}` (it is `pipeline:running:{date}`). All of it changes here. `run_step` gains an optional `renew` callback so the bounded `signals` retry at `:881` renews too.

**Files:**
- Modify: `src/execution/pipeline_orchestrator.py` (:38-39, :155-162, :580-660, :775-786, and the five release sites)
- Create: `tests/execution/test_run_lock_wiring.py`

**Interfaces:**
- Consumes: `lib.run_lock.{lock_key, make_value, ttl_for, acquire, renew, release, LOCK_BUSY_RC}` (Task 1); existing `_resolve_script(script, run_date) -> (argv, timeout)`; existing `notify(msg, channel='pipeline-feed')`.
- Produces (module-level, in `pipeline_orchestrator`):
  - `LOCK_VALUE: str | None` — this process's owned value, set by `acquire_lock`
  - `acquire_lock(r, run_date, ttl_s=None) -> bool` (signature-compatible with today's 2-arg call)
  - `release_lock(r, run_date) -> bool` (unchanged signature, now value-checked)
  - `renew_lock(r, run_date, ttl_s) -> bool`
  - `run_step(script, run_date, env, renew=None) -> tuple[bool, int]` — `renew` is `Callable[[int], None]` called once with the resolved step timeout, before the spawn
- Verified: no test file calls `run_step` / `acquire_lock` / `release_lock` directly (`grep -n 'run_step\|acquire_lock\|release_lock' tests/pipeline/test_run_sentiment_step.py tests/pipeline/test_sp2_smoke.py tests/execution/test_resolve_script_twin_parity.py tests/execution/test_engine_run_date_arg.py` → no hits), so the added keyword-only default breaks nothing.

- [ ] **Step 1: Write the failing test** — create `tests/execution/test_run_lock_wiring.py`:

```python
"""tests/execution/test_run_lock_wiring.py — orchestrator uses the shared,
owned, renewed run lock and exits rc=75 when another run owns today.

Fake Redis only; no subprocess is ever spawned (run_step is stubbed).
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import pipeline_orchestrator as po  # noqa: E402
from lib import run_lock  # noqa: E402


class FakeRedis:
    def __init__(self, store=None):
        self.store = dict(store or {})
        self.ttls = {}

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.store:
            return None
        self.store[k] = v
        if ex is not None:
            self.ttls[k] = ex
        return True

    def get(self, k):
        return self.store.get(k)

    def delete(self, k):
        self.store.pop(k, None)
        self.ttls.pop(k, None)

    def expire(self, k, ttl):
        if k not in self.store:
            return False
        self.ttls[k] = ttl
        return True

    def setex(self, k, ttl, v):
        self.store[k] = v
        self.ttls[k] = ttl

    def publish(self, *a, **kw):
        return 0


DATE = '2026-09-14'
KEY = f'pipeline:run_lock:{DATE}'


class TestLockHelpers(unittest.TestCase):
    def setUp(self):
        po.LOCK_VALUE = None

    def tearDown(self):
        po.LOCK_VALUE = None

    def test_acquire_writes_the_shared_key_with_an_owned_value(self):
        r = FakeRedis()
        self.assertTrue(po.acquire_lock(r, DATE))
        self.assertIn(KEY, r.store)
        self.assertIsNotNone(run_lock.parse_value(r.store[KEY]))
        self.assertEqual(run_lock.parse_value(r.store[KEY])[1], os.getpid())
        self.assertEqual(po.LOCK_VALUE, r.store[KEY])

    def test_acquire_refuses_when_a_live_holder_owns_the_key(self):
        r = FakeRedis({KEY: f'{po._HOST}:{os.getpid()}:T0'})
        self.assertFalse(po.acquire_lock(r, DATE))

    def test_renew_extends_our_ttl_to_step_timeout_plus_120(self):
        r = FakeRedis()
        po.acquire_lock(r, DATE)
        self.assertTrue(po.renew_lock(r, DATE, run_lock.ttl_for(9000)))
        self.assertEqual(r.ttls[KEY], 9120)

    def test_release_is_value_checked(self):
        r = FakeRedis()
        po.acquire_lock(r, DATE)
        r.store[KEY] = 'otherbox:222:T9'          # someone took over
        self.assertFalse(po.release_lock(r, DATE))
        self.assertEqual(r.store[KEY], 'otherbox:222:T9')

    def test_release_deletes_our_own_lock(self):
        r = FakeRedis()
        po.acquire_lock(r, DATE)
        self.assertTrue(po.release_lock(r, DATE))
        self.assertNotIn(KEY, r.store)


class TestRunStepRenews(unittest.TestCase):
    def test_run_step_calls_renew_with_the_resolved_step_timeout(self):
        seen = []
        # `false` exits 1 immediately; we only care that renew fired first.
        orig = po._resolve_script
        po._resolve_script = lambda script, run_date: (['false'], 777)
        try:
            ok, rc = po.run_step('engine', DATE, dict(os.environ),
                                 renew=lambda t: seen.append(t))
        finally:
            po._resolve_script = orig
        self.assertFalse(ok)
        self.assertEqual(seen, [777])

    def test_run_step_without_renew_is_unchanged(self):
        orig = po._resolve_script
        po._resolve_script = lambda script, run_date: (['true'], 5)
        try:
            ok, rc = po.run_step('engine', DATE, dict(os.environ))
        finally:
            po._resolve_script = orig
        self.assertTrue(ok)
        self.assertEqual(rc, 0)


class TestMainBusyLock(unittest.TestCase):
    def test_main_returns_75_and_posts_when_another_run_owns_today(self):
        posts = []
        r = FakeRedis({KEY: f'{po._HOST}:{os.getpid()}:T0'})
        orig = (po.get_redis, po.notify, po.run_step, po.is_completed_today, po.read_checkpoint)
        po.get_redis = lambda: r
        po.notify = lambda msg, channel='pipeline-feed': posts.append((channel, msg))
        po.run_step = lambda *a, **kw: (_ for _ in ()).throw(AssertionError('no step may run'))
        po.is_completed_today = lambda _r, _d: False
        po.read_checkpoint = lambda _r: None
        os.environ.setdefault('POSTGRES_URI', 'postgresql://stub/stub')
        try:
            rc = po.main(['--date', DATE, '--steps', 'report'])
        finally:
            (po.get_redis, po.notify, po.run_step,
             po.is_completed_today, po.read_checkpoint) = orig
        self.assertEqual(rc, 75)
        self.assertEqual(rc, run_lock.LOCK_BUSY_RC)
        self.assertTrue(any('[lock] held by' in m for _c, m in posts), posts)
        self.assertEqual(r.store[KEY], f'{po._HOST}:{os.getpid()}:T0')


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run it — expect failure**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest -q tests/execution/test_run_lock_wiring.py
```

Expected: `AttributeError: module 'execution.pipeline_orchestrator' has no attribute 'LOCK_VALUE'` (and `_HOST`, `renew_lock`).

- [ ] **Step 3: Replace the lock constants and helpers.** In `src/execution/pipeline_orchestrator.py`, replace lines 38-45 (`LOCK_KEY = 'pipeline:running'` through the end of the `LOCK_TTL` comment block) with:

```python
# ── Run lock (QD spec §5 E1, 2026-09-12) ─────────────────────────────────────
# ONE key shared with the JS daily-cycle graph: src/lib/run_lock_key.json.
# The old split — Python `pipeline:running:<date>` = '1' and JS
# `engine:run_lock:<date>` — let both runners hold "the" lock at once. The
# value is now `host:pid:start_iso`, so a dead holder can be detected and
# taken over, and the TTL is the CURRENT step's timeout + 120 s (renewed by
# run_step) rather than one flat 7200 s guess for the whole cycle.
from lib import run_lock as _run_lock          # noqa: E402  (after sys.path setup)

LOCK_VALUE: str | None = None                  # this process's owned value
_HOST = _socket.gethostname()
```

Add `import socket as _socket` to the stdlib import line at `:28` (`import os, sys, json, subprocess, time, requests` → `import os, sys, json, socket as _socket, subprocess, time, requests`). The `from lib import run_lock` import must sit AFTER the `sys.path.insert(0, str(ROOT))` at `:33` — put the whole block below it, replacing the constants where they are (the constants block already follows the path setup at `:37`).

- [ ] **Step 4: Replace `acquire_lock` / `release_lock` and add `renew_lock`.** Replace `:155-162` with:

```python
def acquire_lock(r, run_date, ttl_s=None):
    """Own the day, or return False. Sets module-level LOCK_VALUE on success.

    ttl_s defaults to the floor; run_step renews to the step's own timeout
    + 120 s before each step, so the initial value only has to cover the
    gap between acquire and the first step.
    """
    global LOCK_VALUE
    value = _run_lock.make_value(host=_HOST)
    ok, holder = _run_lock.acquire(
        r, run_date, ttl_s or _run_lock.MIN_TTL_SECONDS,
        value=value, host=_HOST, log=log)
    if ok:
        LOCK_VALUE = value
        return True
    LOCK_VALUE = None
    log(f'[lock] held by {holder} for {run_date}')
    return False


def renew_lock(r, run_date, ttl_s):
    """Extend our lock before a step. Never touches someone else's lock."""
    if not LOCK_VALUE:
        return False
    return _run_lock.renew(r, run_date, LOCK_VALUE, ttl_s, log=log)


def release_lock(r, run_date):
    """Value-checked release. A process whose lock was taken over MUST NOT
    delete the new owner's key — every one of the five release sites in
    main() goes through here, so the check applies to all of them."""
    global LOCK_VALUE
    if not LOCK_VALUE:
        return False
    released = _run_lock.release(r, run_date, LOCK_VALUE, log=log)
    LOCK_VALUE = None
    return released
```

- [ ] **Step 5: Give `run_step` the renew hook.** At `:580`, change the signature and add the renew call immediately after `_resolve_script`:

```python
def run_step(script, run_date, env, renew=None):
```

and, directly after `cmd, timeout = _resolve_script(script, run_date)` (currently `:592`), insert:

```python
    # QD E1: renew the run lock to THIS step's timeout + 120 s before spawning.
    # Done inside run_step (not in main's loop) so the bounded `signals` retry
    # — which calls run_step a second time — renews too.
    if renew is not None:
        try:
            renew(timeout)
        except Exception as e:      # a renew failure must never kill a step
            log(f'[lock] renew before {script} failed: {e}')
```

- [ ] **Step 6: Replace the "already running" path.** Replace `:775-786` (the comment block, the `if not args.force_resume:` branch and its `else`) with:

```python
    # ── Prevent concurrent runs ───────────────────────────────────────────────
    # ONE lock, shared with the JS daily-cycle graph: `pipeline:run_lock:<date>`
    # (src/lib/run_lock_key.json). A redeploy MUST NOT run concurrently with the
    # daily 10 AM cycle. Before QD E1 this path answered a held lock with
    # `return 0` — cron and systemd both recorded a successful cycle that never
    # ran. It now exits rc=75 (EX_TEMPFAIL) and posts, so the failure notifier
    # and #pipeline-feed both see it.
    lock_ttl = _run_lock.ttl_for(max(t for _k, t in
                                     (( k, _resolve_script(s, run_date)[1])
                                      for k, s in effective_steps)))
    if not args.force_resume:
        if not acquire_lock(r, run_date, lock_ttl):
            holder = r.get(_run_lock.lock_key(run_date))
            if isinstance(holder, bytes):
                holder = holder.decode('utf-8', 'replace')
            msg = (f'{reason_tag}🔒 **Pipeline lock held — run refused** | {run_date}\n'
                   f'`[lock] held by {holder}`\n'
                   f'Exit rc={_run_lock.LOCK_BUSY_RC}. Use `--force-resume` to override.')
            log(f'[lock] held by {holder} for {run_date} — exiting rc={_run_lock.LOCK_BUSY_RC}')
            notify(msg, channel='pipeline-feed')
            return _run_lock.LOCK_BUSY_RC
    else:
        # Operator override. Loud, because it deliberately runs beside whatever
        # already holds the lock — the one path that can still produce two
        # concurrent cycles.
        global LOCK_VALUE
        prior = r.get(_run_lock.lock_key(run_date))
        if isinstance(prior, bytes):
            prior = prior.decode('utf-8', 'replace')
        LOCK_VALUE = _run_lock.make_value(host=_HOST)
        r.set(_run_lock.lock_key(run_date), LOCK_VALUE, ex=lock_ttl)
        log(f'[lock] FORCE-RESUME — overriding lock (prior holder: {prior}) '
            f'with {LOCK_VALUE}; a concurrent run is possible')
        notify(f'{reason_tag}⚠️ **--force-resume: run lock overridden** | {run_date}\n'
               f'Prior holder: `{prior}` → now `{LOCK_VALUE}`', channel='pipeline-feed')
```

`global LOCK_VALUE` must be declared once at the top of `main()` instead of mid-body if Python objects; put `global LOCK_VALUE` as the first statement of `main()` and drop it from the `else` branch.

- [ ] **Step 7: Pass the renew hook at both `run_step` call sites.** At `:861` change `ok, rc = run_step(script, run_date, step_env)` to:

```python
            ok, rc = run_step(script, run_date, step_env,
                              renew=lambda t: renew_lock(r, run_date, _run_lock.ttl_for(t)))
```

and identically at the bounded-retry call currently at `:881`.

- [ ] **Step 8: Fix the four other release sites.** `:826, :840, :901, :939` already call `release_lock(r, run_date)`; they now inherit the value check with no edit. Verify with:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && grep -n 'release_lock\|acquire_lock\|renew_lock\|LOCK_VALUE\|pipeline:running' src/execution/pipeline_orchestrator.py
```

Expected: no `pipeline:running` anywhere; five `release_lock(r, run_date)` call sites; one `acquire_lock`; two `renew_lock` closures.

- [ ] **Step 9: Run the task's tests plus the touching module's tests — expect PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest -q tests/execution/test_run_lock_wiring.py tests/lib/test_run_lock.py tests/execution/test_resolve_script_twin_parity.py tests/pipeline/test_run_sentiment_step.py
```

Expected: all pass, no new failures.

- [ ] **Step 10: Commit**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/execution/pipeline_orchestrator.py tests/execution/test_run_lock_wiring.py
git commit -m "fix(cycle): orchestrator takes the shared owned run lock; rc=75 when held (QD E1)

Replaces pipeline:running:<date>='1' with pipeline:run_lock:<date>=host:pid:iso.
run_step renews to the step's own timeout + 120s (so the bounded signals retry
renews too); all five release sites are value-checked; the 'already running'
path no longer returns 0 — it exits rc=75 and posts. --force-resume still
overrides, now loudly.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

<!-- END TASK 2 -->

---

### Task 3 — E1c: `src/lib/run_lock.js` twin + wire `daily-cycle.js` and `cron-schedule.js`

There are **four** readers of "the run lock", not two: the Python orchestrator (Task 2), `daily-cycle.js:138-155`, `cron-schedule.js:210` (`pipeline:running:${runDate}` — the budget-resume check), and `doctor.py:830` (`scan_iter('pipeline:lock:*')`, a pattern matching *neither* live key, so that check has been reporting "no locks held" unconditionally). This task does the two JS ones; the doctor fix lands in Task 7.

**Files:**
- Create: `src/lib/run_lock.js`
- Create: `tests/lib/run_lock.test.js`
- Modify: `src/agent/graphs/daily-cycle.js` (:137-155)
- Modify: `src/engine/cron-schedule.js` (:209-211)
- Create: `tests/test_daily_cycle_run_lock.test.js`

**Interfaces:**
- Consumes: same JSON constant file as Task 1 (`require('./run_lock_key.json')`); an ioredis-like object with `set(k,v,'NX','EX',ttl)`, `set(k,v,'EX',ttl)`, `get(k)`, `del(k)`, `expire(k,ttl)`.
- Produces (`module.exports`): `KEY_PREFIX`, `TTL_SLACK_SECONDS`, `MIN_TTL_SECONDS`, `LOCK_BUSY_RC`, `lockKey(runDate)`, `makeValue({pid, host, startedAt})`, `parseValue(raw)`, `ttlFor(stepTimeoutSec)`, `pidAlive(pid)`, `acquire(r, runDate, ttlSec, opts)` → `{ok, value, holder}`, `renew(r, runDate, value, ttlSec, opts)` → `boolean`, `release(r, runDate, value, opts)` → `boolean`.

- [ ] **Step 1: Write the failing test** — create `tests/lib/run_lock.test.js`:

```javascript
'use strict';

const { test }  = require('node:test');
const assert    = require('node:assert/strict');
const path      = require('node:path');
const fs        = require('node:fs');

const ROOT     = path.resolve(__dirname, '..', '..');
const runLock  = require(path.join(ROOT, 'src/lib/run_lock.js'));

const DATE = '2026-09-14';
const KEY  = `pipeline:run_lock:${DATE}`;

class FakeRedis {
  constructor(store = {}) { this.store = { ...store }; this.ttls = {}; this.calls = []; }
  async set(k, v, ...flags) {
    this.calls.push(['set', k, v, ...flags]);
    const nx = flags.includes('NX');
    if (nx && Object.prototype.hasOwnProperty.call(this.store, k)) return null;
    const exAt = flags.indexOf('EX');
    if (exAt >= 0) this.ttls[k] = flags[exAt + 1];
    this.store[k] = v;
    return 'OK';
  }
  async get(k) { return Object.prototype.hasOwnProperty.call(this.store, k) ? this.store[k] : null; }
  async del(k) { this.calls.push(['del', k]); delete this.store[k]; delete this.ttls[k]; return 1; }
  async expire(k, ttl) {
    this.calls.push(['expire', k, ttl]);
    if (!Object.prototype.hasOwnProperty.call(this.store, k)) return 0;
    this.ttls[k] = ttl; return 1;
  }
}

test('both twins read the key prefix from the same JSON file', () => {
  const cfg = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/lib/run_lock_key.json'), 'utf8'));
  assert.equal(cfg.prefix, 'pipeline:run_lock');
  assert.equal(runLock.KEY_PREFIX, cfg.prefix);
  assert.equal(runLock.lockKey(DATE), KEY);
  assert.equal(runLock.TTL_SLACK_SECONDS, cfg.ttl_slack_seconds);
  assert.equal(runLock.LOCK_BUSY_RC, 75);
});

test('value format matches the Python twin exactly', () => {
  const v = runLock.makeValue({ pid: 4242, host: 'vps1', startedAt: '2026-09-14T10:00:00+00:00' });
  assert.equal(v, 'vps1:4242:2026-09-14T10:00:00+00:00');
  assert.deepEqual(runLock.parseValue(v), { host: 'vps1', pid: 4242, startedAt: '2026-09-14T10:00:00+00:00' });
  assert.equal(runLock.parseValue('1'), null);
  assert.equal(runLock.parseValue(''), null);
});

test('ttlFor = timeout + 120 with a 300s floor', () => {
  assert.equal(runLock.ttlFor(9000), 9120);
  assert.equal(runLock.ttlFor(300), 420);
  assert.equal(runLock.ttlFor(1), runLock.MIN_TTL_SECONDS);
});

test('acquire takes a free lock and records the owned value', async () => {
  const r = new FakeRedis();
  const out = await runLock.acquire(r, DATE, 420, { value: 'vps1:111:T0' });
  assert.equal(out.ok, true);
  assert.equal(r.store[KEY], 'vps1:111:T0');
  assert.equal(r.ttls[KEY], 420);
});

test('acquire refuses a lock held by a live pid on this host', async () => {
  const held = `vps1:${process.pid}:T0`;
  const r = new FakeRedis({ [KEY]: held });
  const out = await runLock.acquire(r, DATE, 420, { value: 'vps1:999:T1', host: 'vps1' });
  assert.equal(out.ok, false);
  assert.equal(out.holder, held);
  assert.equal(r.store[KEY], held);
});

test('acquire takes over a dead pid on this host and logs it', async () => {
  const logs = [];
  const r = new FakeRedis({ [KEY]: 'vps1:999999:T0' });
  const out = await runLock.acquire(r, DATE, 420,
    { value: 'vps1:111:T1', host: 'vps1', log: (m) => logs.push(m) });
  assert.equal(out.ok, true);
  assert.equal(r.store[KEY], 'vps1:111:T1');
  assert.ok(logs.some(m => m.includes('stale lock from pid 999999')), logs.join('|'));
});

test('acquire never touches a lock owned by another host', async () => {
  const r = new FakeRedis({ [KEY]: 'otherbox:999999:T0' });
  const out = await runLock.acquire(r, DATE, 420, { value: 'vps1:111:T1', host: 'vps1' });
  assert.equal(out.ok, false);
  assert.equal(r.store[KEY], 'otherbox:999999:T0');
});

test('renew and release are value-checked', async () => {
  const r = new FakeRedis({ [KEY]: 'vps1:111:T0' });
  assert.equal(await runLock.renew(r, DATE, 'vps1:111:T0', 9120), true);
  assert.equal(r.ttls[KEY], 9120);

  r.store[KEY] = 'vps1:222:T1';                       // taken over
  assert.equal(await runLock.renew(r, DATE, 'vps1:111:T0', 9120), false);
  assert.equal(await runLock.release(r, DATE, 'vps1:111:T0'), false);
  assert.equal(r.store[KEY], 'vps1:222:T1');
  assert.ok(!r.calls.some(c => c[0] === 'del'));

  r.store[KEY] = 'vps1:111:T0';
  assert.equal(await runLock.release(r, DATE, 'vps1:111:T0'), true);
  assert.equal(r.store[KEY], undefined);
});
```

- [ ] **Step 2: Run it — expect failure**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/lib/run_lock.test.js
```

Expected: `Cannot find module '.../src/lib/run_lock.js'`.

- [ ] **Step 3: Write the twin** — create `src/lib/run_lock.js`:

```javascript
'use strict';
/**
 * run_lock.js — JS twin of src/lib/run_lock.py (QD spec §5 E1, 2026-09-12).
 *
 * Both files read src/lib/run_lock_key.json, so the key can never drift
 * between the Python orchestrator and the LangGraph daily cycle. Value
 * format is byte-identical: `host:pid:start_iso`.
 *
 * Takeover rule (same as the Python twin, same PID-liveness pattern as
 * src/lib/manifest_lock.js `_isProcessAlive`): a lock may be taken over ONLY
 * when its holder host equals ours AND the holder PID is dead. A holder on
 * another host is never touched.
 *
 * Every renew and release compares the stored value to ours first — a process
 * whose lock was taken over must not delete the new owner's key.
 */
const os  = require('node:os');
const CFG = require('./run_lock_key.json');

const KEY_PREFIX        = String(CFG.prefix);
const TTL_SLACK_SECONDS = Number(CFG.ttl_slack_seconds);
const MIN_TTL_SECONDS   = Number(CFG.min_ttl_seconds);
const LOCK_BUSY_RC      = 75;   // EX_TEMPFAIL — mirrors run_lock.py

function lockKey(runDate) { return `${KEY_PREFIX}:${runDate}`; }

function makeValue({ pid, host, startedAt } = {}) {
  const h = host || os.hostname();
  const p = pid == null ? process.pid : Number(pid);
  const t = startedAt || new Date().toISOString();
  return `${h}:${p}:${t}`;
}

function parseValue(raw) {
  if (raw == null) return null;
  const s = Buffer.isBuffer(raw) ? raw.toString('utf8') : String(raw);
  // Split on the FIRST TWO colons only — the ISO timestamp contains colons.
  const i = s.indexOf(':');
  if (i < 1) return null;
  const j = s.indexOf(':', i + 1);
  if (j < 0) return null;
  const host = s.slice(0, i);
  const pid  = Number(s.slice(i + 1, j));
  const startedAt = s.slice(j + 1);
  if (!host || !startedAt || !Number.isInteger(pid)) return null;
  return { host, pid, startedAt };
}

function ttlFor(stepTimeoutSec) {
  const base = Number.isFinite(Number(stepTimeoutSec)) ? Number(stepTimeoutSec) : 0;
  return Math.max(MIN_TTL_SECONDS, Math.trunc(base) + TTL_SLACK_SECONDS);
}

function pidAlive(pid) {
  const p = Number(pid);
  if (!Number.isInteger(p) || p <= 1) return false;
  try { process.kill(p, 0); return true; }
  catch (e) { return e.code === 'EPERM'; }
}

async function acquire(r, runDate, ttlSec, opts = {}) {
  const key   = lockKey(runDate);
  const host  = opts.host || os.hostname();
  const value = opts.value || makeValue({ host });
  const ttl   = Math.trunc(Number(ttlSec));
  const log   = opts.log || (() => {});

  if (await r.set(key, value, 'NX', 'EX', ttl)) return { ok: true, value, holder: null };

  const holderRaw = await r.get(key);
  const holder    = holderRaw == null ? null
                  : (Buffer.isBuffer(holderRaw) ? holderRaw.toString('utf8') : String(holderRaw));
  const parsed    = parseValue(holder);
  if (!parsed) return { ok: false, value: null, holder };

  if (parsed.host === host && !pidAlive(parsed.pid)) {
    log(`[lock] stale lock from pid ${parsed.pid} (host ${parsed.host}, started ${parsed.startedAt}) — taking over ${key}`);
    await r.set(key, value, 'EX', ttl);
    return { ok: true, value, holder: null };
  }
  return { ok: false, value: null, holder };
}

async function renew(r, runDate, value, ttlSec, opts = {}) {
  if (!value) return false;
  const key = lockKey(runDate);
  const ttl = Math.trunc(Number(ttlSec));
  const log = opts.log || (() => {});
  const raw = await r.get(key);
  const cur = raw == null ? null : (Buffer.isBuffer(raw) ? raw.toString('utf8') : String(raw));
  if (cur == null) {
    await r.set(key, value, 'EX', ttl);
    log(`[lock] ${key} had expired — re-taken by ${value} (ttl=${ttl}s)`);
    return true;
  }
  if (cur !== value) { log(`[lock] renew skipped — ${key} held by ${cur}, not us (${value})`); return false; }
  await r.expire(key, ttl);
  return true;
}

async function release(r, runDate, value, opts = {}) {
  if (!value) return false;
  const key = lockKey(runDate);
  const log = opts.log || (() => {});
  const raw = await r.get(key);
  const cur = raw == null ? null : (Buffer.isBuffer(raw) ? raw.toString('utf8') : String(raw));
  if (cur == null) return false;
  if (cur !== value) { log(`[lock] release skipped — ${key} held by ${cur}, not us (${value})`); return false; }
  await r.del(key);
  return true;
}

module.exports = {
  KEY_PREFIX, TTL_SLACK_SECONDS, MIN_TTL_SECONDS, LOCK_BUSY_RC,
  lockKey, makeValue, parseValue, ttlFor, pidAlive,
  acquire, renew, release,
};
```

- [ ] **Step 4: Run it — expect PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/lib/run_lock.test.js
```

Expected: `# pass 8`.

- [ ] **Step 5: Write the failing wiring test** — create `tests/test_daily_cycle_run_lock.test.js`:

```javascript
'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');
const fs       = require('node:fs');

const ROOT = path.resolve(__dirname, '..');

test('daily-cycle.js uses the shared run_lock module, not its own key', () => {
  const src = fs.readFileSync(path.join(ROOT, 'src/agent/graphs/daily-cycle.js'), 'utf8');
  assert.ok(!src.includes('engine:run_lock:'), 'legacy engine:run_lock key must be gone');
  assert.ok(src.includes("require('../../lib/run_lock')"), 'must require the shared module');
  assert.ok(src.includes('runLock.acquire('), 'must acquire via the shared module');
  assert.ok(src.includes('runLock.release('), 'release must be value-checked via the module');
  assert.ok(!/r\.del\(key\)/.test(src), 'no unconditional del of the lock key');
});

test('cron-schedule.js reads the shared lock key for the budget-resume gate', () => {
  const src = fs.readFileSync(path.join(ROOT, 'src/engine/cron-schedule.js'), 'utf8');
  assert.ok(!src.includes('pipeline:running:'), 'legacy pipeline:running key must be gone');
  assert.ok(src.includes("require('../lib/run_lock')"), 'must require the shared module');
  assert.ok(src.includes('runLock.lockKey('), 'must build the key from the shared module');
});
```

- [ ] **Step 6: Rewrite `_acquireRunLock` in `src/agent/graphs/daily-cycle.js`.** Replace `:137-155` (the `// ── Redis lock (mirrors pipeline_orchestrator.py:152-158) ──` comment through the closing brace of `_acquireRunLock`) with:

```javascript
// ── Redis lock (shared with pipeline_orchestrator.py via src/lib/run_lock) ───
// QD E1 (2026-09-12): this used to be its OWN key, `engine:run_lock:<date>`,
// while the Python orchestrator held `pipeline:running:<date>` — two locks, so
// both runners could hold "the" lock at once. One key now, owned value, and a
// value-checked release so a lock we lost to a takeover is never deleted by us.
const runLock = require('../../lib/run_lock');

async function _acquireRunLock(runDate) {
  const Redis = require('ioredis');
  const r = new Redis(process.env.REDIS_URL || 'redis://localhost:6379');
  const key = runLock.lockKey(runDate);
  const ttl = runLock.ttlFor(Number(process.env.OPENCLAW_COLLECT_TIMEOUT_SECONDS || 9000));
  const out = await runLock.acquire(r, runDate, ttl,
    { log: (m) => console.log(`[daily-cycle] ${m}`) });
  if (!out.ok) {
    await r.quit().catch(() => {});
    const err = new Error(`cycle already in progress for ${runDate} (lock held by ${out.holder || '?'})`);
    err.lockHeld = true;
    err.holder = out.holder;
    throw err;
  }
  console.log(`[daily-cycle] [lock] acquired ${key} as ${out.value} (ttl=${ttl}s)`);
  // NOTE: the JS runner does NOT renew. `runDailyCycleGraph` holds the lock
  // across the whole graph and `daily_cycle_node.js` has no handle on it, so
  // there is no place to renew from without threading the lock through the
  // LangGraph state. Instead the initial TTL is sized to the LONGEST step
  // (collect, OPENCLAW_COLLECT_TIMEOUT_SECONDS, default 9000) + 120 s. The
  // Python orchestrator, which owns the production cycle, does renew per step.
  return {
    value: out.value,
    release: async () => {
      try { await runLock.release(r, runDate, out.value,
                                  { log: (m) => console.log(`[daily-cycle] ${m}`) }); }
      finally { await r.quit().catch(() => {}); }
    },
  };
}
```

The existing caller at `:169-181` already does `lock = await _acquireRunLock(runDate)` inside a `try` and branches on `err.lockHeld` — unchanged. The `abortedAt: '__lock_held__'` return value stays as-is (it is the JS runner's own convention; only the Python CLI carries an exit code).

- [ ] **Step 7: Point `cron-schedule.js` at the shared key.** Replace `:209-211`:

```javascript
        // Check if pipeline lock still active (already running)
        const locked = await r.get(`pipeline:running:${runDate}`);
        if (locked) return;
```

with:

```javascript
        // Check if the shared run lock is still held (a cycle is running).
        // QD E1: this read used the dead `pipeline:running:<date>` key; the
        // orchestrator and the LangGraph cycle now share
        // `pipeline:run_lock:<date>` (src/lib/run_lock_key.json).
        const runLock = require('../lib/run_lock');
        const locked = await r.get(runLock.lockKey(runDate));
        if (locked) return;
```

- [ ] **Step 8: Run the JS tests — expect PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/lib/run_lock.test.js tests/test_daily_cycle_run_lock.test.js tests/test_daily_cycle_graph.test.js tests/test_daily_cycle_node.test.js
```

Expected: all pass.

- [ ] **Step 9: Commit**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/lib/run_lock.js src/agent/graphs/daily-cycle.js src/engine/cron-schedule.js tests/lib/run_lock.test.js tests/test_daily_cycle_run_lock.test.js
git commit -m "fix(cycle): JS twin of the shared run lock; daily-cycle + cron read one key (QD E1)

daily-cycle.js drops engine:run_lock:<date> and cron-schedule.js drops the dead
pipeline:running:<date> read; both now use src/lib/run_lock.js over the same
src/lib/run_lock_key.json the Python twin reads. Release is value-checked.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

<!-- END TASK 3 -->

---

### Task 4 — E2a: `src/lib/capped_spawn.py` + cap every orchestrator step

`run_step`'s `subprocess.Popen` at `pipeline_orchestrator.py:603` is bare — an OOMing step lets the kernel's global OOM killer choose a victim on an 8 GB no-swap box, and it has chosen johnbot before. `capped_spawn.js` already solves this for the JS callers (4 of them: `server.js:2286`, `research-orchestrator.js:238`, `staging_approver.js:587`, `backfill_runner.js:39`); this is its Python twin, with its own env var so the two caps stay independently tunable.

**The stdio question is already settled in this codebase.** `systemd-run --scope` execs the command in place, so the pid the spawn returns IS the child — `capped_spawn.js` has wrapped `research-orchestrator._spawnPython`, which pipes and reads stdout/stderr and calls `child.kill()`, in production since 2026-08-30. Piped stdio, pid registration and signal delivery are therefore proven unaffected here, which is what lets `run_step`'s stdout-idle watchdog and `proc.terminate()` keep working unchanged around the wrap.

**Files:**
- Create: `src/lib/capped_spawn.py`
- Create: `tests/lib/test_capped_spawn.py`
- Modify: `src/execution/pipeline_orchestrator.py` (`run_step`, the Popen at `:603` and the log line at `:600`)

**Interfaces:**
- Consumes: `OPENCLAW_STEP_MEMORY_MAX` (systemd size string; default `'4500M'`; `'0'` or empty disables).
- Produces:
  - `DEFAULT_MEMORY_MAX: str = '4500M'`, `MEMORY_MAX_ENV: str = 'OPENCLAW_STEP_MEMORY_MAX'`
  - `default_memory_max() -> str`
  - `wrap_capped(cmd: list[str], memory_max=None, log=None) -> tuple[list[str], str | None]` — returns `(argv, cap_applied_or_None)`
  - `_reset(available=None)` — test hook that pins the availability decision without probing the host

- [ ] **Step 1: Write the failing test** — create `tests/lib/test_capped_spawn.py`:

```python
"""tests/lib/test_capped_spawn.py — Python twin of capped_spawn.js (QD §5 E2).

Never probes the host: _reset() pins availability so both branches are
exercised deterministically, and no systemd-run is ever executed.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from lib import capped_spawn as cs  # noqa: E402


class TestDefaults(unittest.TestCase):
    def tearDown(self):
        cs._reset()

    def test_default_cap_is_4500M(self):
        self.assertEqual(cs.DEFAULT_MEMORY_MAX, '4500M')
        self.assertEqual(cs.MEMORY_MAX_ENV, 'OPENCLAW_STEP_MEMORY_MAX')

    def test_env_overrides_the_default(self):
        import os
        old = os.environ.get('OPENCLAW_STEP_MEMORY_MAX')
        os.environ['OPENCLAW_STEP_MEMORY_MAX'] = '2G'
        try:
            self.assertEqual(cs.default_memory_max(), '2G')
        finally:
            if old is None:
                del os.environ['OPENCLAW_STEP_MEMORY_MAX']
            else:
                os.environ['OPENCLAW_STEP_MEMORY_MAX'] = old

    def test_it_does_not_read_the_backtest_cap_var(self):
        import os
        old_step = os.environ.pop('OPENCLAW_STEP_MEMORY_MAX', None)
        old_bt = os.environ.get('OPENCLAW_BACKTEST_MEMORY_MAX')
        os.environ['OPENCLAW_BACKTEST_MEMORY_MAX'] = '999M'
        try:
            self.assertEqual(cs.default_memory_max(), '4500M')
        finally:
            if old_step is not None:
                os.environ['OPENCLAW_STEP_MEMORY_MAX'] = old_step
            if old_bt is None:
                del os.environ['OPENCLAW_BACKTEST_MEMORY_MAX']
            else:
                os.environ['OPENCLAW_BACKTEST_MEMORY_MAX'] = old_bt


class TestWrap(unittest.TestCase):
    def tearDown(self):
        cs._reset()

    def test_wraps_in_a_transient_scope_when_available(self):
        cs._reset(available=True)
        argv, cap = cs.wrap_capped(['python3', 'x.py', '--date', '2026-09-14'], memory_max='4500M')
        self.assertEqual(cap, '4500M')
        self.assertEqual(argv, ['systemd-run', '--scope', '--collect', '--quiet',
                                '-p', 'MemoryMax=4500M', '--',
                                'python3', 'x.py', '--date', '2026-09-14'])

    def test_passes_through_untouched_when_unavailable(self):
        cs._reset(available=False)
        argv, cap = cs.wrap_capped(['python3', 'x.py'], memory_max='4500M')
        self.assertIsNone(cap)
        self.assertEqual(argv, ['python3', 'x.py'])

    def test_zero_cap_disables_even_when_available(self):
        cs._reset(available=True)
        argv, cap = cs.wrap_capped(['node', 'y.js'], memory_max='0')
        self.assertIsNone(cap)
        self.assertEqual(argv, ['node', 'y.js'])

    def test_empty_cap_disables(self):
        cs._reset(available=True)
        argv, cap = cs.wrap_capped(['node', 'y.js'], memory_max='   ')
        self.assertIsNone(cap)
        self.assertEqual(argv, ['node', 'y.js'])

    def test_returns_a_copy_never_the_caller_list(self):
        cs._reset(available=False)
        original = ['python3', 'x.py']
        argv, _cap = cs.wrap_capped(original)
        argv.append('--mutated')
        self.assertEqual(original, ['python3', 'x.py'])

    def test_warns_once_when_unavailable(self):
        logs = []
        cs._reset()
        cs._STATE['probe'] = lambda: False
        cs._STATE['uid'] = lambda: 1001
        cs.wrap_capped(['a'], log=logs.append)
        cs.wrap_capped(['b'], log=logs.append)
        self.assertEqual(len(logs), 1, logs)
        self.assertIn('uid=1001', logs[0])


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run it — expect failure**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest -q tests/lib/test_capped_spawn.py
```

Expected: `ModuleNotFoundError: No module named 'lib.capped_spawn'`.

- [ ] **Step 3: Write the implementation** — create `src/lib/capped_spawn.py`:

```python
"""capped_spawn.py — Python twin of src/lib/capped_spawn.js (QD spec §5 E2).

Why: `pipeline_orchestrator.run_step` spawns every daily-cycle step with a
bare `subprocess.Popen` and no memory limit. On an 8 GB no-swap box the
kernel's GLOBAL OOM killer then picks the victim, and it has picked johnbot.
A transient `systemd-run --scope -p MemoryMax=` cgroup turns that into a
contained kill of the one step (rc=137, which run_step already surfaces and
which the bounded `signals` retry already handles).

TRADE, stated plainly: capping converts "something on the box dies" into
"this step dies and the cycle aborts here". Only `signals` retries, so a
capped `collect` that OOMs is a new hard-abort path. That is the intended
exchange — an aborted, visible cycle beats a silently killed bot.

`systemd-run --scope` execs the command in place (the child pid equals the
pid Popen returns), so stdout piping, `proc.terminate()` and the stdout-idle
watchdog in run_step are unaffected.

Applies only when (a) the cap is non-empty and not '0', (b) we are uid 0 (the
system manager only lets root create scopes; timer units running as claudebot
stay inside their own unit cgroup), and (c) a one-time probe scope succeeds.
Otherwise the argv passes through untouched and the reason is logged ONCE.

Cap: OPENCLAW_STEP_MEMORY_MAX (systemd size string; default 4500M). Kept
DISTINCT from capped_spawn.js's OPENCLAW_BACKTEST_MEMORY_MAX so the cycle
steps and the research/backtest children can be tuned independently.
"""
from __future__ import annotations

import os
import subprocess

DEFAULT_MEMORY_MAX = '4500M'
MEMORY_MAX_ENV     = 'OPENCLAW_STEP_MEMORY_MAX'


def _real_probe() -> bool:
    try:
        r = subprocess.run(
            ['systemd-run', '--scope', '--collect', '--quiet',
             '-p', 'MemoryMax=64M', '--', '/bin/true'],
            timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return r.returncode == 0
    except Exception:
        return False


_STATE = {'probe': _real_probe, 'uid': os.getuid, 'available': None, 'warned': False}


def default_memory_max() -> str:
    v = os.environ.get(MEMORY_MAX_ENV)
    if v is None or not str(v).strip():
        return DEFAULT_MEMORY_MAX
    return str(v).strip()


def _available(log=None) -> bool:
    if _STATE['available'] is None:
        uid = _STATE['uid']() if callable(_STATE['uid']) else _STATE['uid']
        _STATE['available'] = (uid == 0) and bool(_STATE['probe']())
        if not _STATE['available'] and not _STATE['warned']:
            _STATE['warned'] = True
            msg = (f'[capped_spawn] transient scopes unavailable (uid={uid}) '
                   f'— children run uncapped')
            (log or print)(msg)
    return _STATE['available']


def wrap_capped(cmd, memory_max=None, log=None):
    """Return `(argv, cap_applied_or_None)`. Never mutates `cmd`."""
    cap = default_memory_max() if memory_max is None else str(memory_max or '').strip()
    plain = list(cmd)
    if not cap or cap == '0':
        return plain, None
    if not _available(log=log):
        return plain, None
    return (['systemd-run', '--scope', '--collect', '--quiet',
             '-p', f'MemoryMax={cap}', '--', *plain], cap)


def _reset(available=None):
    """Test hook: pin the availability decision; None restores the real probe."""
    _STATE['probe'] = _real_probe
    _STATE['uid'] = os.getuid
    _STATE['available'] = available
    _STATE['warned'] = False
```

- [ ] **Step 4: Run it — expect PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest -q tests/lib/test_capped_spawn.py
```

Expected: `9 passed`.

- [ ] **Step 5: Write the failing wiring test** — append to `tests/execution/test_run_lock_wiring.py` (it already imports `po`):

```python
class TestRunStepIsCapped(unittest.TestCase):
    """run_step wraps the child in a MemoryMax scope when one is available."""

    def setUp(self):
        from lib import capped_spawn as cs
        self.cs = cs
        self._orig_resolve = po._resolve_script

    def tearDown(self):
        po._resolve_script = self._orig_resolve
        self.cs._reset()

    def test_argv_is_wrapped_when_scopes_are_available(self):
        seen = {}
        self.cs._reset(available=True)
        po._resolve_script = lambda script, run_date: (['true'], 5)

        real_popen = po.subprocess.Popen

        def _spy(cmd, **kw):
            seen['cmd'] = list(cmd)
            return real_popen(['true'], **kw)

        po.subprocess.Popen = _spy
        try:
            po.run_step('engine', DATE, dict(os.environ))
        finally:
            po.subprocess.Popen = real_popen
        self.assertEqual(seen['cmd'][:6],
                         ['systemd-run', '--scope', '--collect', '--quiet',
                          '-p', 'MemoryMax=4500M'])
        self.assertEqual(seen['cmd'][-1], 'true')

    def test_argv_is_untouched_when_scopes_are_unavailable(self):
        seen = {}
        self.cs._reset(available=False)
        po._resolve_script = lambda script, run_date: (['true'], 5)

        real_popen = po.subprocess.Popen

        def _spy(cmd, **kw):
            seen['cmd'] = list(cmd)
            return real_popen(['true'], **kw)

        po.subprocess.Popen = _spy
        try:
            ok, rc = po.run_step('engine', DATE, dict(os.environ))
        finally:
            po.subprocess.Popen = real_popen
        self.assertEqual(seen['cmd'], ['true'])
        self.assertTrue(ok)
```

- [ ] **Step 6: Wire it into `run_step`.** In `src/execution/pipeline_orchestrator.py`, immediately after the `renew(timeout)` block added in Task 2 Step 5, insert:

```python
    # QD E2: contain an OOMing step to its own cgroup. Without this the
    # kernel's global OOM killer picks the victim on this 8 GB no-swap box
    # (it has picked johnbot). rc=137 already routes to the bounded retry.
    from lib import capped_spawn as _capped_spawn
    cmd, _cap_applied = _capped_spawn.wrap_capped(cmd, log=log)
```

and change the log line at `:600` from

```python
    log(f'Starting {script} timeout={timeout}s stdout_idle_max={stdout_idle_max_s}s (cmd: {" ".join(cmd)})...')
```

to

```python
    log(f'Starting {script} timeout={timeout}s stdout_idle_max={stdout_idle_max_s}s '
        f'memory_max={_cap_applied or "uncapped"} (cmd: {" ".join(cmd)})...')
```

(the log line must move BELOW the `wrap_capped` call so it prints the wrapped argv).

- [ ] **Step 7: Run the tests — expect PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest -q tests/lib/test_capped_spawn.py tests/execution/test_run_lock_wiring.py
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/lib/capped_spawn.py tests/lib/test_capped_spawn.py src/execution/pipeline_orchestrator.py tests/execution/test_run_lock_wiring.py
git commit -m "feat(cycle): MemoryMax transient scope around every orchestrator step (QD E2)

src/lib/capped_spawn.py is the Python twin of capped_spawn.js with its own
OPENCLAW_STEP_MEMORY_MAX (default 4500M, '0' disables) and a graceful
pass-through when systemd-run is unavailable or we are not uid 0. run_step now
wraps its Popen argv, so an OOMing step dies rc=137 in its own cgroup instead
of letting the global OOM killer pick johnbot.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

<!-- END TASK 4 -->

---

### Task 5 — E2b: cap the JS step runner and the cron launch paths

`daily_cycle_helpers.runSubprocess` (`:25-72`) spawns every LangGraph cycle step uncapped (its only caller is `daily_cycle_node.js:59`), and `cron-schedule.js` detaches two orchestrator spawns uncapped (`:223` the budget-resume `--force-resume` run, `:311` the legacy 10 am `scripts/run_pipeline.py` run). All three get the existing `wrapCapped` contract with the **step** cap, not the backtest cap.

**Files:**
- Modify: `src/agent/graphs/daily_cycle_helpers.js` (imports, `runSubprocess`, `module.exports`)
- Modify: `src/engine/cron-schedule.js` (:222-229, :311-316)
- Create: `tests/test_daily_cycle_capped_spawn.test.js`

**Interfaces:**
- Consumes: `require('../../lib/capped_spawn').wrapCapped(cmd, args, {memoryMax})` — existing contract, `src/lib/capped_spawn.js:63`, returns `{cmd, args, capped, memoryMax}`; test hook `_internals.reset({probe, uid})` at `:73`.
- Produces (new export from `daily_cycle_helpers.js`): `stepMemoryMax() -> string` — reads `OPENCLAW_STEP_MEMORY_MAX`, default `'4500M'` (the SAME var the Python twin reads; deliberately not `OPENCLAW_BACKTEST_MEMORY_MAX`, which `wrapCapped`'s own default would pick up).
- `runSubprocess(argv, {timeoutSec, env, cwd, memoryMax})` — new optional `memoryMax`; `undefined` ⇒ `stepMemoryMax()`, `'0'` ⇒ uncapped. Return shape gains `memoryMax: string|null`; existing fields unchanged, so `daily_cycle_node.js:59` and `tests/test_daily_cycle_helpers.test.js` keep passing untouched.

- [ ] **Step 1: Write the failing test** — create `tests/test_daily_cycle_capped_spawn.test.js`:

```javascript
'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');
const fs       = require('node:fs');

const ROOT     = path.resolve(__dirname, '..');
const helpers  = require(path.join(ROOT, 'src/agent/graphs/daily_cycle_helpers.js'));
const capped   = require(path.join(ROOT, 'src/lib/capped_spawn.js'));

test('stepMemoryMax defaults to 4500M and reads OPENCLAW_STEP_MEMORY_MAX', () => {
  const old = process.env.OPENCLAW_STEP_MEMORY_MAX;
  delete process.env.OPENCLAW_STEP_MEMORY_MAX;
  try {
    assert.equal(helpers.stepMemoryMax(), '4500M');
    process.env.OPENCLAW_STEP_MEMORY_MAX = '3G';
    assert.equal(helpers.stepMemoryMax(), '3G');
  } finally {
    if (old === undefined) delete process.env.OPENCLAW_STEP_MEMORY_MAX;
    else process.env.OPENCLAW_STEP_MEMORY_MAX = old;
  }
});

test('runSubprocess reports memoryMax=null and still works when scopes are unavailable', async () => {
  capped._internals.reset({ probe: () => false, uid: () => 1001 });
  try {
    const out = await helpers.runSubprocess(['echo', 'hi'], { timeoutSec: 5, env: process.env });
    assert.equal(out.rc, 0);
    assert.match(out.stdout || '', /hi/);
    assert.equal(out.memoryMax, null);
  } finally {
    capped._internals.reset();
  }
});

test('runSubprocess wraps the argv in a MemoryMax scope when available', async () => {
  // Pretend scopes work but never actually run systemd-run: assert on the
  // wrapper contract instead, using the same call runSubprocess makes.
  capped._internals.reset({ probe: () => true, uid: () => 0 });
  try {
    const w = capped.wrapCapped('echo', ['hi'], { memoryMax: helpers.stepMemoryMax() });
    assert.equal(w.capped, true);
    assert.equal(w.cmd, 'systemd-run');
    assert.deepEqual(w.args.slice(0, 6),
      ['--scope', '--collect', '--quiet', '-p', 'MemoryMax=4500M', '--']);
  } finally {
    capped._internals.reset();
  }
});

test('memoryMax "0" disables the wrap even when scopes are available', async () => {
  capped._internals.reset({ probe: () => true, uid: () => 0 });
  try {
    const out = await helpers.runSubprocess(['echo', 'hi'],
      { timeoutSec: 5, env: process.env, memoryMax: '0' });
    assert.equal(out.rc, 0);
    assert.equal(out.memoryMax, null);
  } finally {
    capped._internals.reset();
  }
});

test('cron-schedule spawns the orchestrator through wrapCapped', () => {
  const src = fs.readFileSync(path.join(ROOT, 'src/engine/cron-schedule.js'), 'utf8');
  const hits = src.match(/wrapCapped\(/g) || [];
  assert.ok(hits.length >= 2, `expected >= 2 wrapCapped call sites, found ${hits.length}`);
  assert.ok(src.includes("require('../lib/capped_spawn')"), 'must require capped_spawn');
  assert.ok(src.includes('OPENCLAW_STEP_MEMORY_MAX'), 'must use the step cap var');
});
```

- [ ] **Step 2: Run it — expect failure**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/test_daily_cycle_capped_spawn.test.js
```

Expected: `helpers.stepMemoryMax is not a function`.

- [ ] **Step 3: Modify `src/agent/graphs/daily_cycle_helpers.js`.** Change the header import line (`:10`) from `const { spawn } = require('node:child_process');` to:

```javascript
const { spawn } = require('node:child_process');
const { wrapCapped } = require('../../lib/capped_spawn');

// QD E2 (2026-09-12): the cycle-step cap. Deliberately its own env var —
// wrapCapped's built-in default reads OPENCLAW_BACKTEST_MEMORY_MAX, which
// tunes the research/backtest children, not the daily cycle.
const DEFAULT_STEP_MEMORY_MAX = '4500M';

function stepMemoryMax() {
  const v = process.env.OPENCLAW_STEP_MEMORY_MAX;
  return (v === undefined || v === null || String(v).trim() === '')
    ? DEFAULT_STEP_MEMORY_MAX : String(v).trim();
}
```

Change the `runSubprocess` signature and spawn (`:25-37`):

```javascript
function runSubprocess(argv, { timeoutSec = 600, env = process.env, cwd, memoryMax } = {}) {
  return new Promise((resolve) => {
    const startedAt = Date.now();
    const cap = (memoryMax === undefined) ? stepMemoryMax() : memoryMax;
    const wrapped = wrapCapped(argv[0], argv.slice(1), { memoryMax: cap });
    const cmd  = wrapped.cmd;
    const args = wrapped.args;
    let stdout = '';
    let stderr = '';
    let timedOut = false;

    const proc = spawn(cmd, args, {
      env,
      cwd: cwd || process.cwd(),
      stdio: ['ignore', 'pipe', 'pipe'],
    });
```

Add `memoryMax` to the `close` resolve payload (`:50-58`):

```javascript
      resolve({
        rc,
        stdout,
        stderrTail: stderr.slice(-4000),
        durationMs,
        timedOut,
        memoryMax: wrapped.memoryMax,
      });
```

and to the `error` resolve payload (`:62-69`) as `memoryMax: wrapped.memoryMax`. Finally extend the exports (`:183-186`):

```javascript
module.exports = {
  skipForSubset, strictMode, runSubprocess, stepMemoryMax,
  formatAbortAlert, postAbortAlert,
};
```

- [ ] **Step 4: Cap the two `cron-schedule.js` launch paths.** Replace `:222-229` (the budget-resume spawn):

```javascript
        const orchestrator = path.join(ROOT, 'src', 'execution', 'pipeline_orchestrator.py');
        const { wrapCapped } = require('../lib/capped_spawn');   // QD E2: MemoryMax scope
        const w = wrapCapped('python3', [orchestrator, '--date', runDate, '--force-resume'],
                             { memoryMax: process.env.OPENCLAW_STEP_MEMORY_MAX || '4500M' });
        const proc = spawn(w.cmd, w.args, {
            cwd:      ROOT,
            env:      { ...process.env, PYTHONPATH: ROOT },
            detached: true,
            stdio:    'ignore',
        });
```

and `:311-316` (the legacy 10 am spawn):

```javascript
            const { wrapCapped } = require('../lib/capped_spawn');   // QD E2: MemoryMax scope
            const w = wrapCapped(PYTHON, ['scripts/run_pipeline.py', '--date', today],
                                 { memoryMax: process.env.OPENCLAW_STEP_MEMORY_MAX || '4500M' });
            const child = spawn(w.cmd, w.args, {
                cwd: ROOT,
                env: { ...process.env },
                detached: true,
                stdio: ['ignore', logFd, logFd],
            });
```

- [ ] **Step 5: Run the tests — expect PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/test_daily_cycle_capped_spawn.test.js tests/test_daily_cycle_helpers.test.js tests/test_daily_cycle_node.test.js tests/test_daily_cycle_abort_alert.test.js
```

Expected: all pass — the four pre-existing `runSubprocess` tests in `test_daily_cycle_helpers.test.js` still pass because the wrap is a no-op when the probe fails (which it does under a non-root test run).

- [ ] **Step 6: Commit**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/agent/graphs/daily_cycle_helpers.js src/engine/cron-schedule.js tests/test_daily_cycle_capped_spawn.test.js
git commit -m "feat(cycle): MemoryMax scope around the JS step runner and cron launches (QD E2)

runSubprocess and both cron-schedule orchestrator spawns go through
wrapCapped with OPENCLAW_STEP_MEMORY_MAX (default 4500M). Return shape gains
memoryMax; existing callers and tests unchanged because the wrap is a no-op
whenever systemd-run is unavailable.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

<!-- END TASK 5 -->

---

### Task 6 — E2c: `OnFailure=` drop-ins on the 22 units that lack one — **OPERATOR-RUN**

> **OPERATOR-RUN.** This task writes into `/etc/systemd/system/` and runs `systemctl daemon-reload` on the production host. An agent must NOT execute it — it prepares the repo-side snapshot files and hands the operator the exact command block. Nothing here is testable by pytest or `node --test`; the verification is `systemctl show`.

The spec's list was written from the fit review and is stale in three ways: it names `afterhours-redeploy` (no such unit), it names six units that **already** have `OnFailure=` (`cboe-chains`, `options-archive`, `fmp-profiles`, `edgar-shares`, `finra-short-interest`, `macro-rates`, `nasdaq-earnings-calendar`, `research-retry@`), and it misses eleven that lack it. The list below was derived on 2026-09-13 from the live host and is **drop-in aware** — `grep -L OnFailure /etc/systemd/system/openclaw-*.service` returns 38 names, but 15 of those already receive `OnFailure=` from a `.service.d/onfailure.conf` drop-in. Excluding `openclaw-failure-notify@.service` itself leaves exactly **22**.

**The 22 units that genuinely lack `OnFailure=` (unit file AND drop-ins):**

```
openclaw-afterhours-stop-monitor.service
openclaw-afterhours-tp-postmarket.service
openclaw-afterhours-tp-premarket.service
openclaw-afterhours-tp-rth-reconcile.service
openclaw-amcheck.service
openclaw-edgar-8k@.service
openclaw-fleet-overnight-resume.service
openclaw-mastermind-critique.service
openclaw-options-eligibility.service
openclaw-options-surface-flip.service
openclaw-premarket-realized-backfill.service
openclaw-premarket-scan@.service
openclaw-refresh-universe-sizes.service
openclaw-research-commit.service
openclaw-rf-flip.service
openclaw-sp5-cleanup.service
openclaw-stop-reattach.service
openclaw-target-mode-flip.service
openclaw-universe-recs.service
openclaw-weekend-maintenance-sat.service
openclaw-weekend-maintenance-sun.service
openclaw-weekend-sunday.service
```

Excluded deliberately: `openclaw-failure-notify@.service` (a unit must never notify on its own failure — infinite loop), and the 15 units already covered by a drop-in (`backtest-refresh`, `botjohn-maintenance`, `botjohn-saturday-maintenance`, `botjohn-saturday-verify`, `eod-refresh`, `mastermind-corpus`, `paper-expansion`, `phase2d-nightly`, `position-recs`, `regime-live-pnl`, `saturday-brain`, `strategy-backtest-refresh`, `strategy-review`, `tradable-universe-refresh`, `weekly-strategy-weights`) plus the 15 that carry it in the unit file itself (including `sunday-research-ingest`/`sunday-research-code`/`sunday-code-review`, which route to `openclaw-research-retry@%n.service` — do NOT overwrite those with the notifier).

Two of the 22 are **templates** (`openclaw-edgar-8k@.service`, `openclaw-premarket-scan@.service`). `%n` on a template expands to the full instance name, producing `openclaw-failure-notify@openclaw-premarket-scan@0930.service` — a name with two `@`. **Verified on this host 2026-09-13** that systemd parses it: `systemctl show 'openclaw-failure-notify@openclaw-premarket-scan@0930.service' -p Names -p LoadState` → `LoadState=loaded`, instance resolved at the FIRST `@`. So the drop-in content is uniform across all 22; the templates need no special form.

**Files:**
- Create (repo snapshot, 22 files): `docs/systemd/<unit>.service.d/onfailure.conf` for each unit above (the `@` in a template name stays in the directory name, e.g. `docs/systemd/openclaw-edgar-8k@.service.d/onfailure.conf`).
- Modify: nothing else in the repo. `/etc/systemd/system/**` is host state, not repo state.

**Interfaces:**
- Consumes: the existing template `openclaw-failure-notify@.service` (`ExecStart=/usr/bin/python3 /root/openclaw/scripts/systemd_failure_notify.py %i`, `User=claudebot`, `EnvironmentFile=/root/openclaw/.env`) and `scripts/systemd_failure_notify.py:139`, which takes the failed unit name as `argv[1]` and posts to the `botjohn-log` webhook from `agent_registry`.
- Produces: identical 2-line drop-ins.

- [ ] **Step 1: Re-verify the list before touching anything** (read-only; safe for an agent to run):

```bash
grep -L OnFailure /etc/systemd/system/openclaw-*.service | sed 's#.*/##' | sort > /tmp/qd_e2_nofile.txt
grep -rl OnFailure /etc/systemd/system/openclaw-*.service.d/ 2>/dev/null | sed 's#.*/\(openclaw-[^/]*\.service\)\.d/.*#\1#' | sort -u > /tmp/qd_e2_hasdropin.txt
comm -23 /tmp/qd_e2_nofile.txt /tmp/qd_e2_hasdropin.txt | grep -v '^openclaw-failure-notify@\.service$'
```

Expected: exactly the 22 names above. **If the output differs, stop and reconcile the plan before installing** — a unit added or removed since 2026-09-13 changes the set.

- [ ] **Step 2: Write the 22 repo snapshot files.** Each is byte-identical (copy the exact form already in use at `/etc/systemd/system/openclaw-backtest-refresh.service.d/onfailure.conf`):

```ini
[Unit]
OnFailure=openclaw-failure-notify@%n.service
```

Write the 22 files with the editor tool, one per unit — **not** with a shell `for` loop. (A loop that builds paths from a variable and writes into the repo is refused by the worktree-isolation guard, and it is also the kind of command that silently writes 22 files to the wrong place if one name is mistyped.) The 22 target paths:

```
docs/systemd/openclaw-afterhours-stop-monitor.service.d/onfailure.conf
docs/systemd/openclaw-afterhours-tp-postmarket.service.d/onfailure.conf
docs/systemd/openclaw-afterhours-tp-premarket.service.d/onfailure.conf
docs/systemd/openclaw-afterhours-tp-rth-reconcile.service.d/onfailure.conf
docs/systemd/openclaw-amcheck.service.d/onfailure.conf
docs/systemd/openclaw-edgar-8k@.service.d/onfailure.conf
docs/systemd/openclaw-fleet-overnight-resume.service.d/onfailure.conf
docs/systemd/openclaw-mastermind-critique.service.d/onfailure.conf
docs/systemd/openclaw-options-eligibility.service.d/onfailure.conf
docs/systemd/openclaw-options-surface-flip.service.d/onfailure.conf
docs/systemd/openclaw-premarket-realized-backfill.service.d/onfailure.conf
docs/systemd/openclaw-premarket-scan@.service.d/onfailure.conf
docs/systemd/openclaw-refresh-universe-sizes.service.d/onfailure.conf
docs/systemd/openclaw-research-commit.service.d/onfailure.conf
docs/systemd/openclaw-rf-flip.service.d/onfailure.conf
docs/systemd/openclaw-sp5-cleanup.service.d/onfailure.conf
docs/systemd/openclaw-stop-reattach.service.d/onfailure.conf
docs/systemd/openclaw-target-mode-flip.service.d/onfailure.conf
docs/systemd/openclaw-universe-recs.service.d/onfailure.conf
docs/systemd/openclaw-weekend-maintenance-sat.service.d/onfailure.conf
docs/systemd/openclaw-weekend-maintenance-sun.service.d/onfailure.conf
docs/systemd/openclaw-weekend-sunday.service.d/onfailure.conf
```

Note `openclaw-fleet-overnight-resume.service.d/` already exists (it holds `oom-continue.conf`, `rf-macro.conf`, `target-atr-r.conf`) — add `onfailure.conf` beside them, do not replace the directory. Then verify:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && ls docs/systemd/openclaw-*.service.d/onfailure.conf | wc -l
cd /root/openclaw/.claude/worktrees/qd-adoptions && git status --porcelain docs/systemd | wc -l
```

Expected: the first count grows by exactly 22 over its pre-task value (record that value first with the same command); `git status` shows only additions under `docs/systemd/`.

- [ ] **Step 3: OPERATOR — install onto the host.** Hand the operator this block verbatim:

```bash
cd /root/openclaw
for u in openclaw-afterhours-stop-monitor openclaw-afterhours-tp-postmarket \
         openclaw-afterhours-tp-premarket openclaw-afterhours-tp-rth-reconcile \
         openclaw-amcheck 'openclaw-edgar-8k@' openclaw-fleet-overnight-resume \
         openclaw-mastermind-critique openclaw-options-eligibility \
         openclaw-options-surface-flip openclaw-premarket-realized-backfill \
         'openclaw-premarket-scan@' openclaw-refresh-universe-sizes \
         openclaw-research-commit openclaw-rf-flip openclaw-sp5-cleanup \
         openclaw-stop-reattach openclaw-target-mode-flip openclaw-universe-recs \
         openclaw-weekend-maintenance-sat openclaw-weekend-maintenance-sun \
         openclaw-weekend-sunday; do
  install -d -m 0755 "/etc/systemd/system/${u}.service.d"
  install -m 0644 "docs/systemd/${u}.service.d/onfailure.conf" \
                  "/etc/systemd/system/${u}.service.d/onfailure.conf"
done
systemctl daemon-reload
```

`daemon-reload` does not restart or start anything; drop-ins take effect on each unit's next activation. Timer-spawned oneshots pick it up on their next fire — no timer boundary risk.

- [ ] **Step 4: OPERATOR — verify.** Every line must print the notifier:

```bash
for u in openclaw-afterhours-stop-monitor openclaw-afterhours-tp-postmarket \
         openclaw-afterhours-tp-premarket openclaw-afterhours-tp-rth-reconcile \
         openclaw-amcheck openclaw-fleet-overnight-resume \
         openclaw-mastermind-critique openclaw-options-eligibility \
         openclaw-options-surface-flip openclaw-premarket-realized-backfill \
         openclaw-refresh-universe-sizes openclaw-research-commit \
         openclaw-rf-flip openclaw-sp5-cleanup openclaw-stop-reattach \
         openclaw-target-mode-flip openclaw-universe-recs \
         openclaw-weekend-maintenance-sat openclaw-weekend-maintenance-sun \
         openclaw-weekend-sunday; do
  printf '%-48s %s\n' "$u" "$(systemctl show "${u}.service" -p OnFailure --value)"
done
# templates: check a real instance, not the template itself
systemctl show 'openclaw-premarket-scan@0930.service' -p OnFailure --value
systemctl show 'openclaw-edgar-8k@0715.service'       -p OnFailure --value
```

Expected: `openclaw-failure-notify@<the unit's own name>.service` on every line, including both instances. Confirm nothing regressed on the three research units:

```bash
systemctl show openclaw-sunday-research-ingest.service -p OnFailure --value   # must stay openclaw-research-retry@...
```

- [ ] **Step 5: OPERATOR — one live smoke.** Post-proof only; no repo change:

```bash
systemd-run --unit=qd-e2-smoke --property=OnFailure=openclaw-failure-notify@qd-e2-smoke.service /bin/false
journalctl -u openclaw-failure-notify@qd-e2-smoke.service -n 20 --no-pager
```

Expected: the notifier unit ran and posted to `#botjohn-log`. Then `systemctl reset-failed qd-e2-smoke.service` to clean up.

- [ ] **Step 6: Commit the snapshot**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add docs/systemd
git commit -m "ops(systemd): OnFailure notify drop-ins for the 22 units that lacked one (QD E2)

Verified drop-in aware: grep -L over the unit files returns 38, but 15 already
receive OnFailure from a .service.d/onfailure.conf and failure-notify@ itself is
excluded, leaving 22. Both template units (edgar-8k@, premarket-scan@) take the
same %n form — verified that systemd parses the resulting double-@ instance
name. Snapshot only; installation and daemon-reload are operator-run.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

<!-- END TASK 6 -->

---

### Task 7 — E3a: `proc_heartbeat` twins, call sites, `proc_registry` check, doctor `co_tenant_memory`

When the box is under memory pressure the operator currently has `ps` and guesswork. This gives every long-lived child a self-declared Redis identity, a system check that lists them, and a doctor line that names any python over 1 GB by argv. It also repairs `doctor.check_orchestrator_lock` (`:821-843`), whose `scan_iter('pipeline:lock:*')` matches **neither** the old key (`pipeline:running:*`) nor the new one — it has been reporting "no locks held" unconditionally.

**Cadence, honestly:** `run_step` writes from inside its existing 30 s `proc.wait` poll loop — no timer thread (spec §0: no always-on threads). The fleet driver blocks in `spawnSync` for up to `PER_TIMEOUT_S` (3600 s) and cannot tick at 60 s, so it writes **once before each spawn** with `ttl = PER_TIMEOUT_S + 300`. `run_premarket_scan.py` writes once at start. Only the orchestrator meets the spec's "every 60 s"; the plan says so rather than pretending.

**Files:**
- Create: `src/lib/proc_heartbeat.py`, `src/lib/proc_heartbeat.js`
- Create: `tests/lib/test_proc_heartbeat.py`, `tests/lib/proc_heartbeat.test.js`
- Modify: `src/execution/pipeline_orchestrator.py` (`run_step` poll loop)
- Modify: `src/pipeline/run_premarket_scan.py` (`main`, :304)
- Modify: `scripts/refresh_backtests_resumable.js` (the per-strategy loop, ~:168-172)
- Modify: `src/system_checks/checks/agents.py` (new `proc_registry` check)
- Modify: `src/maintenance/doctor.py` (`check_orchestrator_lock` :821; new `check_co_tenant_memory`)

**Interfaces:**
- Produces (Python, `src/lib/proc_heartbeat.py`):
  - `KEY_PREFIX = 'proc'`, `DEFAULT_TTL_S = 180`
  - `heartbeat_key(host=None, pid=None) -> str` → `proc:<host>:<pid>`
  - `rss_mb(pid=None) -> float | None`
  - `write(r, *, step, argv=None, pid=None, host=None, started_at=None, ttl_s=DEFAULT_TTL_S) -> str | None` (returns the key, or None on any Redis error — never raises)
  - `clear(r, *, pid=None, host=None) -> None`
- Produces (JS, `src/lib/proc_heartbeat.js`): `KEY_PREFIX`, `DEFAULT_TTL_SEC`, `heartbeatKey({host, pid})`, `rssMb(pid)`, `writeHeartbeat(r, {step, argv, pid, host, startedAt, ttlSec})`, `clearHeartbeat(r, {pid, host})`.
- Hash fields (identical in both twins, all string-valued): `argv`, `step`, `rss_mb`, `started_at`, `updated_at`, `host`, `pid`.
- Consumes: a Redis-like object with `hset(key, mapping=...)` / `hset(key, obj)` and `expire(key, ttl)`; `delete`/`del`.

- [ ] **Step 1: Write the failing Python test** — create `tests/lib/test_proc_heartbeat.py`:

```python
"""tests/lib/test_proc_heartbeat.py — per-process identity in Redis (QD §5 E3)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from lib import proc_heartbeat as ph  # noqa: E402


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.ttls = {}

    def hset(self, key, mapping=None, **kw):
        self.hashes.setdefault(key, {}).update(mapping or kw)
        return len(mapping or kw)

    def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    def delete(self, key):
        self.hashes.pop(key, None)
        self.ttls.pop(key, None)


class ExplodingRedis:
    def hset(self, *a, **kw):
        raise RuntimeError('redis down')

    def expire(self, *a, **kw):
        raise RuntimeError('redis down')

    def delete(self, *a, **kw):
        raise RuntimeError('redis down')


class TestKeyAndRss(unittest.TestCase):
    def test_key_shape(self):
        self.assertEqual(ph.heartbeat_key(host='vps1', pid=42), 'proc:vps1:42')
        self.assertTrue(ph.heartbeat_key().startswith('proc:'))
        self.assertTrue(ph.heartbeat_key().endswith(f':{os.getpid()}'))

    def test_rss_of_this_process_is_positive(self):
        v = ph.rss_mb()
        self.assertIsNotNone(v)
        self.assertGreater(v, 0.0)

    def test_rss_of_a_dead_pid_is_none(self):
        self.assertIsNone(ph.rss_mb(999999))

    def test_default_ttl_is_180(self):
        self.assertEqual(ph.DEFAULT_TTL_S, 180)


class TestWrite(unittest.TestCase):
    def test_writes_every_field_and_sets_the_ttl(self):
        r = FakeRedis()
        key = ph.write(r, step='signals', argv=['python3', 'engine.py', '--date', '2026-09-14'],
                       pid=42, host='vps1', started_at='2026-09-14T10:00:00+00:00')
        self.assertEqual(key, 'proc:vps1:42')
        h = r.hashes[key]
        self.assertEqual(h['step'], 'signals')
        self.assertEqual(h['argv'], 'python3 engine.py --date 2026-09-14')
        self.assertEqual(h['host'], 'vps1')
        self.assertEqual(h['pid'], '42')
        self.assertEqual(h['started_at'], '2026-09-14T10:00:00+00:00')
        self.assertIn('updated_at', h)
        self.assertIn('rss_mb', h)
        self.assertTrue(all(isinstance(v, str) for v in h.values()), h)
        self.assertEqual(r.ttls[key], 180)

    def test_ttl_is_overridable_for_long_blocking_children(self):
        r = FakeRedis()
        key = ph.write(r, step='fleet:S_x', pid=7, host='vps1', ttl_s=3900)
        self.assertEqual(r.ttls[key], 3900)

    def test_never_raises_when_redis_is_down(self):
        self.assertIsNone(ph.write(ExplodingRedis(), step='signals', pid=1, host='h'))
        ph.clear(ExplodingRedis(), pid=1, host='h')      # must not raise

    def test_clear_removes_the_key(self):
        r = FakeRedis()
        ph.write(r, step='signals', pid=42, host='vps1')
        ph.clear(r, pid=42, host='vps1')
        self.assertNotIn('proc:vps1:42', r.hashes)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run it — expect failure**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest -q tests/lib/test_proc_heartbeat.py
```

Expected: `ModuleNotFoundError: No module named 'lib.proc_heartbeat'`.

- [ ] **Step 3: Write `src/lib/proc_heartbeat.py`**

```python
"""proc_heartbeat.py — per-process identity in Redis (QD spec §5 E3).

`proc:<host>:<pid>` is a hash of {argv, step, rss_mb, started_at, updated_at,
host, pid} with a short TTL, so "what is eating this 8 GB box right now" is
answerable from Redis instead of from `ps` plus guesswork. The TTL is the
liveness signal: a process that dies stops refreshing and its entry expires.

NEVER raises and NEVER blocks a caller: a heartbeat is diagnostics, and a
Redis hiccup must not take down a pipeline step. Every public function
swallows its exceptions and returns None.

No thread, no timer (spec §0: no always-on threads). Callers write from a
loop they already run.
"""
from __future__ import annotations

import os
import socket
from datetime import datetime, timezone

KEY_PREFIX    = 'proc'
DEFAULT_TTL_S = 180


def heartbeat_key(host=None, pid=None) -> str:
    host = host or socket.gethostname()
    pid = os.getpid() if pid is None else int(pid)
    return f'{KEY_PREFIX}:{host}:{pid}'


def rss_mb(pid=None):
    """Resident set size in MiB from /proc/<pid>/statm, or None."""
    pid = os.getpid() if pid is None else int(pid)
    try:
        with open(f'/proc/{pid}/statm', 'r') as fh:
            resident_pages = int(fh.read().split()[1])
        return round(resident_pages * os.sysconf('SC_PAGE_SIZE') / (1024 * 1024), 1)
    except Exception:
        return None


def write(r, *, step, argv=None, pid=None, host=None, started_at=None,
          ttl_s=DEFAULT_TTL_S):
    """Upsert this process's heartbeat hash. Returns the key, or None."""
    if r is None:
        return None
    key = heartbeat_key(host=host, pid=pid)
    now = datetime.now(timezone.utc).isoformat()
    resident = rss_mb(pid)
    fields = {
        'host':       host or socket.gethostname(),
        'pid':        str(os.getpid() if pid is None else int(pid)),
        'step':       str(step or ''),
        'argv':       ' '.join(str(a) for a in (argv or []))[:500],
        'rss_mb':     '' if resident is None else str(resident),
        'started_at': str(started_at or now),
        'updated_at': now,
    }
    try:
        r.hset(key, mapping=fields)
        r.expire(key, int(ttl_s))
        return key
    except Exception:
        return None


def clear(r, *, pid=None, host=None) -> None:
    if r is None:
        return
    try:
        r.delete(heartbeat_key(host=host, pid=pid))
    except Exception:
        pass
```

- [ ] **Step 4: Run — expect PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest -q tests/lib/test_proc_heartbeat.py
```

Expected: `8 passed`. (The registry test in Step 11 brings the file to 9.)

- [ ] **Step 5: Write the failing JS test** — create `tests/lib/proc_heartbeat.test.js`:

```javascript
'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const ph   = require(path.join(ROOT, 'src/lib/proc_heartbeat.js'));

class FakeRedis {
  constructor() { this.hashes = {}; this.ttls = {}; }
  async hset(key, obj) { this.hashes[key] = { ...(this.hashes[key] || {}), ...obj }; return 1; }
  async expire(key, ttl) { this.ttls[key] = ttl; return 1; }
  async del(key) { delete this.hashes[key]; delete this.ttls[key]; return 1; }
}

class ExplodingRedis {
  async hset() { throw new Error('redis down'); }
  async expire() { throw new Error('redis down'); }
  async del() { throw new Error('redis down'); }
}

test('key shape matches the Python twin', () => {
  assert.equal(ph.heartbeatKey({ host: 'vps1', pid: 42 }), 'proc:vps1:42');
  assert.ok(ph.heartbeatKey().endsWith(`:${process.pid}`));
  assert.equal(ph.DEFAULT_TTL_SEC, 180);
});

test('rssMb is positive for this process and null for a dead pid', () => {
  assert.ok(ph.rssMb() > 0);
  assert.equal(ph.rssMb(999999), null);
});

test('writeHeartbeat stores every field as a string and sets the TTL', async () => {
  const r = new FakeRedis();
  const key = await ph.writeHeartbeat(r, {
    step: 'fleet:S_x', argv: ['python3', '-m', 'backtest.unified_backtest'],
    pid: 42, host: 'vps1', startedAt: '2026-09-14T10:00:00.000Z', ttlSec: 3900,
  });
  assert.equal(key, 'proc:vps1:42');
  const h = r.hashes[key];
  assert.equal(h.step, 'fleet:S_x');
  assert.equal(h.argv, 'python3 -m backtest.unified_backtest');
  assert.equal(h.host, 'vps1');
  assert.equal(h.pid, '42');
  assert.equal(h.started_at, '2026-09-14T10:00:00.000Z');
  assert.ok(h.updated_at);
  assert.ok('rss_mb' in h);
  assert.ok(Object.values(h).every(v => typeof v === 'string'));
  assert.equal(r.ttls[key], 3900);
});

test('writeHeartbeat never throws when redis is down', async () => {
  assert.equal(await ph.writeHeartbeat(new ExplodingRedis(), { step: 's', pid: 1, host: 'h' }), null);
  await ph.clearHeartbeat(new ExplodingRedis(), { pid: 1, host: 'h' });   // must not throw
  assert.equal(await ph.writeHeartbeat(null, { step: 's' }), null);
});
```

- [ ] **Step 6: Write `src/lib/proc_heartbeat.js`**

```javascript
'use strict';
/**
 * proc_heartbeat.js — JS twin of src/lib/proc_heartbeat.py (QD spec §5 E3).
 *
 * Same key (`proc:<host>:<pid>`), same seven string fields, same TTL default,
 * so `proc_registry` and the doctor read one shape regardless of which runtime
 * wrote the entry. Never throws: diagnostics must not break a caller.
 */
const fs = require('node:fs');
const os = require('node:os');

const KEY_PREFIX      = 'proc';
const DEFAULT_TTL_SEC = 180;

function heartbeatKey({ host, pid } = {}) {
  const h = host || os.hostname();
  const p = pid == null ? process.pid : Number(pid);
  return `${KEY_PREFIX}:${h}:${p}`;
}

function rssMb(pid) {
  const p = pid == null ? process.pid : Number(pid);
  try {
    const residentPages = Number(fs.readFileSync(`/proc/${p}/statm`, 'utf8').split(/\s+/)[1]);
    if (!Number.isFinite(residentPages)) return null;
    return Math.round((residentPages * 4096) / (1024 * 1024) * 10) / 10;
  } catch (_) {
    return null;
  }
}

async function writeHeartbeat(r, { step, argv, pid, host, startedAt, ttlSec } = {}) {
  if (!r) return null;
  const key = heartbeatKey({ host, pid });
  const now = new Date().toISOString();
  const resident = rssMb(pid);
  const fields = {
    host:       String(host || os.hostname()),
    pid:        String(pid == null ? process.pid : Number(pid)),
    step:       String(step || ''),
    argv:       (argv || []).map(String).join(' ').slice(0, 500),
    rss_mb:     resident == null ? '' : String(resident),
    started_at: String(startedAt || now),
    updated_at: now,
  };
  try {
    await r.hset(key, fields);
    await r.expire(key, Math.trunc(Number(ttlSec) || DEFAULT_TTL_SEC));
    return key;
  } catch (_) {
    return null;
  }
}

async function clearHeartbeat(r, { pid, host } = {}) {
  if (!r) return;
  try { await r.del(heartbeatKey({ host, pid })); } catch (_) { /* diagnostics only */ }
}

module.exports = { KEY_PREFIX, DEFAULT_TTL_SEC, heartbeatKey, rssMb, writeHeartbeat, clearHeartbeat };
```

- [ ] **Step 7: Run — expect PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/lib/proc_heartbeat.test.js
```

Expected: `# pass 4`.

- [ ] **Step 8: Call site 1 — the orchestrator poll loop.** Extend the Task-2 signature once more:

```python
def run_step(script, run_date, env, renew=None, heartbeat=None):
```

where `heartbeat` is `Callable[[int, str], None]` taking `(child_pid, started_at_iso)`. Capture the start stamp ONCE, immediately after `_resolve_script` (beside the `renew` block from Task 2 Step 5), so `started_at` is a real start time rather than a copy of `updated_at`:

```python
    _hb_started = datetime.now(timezone.utc).isoformat()
```

Then inside the `while True:` poll loop (`:618-640`), in the `except subprocess.TimeoutExpired:` branch — i.e. every 30 s tick — add the call, and add one immediately after the Popen:

```python
        # QD E3: declare who we are, every 30s tick, from the loop that
        # already exists. No timer thread (spec §0).
        if heartbeat is not None:
            try: heartbeat(proc.pid, _hb_started)
            except Exception: pass
```

(once right after `last_output_ts = [time.time()]`, and once inside the `except subprocess.TimeoutExpired:` branch, replacing the bare `pass`).

In `main()`, pass it alongside `renew` at BOTH `run_step` call sites:

```python
            def _hb(child_pid, started_at, _k=step_key, _s=script):
                from lib import proc_heartbeat as _ph
                _ph.write(r, step=_k, argv=[_s], pid=child_pid,
                          started_at=started_at, ttl_s=_ph.DEFAULT_TTL_S)

            ok, rc = run_step(script, run_date, step_env,
                              renew=lambda t: renew_lock(r, run_date, _run_lock.ttl_for(t)),
                              heartbeat=_hb)
```

Both loop variables are bound as defaults (`_k`, `_s`) because the closure outlives the loop iteration.

- [ ] **Step 9: Call site 2 — the fleet driver.** In `scripts/refresh_backtests_resumable.js`, the per-strategy loop already lives inside the `(async () => {` IIFE at `:143`, so an `await` before the blocking `spawnSync` is safe. After the `require` block at `:48` add:

```javascript
const { writeHeartbeat, clearHeartbeat } = require(path.join(__dirname, '..', 'src/lib/proc_heartbeat.js'));
const Redis = require('ioredis');
const _hbRedis = new Redis(process.env.REDIS_URL || 'redis://localhost:6379',
                           { lazyConnect: true, maxRetriesPerRequest: 1 });
_hbRedis.on('error', () => { /* heartbeats are diagnostics; never fail the fleet */ });
```

and immediately before `const r = spawnSync('bash', [...])` at `:176`:

```javascript
    // QD E3: one heartbeat per child, written BEFORE the blocking spawnSync.
    // The driver blocks for up to PER_TIMEOUT_S, so a 60s cadence is
    // impossible here — the TTL covers the whole child instead.
    await writeHeartbeat(_hbRedis, {
      step: `fleet:${sid}`,
      argv: ['python3', '-m', 'backtest.unified_backtest', '--strategy-id', sid],
      ttlSec: PER_TIMEOUT_S + 300,
    });
```

and after the loop (before the final summary at `:214`):

```javascript
  await clearHeartbeat(_hbRedis);
  _hbRedis.disconnect();
```

- [ ] **Step 10: Call site 3 — the premarket scan.** In `src/pipeline/run_premarket_scan.py`, as the first statement inside `main(argv)` (`:304`):

```python
    # QD E3: this unit is spawned per instance (openclaw-premarket-scan@%i)
    # and is a routine co-tenant of the 8 GB box — declare it.
    try:
        import redis as _redis
        from lib import proc_heartbeat as _ph
        _ph.write(_redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379'),
                                  decode_responses=True),
                  step='premarket_scan', argv=sys.argv, ttl_s=900)
    except Exception:
        pass
```

- [ ] **Step 11: Write the failing check test** — append to `tests/lib/test_proc_heartbeat.py`:

```python
class TestProcRegistryCheck(unittest.TestCase):
    """The system check reads the same keys the writers produce."""

    def test_check_is_registered_with_the_agents_tag(self):
        from system_checks import all_checks  # noqa: E402 (imports checks/ as a side effect)
        reg = all_checks()
        self.assertIn('proc_registry', reg)
        self.assertIn('agents', reg['proc_registry']['tags'])
        self.assertEqual(reg['proc_registry']['requires'], ['fs'])
```

- [ ] **Step 12: Add the `proc_registry` check.** Append to `src/system_checks/checks/agents.py`:

```python
@check(name='proc_registry', tags=['agents'], requires=['fs'])
def _proc_registry():
    """List the live `proc:<host>:<pid>` heartbeats (QD E3).

    Diagnostic, not a gate: PASS with the roster, WARN only when an entry
    names a process that is no longer alive on this host (a writer that
    died between refreshes leaves a ghost until its TTL expires — worth
    seeing, never worth failing a maintenance run over).
    """
    import socket
    try:
        import redis as _redis
        r = _redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379'),
                            socket_connect_timeout=2, decode_responses=True)
        keys = sorted(r.scan_iter('proc:*', count=100))
    except Exception as e:
        return Status.SKIP, f'redis unreachable ({type(e).__name__}) — no proc registry'
    if not keys:
        return Status.PASS, 'no live process heartbeats'
    me = socket.gethostname()
    lines, ghosts = [], []
    for k in keys[:20]:
        h = r.hgetall(k) or {}
        lines.append(f"{h.get('step') or '?'}@{h.get('pid') or '?'}"
                     f"({h.get('rss_mb') or '?'}MB)")
        if h.get('host') == me:
            try:
                os.kill(int(h.get('pid', 0)), 0)
            except PermissionError:
                pass
            except Exception:
                ghosts.append(k)
    detail = f'{len(keys)} live: ' + ', '.join(lines)
    if ghosts:
        return Status.WARN, (detail + f' | {len(ghosts)} ghost entr'
                             f'{"y" if len(ghosts) == 1 else "ies"}: {", ".join(ghosts[:3])}')[:200]
    return Status.PASS, detail[:200]
```

- [ ] **Step 13: Fix `check_orchestrator_lock` and add `check_co_tenant_memory` in `src/maintenance/doctor.py`.** Replace `:829-830`:

```python
        # The orchestrator stores locks under `pipeline:lock:<run_date>`.
        keys = list(r.scan_iter('pipeline:lock:*', count=20))
```

with:

```python
        # QD E1: the shared key is `pipeline:run_lock:<run_date>`
        # (src/lib/run_lock_key.json). This scan used to look for
        # `pipeline:lock:*`, which matched NEITHER the old Python key
        # (`pipeline:running:*`) nor the JS one — so the check has been
        # reporting "no locks held" unconditionally since it was written.
        keys = list(r.scan_iter('pipeline:run_lock:*', count=20))
```

and change the per-key report line (`:842`) to include the owner, which the value now carries:

```python
        val = r.get(k)
        if isinstance(val, bytes):
            val = val.decode('utf-8', 'replace')
        stale.append(f'{k.decode() if isinstance(k, bytes) else k} (ttl={ttl}s, owner={val})')
```

Then append a new check next to it:

```python
CO_TENANT_RSS_MB = 1024


@_check('co_tenant_memory')
def check_co_tenant_memory():
    """Name every python/node co-tenant over 1 GB RSS, by argv (QD E3).

    Pure /proc read — no DB, no Redis, no `ps` shell-out. On an 8 GB no-swap
    box the question at 3am is always "who else is resident right now"; this
    answers it in one line of the digest."""
    import glob
    page = os.sysconf('SC_PAGE_SIZE')
    big = []
    for statm in glob.glob('/proc/[0-9]*/statm'):
        pid = statm.split('/')[2]
        try:
            with open(statm) as fh:
                rss = int(fh.read().split()[1]) * page / (1024 * 1024)
            if rss < CO_TENANT_RSS_MB:
                continue
            with open(f'/proc/{pid}/cmdline', 'rb') as fh:
                argv = fh.read().replace(b'\x00', b' ').decode('utf-8', 'replace').strip()
        except Exception:
            continue
        if not argv:
            continue
        big.append((round(rss), pid, argv[:90]))
    if not big:
        return _ok('co_tenant_memory', f'no process over {CO_TENANT_RSS_MB} MB RSS')
    big.sort(reverse=True)
    detail = '; '.join(f'{mb}MB pid={pid} {argv}' for mb, pid, argv in big[:5])
    total = sum(mb for mb, _p, _a in big)
    return _warn('co_tenant_memory', f'{len(big)} co-tenant(s), {total}MB total — {detail}'[:400])
```

- [ ] **Step 14: Run the tests — expect PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest -q tests/lib/test_proc_heartbeat.py tests/system_checks/test_system_checks_framework.py tests/maintenance
```

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/lib/proc_heartbeat.test.js
```

Expected: all pass. Then a read-only live smoke (safe — it starts nothing):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m system_checks --check proc_registry
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 src/maintenance/doctor.py --json | python3 -c "import json,sys; d=json.load(sys.stdin); print([c for c in d['checks'] if c['name'] in ('co_tenant_memory','orchestrator_lock')])"
```

- [ ] **Step 15: Commit**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/lib/proc_heartbeat.py src/lib/proc_heartbeat.js tests/lib/test_proc_heartbeat.py tests/lib/proc_heartbeat.test.js src/execution/pipeline_orchestrator.py src/pipeline/run_premarket_scan.py scripts/refresh_backtests_resumable.js src/system_checks/checks/agents.py src/maintenance/doctor.py
git commit -m "feat(ops): per-process heartbeat + proc_registry check + co-tenant doctor line (QD E3)

proc:<host>:<pid> hashes (argv, step, rss_mb, started_at, updated_at) written
by the orchestrator step runner (from its existing 30s poll — no new thread),
the fleet children (one pre-spawn write, TTL = per-timeout + 300s) and the
premarket scan. New proc_registry system check lists them; new doctor
co_tenant_memory names any process over 1GB RSS by argv.

Also fixes check_orchestrator_lock, which scanned pipeline:lock:* — a pattern
matching neither the old nor the new key — and so always said 'no locks held'.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

<!-- END TASK 7 -->

---

### Task 8 — E3b: port the stdout-idle wedge detector to `runSubprocess`

`pipeline_orchestrator.run_step` has carried a stdout-idle watchdog since the 2026-04-29 wedge (`:592-633`: 30 s poll grain, `STEP_STDOUT_IDLE_MAX_S` default 600, SIGTERM then SIGKILL, distinct `rc=-2`). `daily_cycle_helpers.runSubprocess` has only a wall-clock timeout (`:41-47`) — a step that produces no output but never exits burns the full `timeoutSec` (up to 9000 s for collect) instead of being killed in 10 minutes. This ports the detector, mapping the wedge to a distinct rc so the abort alert can name it.

**Files:**
- Modify: `src/agent/graphs/daily_cycle_helpers.js` (`runSubprocess`, `_RC_HINTS`)
- Create: `tests/test_daily_cycle_idle_watchdog.test.js`

**Interfaces:**
- Consumes: `STEP_STDOUT_IDLE_MAX_S` (same env var the Python twin reads, `pipeline_orchestrator.py:598`; default `600`), optional `opts.stdoutIdleMaxSec` override.
- Produces: `runSubprocess(...)` return gains `wedged: boolean`; a wedge resolves `rc: 125` (distinct from `124` wall-clock timeout and `137` SIGKILL/OOM) and `stderrTail` gains a `[wedge]` line. `_RC_HINTS` (`:81-85`) gains `125: 'stdout idle — wedge detected (rc=125)'` so `formatAbortAlert` (`:87`) names it.

**Why rc=125 and not the Python twin's -2:** `runSubprocess` already normalises signals into positive rc values (`rc = timedOut ? 124 : (code === null ? (signal ? 137 : 1) : code)`), and `_RC_HINTS` is keyed by positive rc. 125 is unused by every step script in the cycle and adjacent to 124, which is what the operator will be reading it next to.

- [ ] **Step 1: Write the failing test** — create `tests/test_daily_cycle_idle_watchdog.test.js`:

```javascript
'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT    = path.resolve(__dirname, '..');
const helpers = require(path.join(ROOT, 'src/agent/graphs/daily_cycle_helpers.js'));

test('a silent child is killed by the stdout-idle watchdog, not the wall clock', async () => {
  const t0 = Date.now();
  // Emits once, then goes quiet for far longer than the idle budget.
  const out = await helpers.runSubprocess(
    ['sh', '-c', 'echo alive; sleep 30'],
    { timeoutSec: 25, stdoutIdleMaxSec: 2, env: process.env });
  const elapsed = (Date.now() - t0) / 1000;
  assert.equal(out.wedged, true);
  assert.equal(out.rc, 125);
  assert.equal(out.timedOut, false);
  assert.ok(elapsed < 20, `killed at ${elapsed}s — should be ~idle budget, not the wall clock`);
  assert.match(out.stderrTail || '', /\[wedge\]/);
});

test('a chatty child is never wedged even past the idle budget', async () => {
  const out = await helpers.runSubprocess(
    ['sh', '-c', 'for i in 1 2 3 4 5 6; do echo tick; sleep 0.5; done'],
    { timeoutSec: 20, stdoutIdleMaxSec: 2, env: process.env });
  assert.equal(out.rc, 0);
  assert.equal(out.wedged, false);
  assert.match(out.stdout || '', /tick/);
});

test('stderr output also counts as liveness', async () => {
  const out = await helpers.runSubprocess(
    ['sh', '-c', 'for i in 1 2 3 4 5 6; do echo tick >&2; sleep 0.5; done'],
    { timeoutSec: 20, stdoutIdleMaxSec: 2, env: process.env });
  assert.equal(out.rc, 0);
  assert.equal(out.wedged, false);
});

test('the wall-clock timeout still wins when it fires first', async () => {
  const out = await helpers.runSubprocess(
    ['sh', '-c', 'sleep 10'],
    { timeoutSec: 1, stdoutIdleMaxSec: 30, env: process.env });
  assert.equal(out.timedOut, true);
  assert.equal(out.rc, 124);
  assert.equal(out.wedged, false);
});

test('the idle budget defaults to STEP_STDOUT_IDLE_MAX_S, matching the Python twin', () => {
  const old = process.env.STEP_STDOUT_IDLE_MAX_S;
  delete process.env.STEP_STDOUT_IDLE_MAX_S;
  try {
    assert.equal(helpers.stdoutIdleMaxSec(), 600);
    process.env.STEP_STDOUT_IDLE_MAX_S = '90';
    assert.equal(helpers.stdoutIdleMaxSec(), 90);
  } finally {
    if (old === undefined) delete process.env.STEP_STDOUT_IDLE_MAX_S;
    else process.env.STEP_STDOUT_IDLE_MAX_S = old;
  }
});

test('formatAbortAlert names the wedge rc', () => {
  const msg = helpers.formatAbortAlert({
    runDate: '2026-09-14', runId: 'r1', abortedAt: 'collect',
    lastError: { rc: 125, message: 'wedged' }, reason: 'scheduled',
  });
  assert.match(msg, /wedge/i);
});
```

- [ ] **Step 2: Run it — expect failure**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/test_daily_cycle_idle_watchdog.test.js
```

Expected: `helpers.stdoutIdleMaxSec is not a function`, plus `out.wedged` undefined.

- [ ] **Step 3: Implement.** In `src/agent/graphs/daily_cycle_helpers.js`, add next to `stepMemoryMax` (Task 5 Step 3):

```javascript
// QD E3 (2026-09-12): the stdout-idle wedge detector, ported from
// pipeline_orchestrator.run_step (:592-633). The 2026-04-29 cycle sat in
// collector Phase 3 for 30+ minutes with zero output on a half-open TCP
// stream. The Python runner has caught that class since; the JS runner only
// had a wall-clock timeout, so the same wedge would burn the full 9000s
// collect budget. Same env var, same default, so the twins agree.
const DEFAULT_STDOUT_IDLE_MAX_SEC = 600;

function stdoutIdleMaxSec() {
  const v = parseInt(process.env.STEP_STDOUT_IDLE_MAX_S, 10);
  return Number.isFinite(v) && v > 0 ? v : DEFAULT_STDOUT_IDLE_MAX_SEC;
}
```

Replace the body of `runSubprocess` (the whole function as amended in Task 5) with:

```javascript
function runSubprocess(argv, { timeoutSec = 600, env = process.env, cwd,
                               memoryMax, stdoutIdleMaxSec: idleOverride } = {}) {
  return new Promise((resolve) => {
    const startedAt = Date.now();
    const cap = (memoryMax === undefined) ? stepMemoryMax() : memoryMax;
    const wrapped = wrapCapped(argv[0], argv.slice(1), { memoryMax: cap });
    const idleMax = (idleOverride === undefined) ? stdoutIdleMaxSec() : Number(idleOverride);
    let stdout = '';
    let stderr = '';
    let timedOut = false;
    let wedged = false;
    let lastOutputAt = Date.now();

    const proc = spawn(wrapped.cmd, wrapped.args, {
      env,
      cwd: cwd || process.cwd(),
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    // ANY output — stdout or stderr — counts as liveness. The Python twin
    // merges stderr into stdout (stderr=subprocess.STDOUT), so this matches.
    proc.stdout.on('data', (b) => { lastOutputAt = Date.now(); stdout += b.toString(); });
    proc.stderr.on('data', (b) => { lastOutputAt = Date.now(); stderr += b.toString(); });

    const hardKill = () => setTimeout(() => { try { proc.kill('SIGKILL'); } catch {} }, 5000);

    const timer = setTimeout(() => {
      timedOut = true;
      try { proc.kill('SIGTERM'); } catch {}
      hardKill();
    }, timeoutSec * 1000);

    // Poll on a 5s grain: fine enough to honour a short idle budget in tests,
    // negligible on a 9000s collect. Cleared in the same place as `timer`.
    const idleTimer = setInterval(() => {
      if (timedOut || wedged) return;
      const idleSec = (Date.now() - lastOutputAt) / 1000;
      if (idleSec <= idleMax) return;
      wedged = true;
      stderr += `\n[wedge] stdout idle ${Math.round(idleSec)}s > ${idleMax}s — SIGTERM\n`;
      try { proc.kill('SIGTERM'); } catch {}
      hardKill();
    }, 5000);
    if (typeof idleTimer.unref === 'function') idleTimer.unref();

    proc.on('close', (code, signal) => {
      clearTimeout(timer);
      clearInterval(idleTimer);
      const durationMs = Date.now() - startedAt;
      const rc = wedged   ? 125
               : timedOut ? 124
               : (code === null ? (signal ? 137 : 1) : code);
      resolve({
        rc,
        stdout,
        stderrTail: stderr.slice(-4000),
        durationMs,
        timedOut,
        wedged,
        memoryMax: wrapped.memoryMax,
      });
    });

    proc.on('error', (e) => {
      clearTimeout(timer);
      clearInterval(idleTimer);
      resolve({
        rc: 127,
        stdout: '',
        stderrTail: `spawn failed: ${e.message}`,
        durationMs: Date.now() - startedAt,
        timedOut: false,
        wedged: false,
        memoryMax: wrapped.memoryMax,
      });
    });
  });
}
```

Add the hint (`:81-85`):

```javascript
const _RC_HINTS = {
  137: 'SIGKILL — almost always OOM (rc=137)',
  139: 'SIGSEGV (rc=139)',
  125: 'stdout idle — wedge detected and SIGTERMed (rc=125)',
  124: 'timed out (rc=124)',
};
```

and export it (`:183-186`):

```javascript
module.exports = {
  skipForSubset, strictMode, runSubprocess, stepMemoryMax, stdoutIdleMaxSec,
  formatAbortAlert, postAbortAlert,
};
```

- [ ] **Step 4: Run the tests — expect PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/test_daily_cycle_idle_watchdog.test.js tests/test_daily_cycle_helpers.test.js tests/test_daily_cycle_capped_spawn.test.js tests/test_daily_cycle_abort_alert.test.js tests/test_daily_cycle_node.test.js
```

Expected: all pass. The four pre-existing `runSubprocess` tests are unaffected — their children either finish fast or are killed by the wall clock, and the default 600 s idle budget never fires.

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/agent/graphs/daily_cycle_helpers.js tests/test_daily_cycle_idle_watchdog.test.js
git commit -m "feat(cycle): port the stdout-idle wedge detector to runSubprocess (QD E3)

The JS step runner had only a wall-clock timeout, so a silent-but-alive child
burned the full budget (9000s for collect). Now it mirrors
pipeline_orchestrator.run_step: same STEP_STDOUT_IDLE_MAX_S env var, same 600s
default, SIGTERM then SIGKILL, and a distinct rc=125 that formatAbortAlert
names so a wedge is never mistaken for a timeout or an OOM.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

<!-- END TASK 8 -->

---

### Task 9 — E4: AST import allowlist for LLM-written strategies

**This is a guardrail, not a sandbox.** It stops an LLM-written strategy file from reaching the network or the filesystem *by accident or by prompt drift*; it does not isolate anything. Root `backtest` is allowlisted (26 fleet files import `backtest.quick_backtest`) and transitively reaches everything in the repo. Say so in the module docstring so nobody mistakes it for security.

**The allowlist is the verified census, not the spec's list.** An AST walk over all 156 files in `src/strategies/implementations/` on 2026-09-13 found exactly 27 absolute roots plus one relative form. The spec's list omits twelve of them (`src`, `backtest`, `json`, `traceback`, `pyarrow`, `lib`, `base`, `_extra_panels`, `enum`, `statistics`, `sklearn` sub-roots, relative imports) and *rejects* two that 137 and 29 files respectively use legitimately (`sys`, `os`). Resolution, adopted here: **the module is allowed, the dangerous attributes are not.** `strategycoder.md:113` MANDATES `print(f'[debug] signals={len(signals)}', file=sys.stderr)` on every strategy — a lint that rejects `sys` would reject the fleet and every future candidate the prompt produces.

Verified census (count = files-with-that-root; all 27 go in the allowlist):

```
strategies 179 · typing 151 · __future__ 148 · sys 137 · pandas 118 · numpy 83
os 29 · src 28 · backtest 28 · json 18 · sklearn 9 · _extra_panels 8 · pathlib 7
scipy 7 · traceback 6 · math 5 · datetime 5 · itertools 3 · pyarrow 3 · base 2
lib 2 · statsmodels 2 · functools 1 · logging 1 · dataclasses 1 · enum 1 · statistics 1
relative (level>0): ..base ×6, ._greeks_filter ×1
```

Attribute policy (also census-derived — these are the ONLY `os.`/`sys.` attributes the fleet touches): `os.{environ, path, getenv, sep, pathsep, linesep, name}` and `sys.{stderr, stdout, path, exit, argv, version_info, maxsize, platform}`. Anything else on those two modules is a violation — **including via `from os import system`**, which binds the name locally and would otherwise never appear as an `ast.Attribute`. The `ImportFrom` branch applies the same allowlist to the imported names when the module IS the bare root (`from os.path import join` keeps root-level semantics and stays clean). Verified safe against the fleet: the census found `import os` / `import sys` only — zero `from os` / `from sys` lines.

Banned calls: bare `open()`, `eval()`, `exec()`, `compile()`, `__import__()`; and the method names `write_text, write_bytes, unlink, rmdir, mkdir, chmod, symlink_to, hardlink_to, touch, to_csv, to_parquet, to_pickle, to_hdf, to_sql, system, popen, remove, makedirs, urlopen, check_output, Popen`. **`rename` is deliberately NOT banned** — an AST scan found 4 uses and all four are `DataFrame.rename`; a name-based ban there would reject a legitimate strategy. Verified: zero fleet hits for every name that IS banned.

**Files:**
- Create: `src/strategies/strategy_lint.py`
- Create: `tests/strategies/test_strategy_lint.py`
- Modify: `src/strategies/validate_strategy.py` (the lint hook goes at `:70`, before `importlib.import_module` at `:80`)
- Modify: `src/agent/research/research-orchestrator.js` (before `_validateFn` at `:1299`)
- Modify: `src/agent/prompts/subagents/strategycoder.md` (the rules list at `:110-118`)

**Interfaces:**
- Produces (`src/strategies/strategy_lint.py`):
  - `ALLOWED_ROOTS: frozenset[str]` (the 27 above)
  - `ALLOWED_ATTRS: dict[str, frozenset[str]]` (`os`, `sys`)
  - `BANNED_CALLS: frozenset[str]`, `BANNED_METHODS: frozenset[str]`
  - `Violation = namedtuple('Violation', 'line kind detail')`
  - `lint_source(source: str, filename: str = '<candidate>') -> list[Violation]`
  - `lint_file(path: str) -> list[Violation]`
  - `format_violations(violations) -> list[str]`
  - CLI: `python3 src/strategies/strategy_lint.py <file> [<file>…]` → JSON `{"ok": bool, "violations": [{"file","line","kind","detail"}]}`, exit 0/1
- Consumed by: `validate_strategy.validate()` (returns the existing `{'ok': False, 'errors': [...]}` shape) and `research-orchestrator._runGateChain` (new `reasonCode: 'import_violation'`).

**Two invocation sites, two distinct jobs:**
1. `validate_strategy.py` — **authoritative**. It runs before `importlib.import_module` at `:80`, and `unified_backtest.load_strategy_class` (`:200`) calls `validate(filepath)` at `:202` first, so every import path in the system is covered by this one hook.
2. `research-orchestrator.js` — **pre-flight**, so a violating candidate never pays for the 60 s python spawn, and the rejection is attributed as `import_violation` rather than `contract_violation`.

- [ ] **Step 1: Write the failing test** — create `tests/strategies/test_strategy_lint.py`:

```python
"""tests/strategies/test_strategy_lint.py — AST import allowlist (QD §5 E4).

Pure source-string linting: nothing is imported, nothing is executed, no DB.
The last test lints all 156 live fleet files and asserts zero violations.
"""
from __future__ import annotations

import glob
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import strategy_lint as sl  # noqa: E402

OK_HEAD = (
    'from __future__ import annotations\n'
    'import sys\n'
    'import pandas as pd\n'
    'import numpy as np\n'
    'from typing import List\n'
    'from strategies.base import BaseStrategy, Signal\n'
)


class TestAllowed(unittest.TestCase):
    def test_a_typical_strategy_header_is_clean(self):
        self.assertEqual(sl.lint_source(OK_HEAD), [])

    def test_every_census_root_is_allowed(self):
        for root in ['strategies', 'typing', '__future__', 'sys', 'pandas', 'numpy',
                     'os', 'src', 'backtest', 'json', 'sklearn', '_extra_panels',
                     'pathlib', 'scipy', 'traceback', 'math', 'datetime', 'itertools',
                     'pyarrow', 'base', 'lib', 'statsmodels', 'functools', 'logging',
                     'dataclasses', 'enum', 'statistics']:
            self.assertEqual(sl.lint_source(f'import {root}\n'), [], root)
            self.assertEqual(sl.lint_source(f'from {root} import x\n'), [], root)

    def test_relative_imports_are_allowed(self):
        self.assertEqual(sl.lint_source('from ..base import BaseStrategy\n'), [])
        self.assertEqual(sl.lint_source('from ._greeks_filter import f\n'), [])

    def test_the_mandated_stderr_debug_line_is_allowed(self):
        src = OK_HEAD + "print('[debug] signals=0', file=sys.stderr)\n"
        self.assertEqual(sl.lint_source(src), [])

    def test_the_sys_path_and_os_path_idioms_are_allowed(self):
        src = ("import os, sys\n"
               "sys.path.insert(0, 'src/strategies')\n"
               "p = os.path.join(os.path.dirname(__file__), 'x')\n"
               "v = os.environ.get('OPENCLAW_X', '0')\n")
        self.assertEqual(sl.lint_source(src), [])

    def test_dotted_allowed_roots_are_allowed(self):
        self.assertEqual(sl.lint_source('from statsmodels.tsa.stattools import coint\n'), [])
        self.assertEqual(sl.lint_source('from src.strategies.universe_default import sp500\n'), [])
        self.assertEqual(sl.lint_source('from backtest.quick_backtest import run\n'), [])


class TestRejected(unittest.TestCase):
    def _kinds(self, src):
        return sorted({v.kind for v in sl.lint_source(src)})

    def test_network_and_process_roots_are_rejected(self):
        for root in ['subprocess', 'socket', 'requests', 'urllib', 'http',
                     'shutil', 'importlib', 'ctypes', 'pickle', 'multiprocessing']:
            vs = sl.lint_source(f'import {root}\n')
            self.assertEqual(len(vs), 1, root)
            self.assertEqual(vs[0].kind, 'import')
            self.assertIn(root, vs[0].detail)

    def test_from_import_of_a_rejected_root_is_caught(self):
        vs = sl.lint_source('from urllib.request import urlopen\n')
        self.assertEqual([v.kind for v in vs], ['import'])
        self.assertEqual(vs[0].line, 1)

    def test_from_os_import_system_does_not_bypass_the_attribute_policy(self):
        # The name is bound locally, so it never appears as an ast.Attribute —
        # the ImportFrom branch has to apply the same allowlist.
        for stmt, bad in [('from os import system', 'system'),
                          ('from os import remove, path', 'remove'),
                          ('from sys import modules', 'modules')]:
            vs = sl.lint_source(stmt + '\n')
            self.assertTrue(any(v.kind == 'import' and bad in v.detail for v in vs), stmt)

    def test_from_os_import_of_a_permitted_name_is_clean(self):
        self.assertEqual(sl.lint_source('from os import environ, path\n'), [])
        self.assertEqual(sl.lint_source('from sys import stderr\n'), [])
        # A dotted submodule of an allowed root keeps root-level semantics.
        self.assertEqual(sl.lint_source('from os.path import join\n'), [])

    def test_dangerous_os_and_sys_attributes_are_rejected(self):
        self.assertEqual(self._kinds('import os\nos.system("rm -rf /")\n'),
                         ['attribute', 'method'])
        self.assertEqual(self._kinds('import os\nos.remove("/tmp/x")\n'),
                         ['attribute', 'method'])
        self.assertEqual(self._kinds('import sys\nsys.modules.clear()\n'), ['attribute'])

    def test_banned_builtin_calls_are_rejected(self):
        for expr, kind in [("open('/etc/passwd')", 'call'),
                           ("eval('1+1')", 'call'),
                           ("exec('x=1')", 'call'),
                           ("compile('x', 'f', 'exec')", 'call'),
                           ("__import__('os')", 'call')]:
            vs = sl.lint_source(expr + '\n')
            self.assertTrue(any(v.kind == kind for v in vs), expr)

    def test_banned_write_methods_are_rejected(self):
        for expr in ['p.write_text("x")', 'p.unlink()', 'df.to_csv("/tmp/x.csv")',
                     'df.to_parquet("/tmp/x.pq")', 'p.mkdir()']:
            vs = sl.lint_source(expr + '\n')
            self.assertTrue(any(v.kind == 'method' for v in vs), expr)

    def test_dataframe_rename_is_NOT_a_violation(self):
        self.assertEqual(sl.lint_source('df = df.rename(columns={"a": "b"})\n'), [])

    def test_a_syntax_error_is_reported_as_one_violation(self):
        vs = sl.lint_source('def f(:\n')
        self.assertEqual(len(vs), 1)
        self.assertEqual(vs[0].kind, 'syntax')

    def test_violations_format_with_line_numbers(self):
        lines = sl.format_violations(sl.lint_source('import socket\n'))
        self.assertEqual(len(lines), 1)
        self.assertIn('line 1', lines[0])
        self.assertIn('socket', lines[0])


class TestFleetIsClean(unittest.TestCase):
    def test_every_live_strategy_file_lints_clean(self):
        impl = ROOT / 'src' / 'strategies' / 'implementations'
        files = sorted(glob.glob(str(impl / '*.py')))
        self.assertGreater(len(files), 100, 'fleet not found — wrong ROOT?')
        offenders = {}
        for f in files:
            vs = sl.lint_file(f)
            if vs:
                offenders[Path(f).name] = sl.format_violations(vs)
        self.assertEqual(offenders, {},
                         'the allowlist must cover every legitimate fleet import; '
                         'fix the strategy or widen ALLOWED_ROOTS deliberately')


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run it — expect failure**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest -q tests/strategies/test_strategy_lint.py
```

Expected: `ImportError: cannot import name 'strategy_lint'`.

- [ ] **Step 3: Write `src/strategies/strategy_lint.py`**

```python
"""strategy_lint.py — AST import allowlist for LLM-written strategies (QD §5 E4).

NOT A SANDBOX. This is a guardrail against a generated strategy reaching the
network or the filesystem by accident or prompt drift. It is trivially
defeatable by anyone trying: `backtest` is allowlisted (26 fleet files import
`backtest.quick_backtest`) and transitively reaches the whole repo. Treat a
clean lint as "no obvious I/O", never as "safe to run untrusted code".

ALLOWED_ROOTS is the MEASURED import census of src/strategies/implementations/
(AST walk, all 156 files, 2026-09-13) — not a wish list. The test
`TestFleetIsClean` re-derives it every run: widening the allowlist is a
deliberate, reviewed act, and narrowing it fails loudly instead of silently
rejecting the next candidate the strategycoder prompt produces.

`os` and `sys` are allowed as MODULES but restricted by ATTRIBUTE. 137 fleet
files import sys and 29 import os, and the strategycoder prompt itself
mandates `print(..., file=sys.stderr)` on every strategy. Rejecting the module
would reject the fleet; rejecting `os.system` / `os.remove` / `sys.modules` is
the part that actually matters.
"""
from __future__ import annotations

import ast
import json
import sys
from collections import namedtuple
from pathlib import Path

Violation = namedtuple('Violation', 'line kind detail')

# ── The measured census (see the module docstring) ───────────────────────────
ALLOWED_ROOTS = frozenset({
    # repo packages
    'strategies', 'src', 'backtest', 'lib', 'base', '_extra_panels',
    # scientific stack
    'pandas', 'numpy', 'scipy', 'sklearn', 'statsmodels', 'pyarrow',
    # stdlib the fleet actually uses
    '__future__', 'typing', 'sys', 'os', 'json', 'pathlib', 'traceback',
    'math', 'datetime', 'itertools', 'functools', 'logging', 'dataclasses',
    'enum', 'statistics', 'collections', 're',
})

# Attribute-level policy for the two modules whose ROOT is allowed only
# because the fleet needs a narrow slice of them.
ALLOWED_ATTRS = {
    'os':  frozenset({'environ', 'getenv', 'path', 'sep', 'pathsep', 'linesep', 'name'}),
    'sys': frozenset({'stderr', 'stdout', 'path', 'exit', 'argv',
                      'version_info', 'maxsize', 'platform'}),
}

BANNED_CALLS = frozenset({'open', 'eval', 'exec', 'compile', '__import__'})

# Method NAMES that write or reach out. Every one verified to have zero hits
# across the live fleet. `rename` is deliberately absent: all 4 fleet uses are
# DataFrame.rename, and a name-based ban would reject legitimate code.
BANNED_METHODS = frozenset({
    'write_text', 'write_bytes', 'unlink', 'rmdir', 'mkdir', 'chmod',
    'symlink_to', 'hardlink_to', 'touch',
    'to_csv', 'to_parquet', 'to_pickle', 'to_hdf', 'to_sql',
    'system', 'popen', 'remove', 'makedirs',
    'urlopen', 'check_output', 'Popen',
})


def _root(dotted: str) -> str:
    return (dotted or '').split('.')[0]


def lint_source(source: str, filename: str = '<candidate>') -> list:
    """Return the list of Violations in `source`. Never imports, never execs."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        return [Violation(getattr(e, 'lineno', 0) or 0, 'syntax', f'syntax error: {e.msg}')]

    out = []
    for node in ast.walk(tree):
        # import x, import x.y
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = _root(alias.name)
                if root not in ALLOWED_ROOTS:
                    out.append(Violation(node.lineno, 'import',
                                         f'import {alias.name!r} — root {root!r} is not on the allowlist'))
        # from x import y  /  from . import y
        elif isinstance(node, ast.ImportFrom):
            if node.level:                       # relative — inside the package, fine
                continue
            root = _root(node.module or '')
            if root not in ALLOWED_ROOTS:
                out.append(Violation(node.lineno, 'import',
                                     f'from {node.module!r} import … — root {root!r} is not on the allowlist'))
            elif root in ALLOWED_ATTRS and (node.module or '') == root:
                # `from os import system` would otherwise walk straight past the
                # attribute policy: the name is bound locally and never appears
                # as an ast.Attribute. Apply the same allowlist to the names.
                for alias in node.names:
                    if alias.name not in ALLOWED_ATTRS[root]:
                        out.append(Violation(node.lineno, 'import',
                                             f'from {root} import {alias.name} — only '
                                             f'{sorted(ALLOWED_ATTRS[root])} are permitted'))
        # os.<attr> / sys.<attr>
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            mod = node.value.id
            if mod in ALLOWED_ATTRS and node.attr not in ALLOWED_ATTRS[mod]:
                out.append(Violation(node.lineno, 'attribute',
                                     f'{mod}.{node.attr} — only '
                                     f'{sorted(ALLOWED_ATTRS[mod])} are permitted'))
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in BANNED_CALLS:
                out.append(Violation(node.lineno, 'call', f'{fn.id}(…) is not permitted'))
            elif isinstance(fn, ast.Attribute):
                if fn.attr in BANNED_CALLS:
                    out.append(Violation(node.lineno, 'call', f'.{fn.attr}(…) is not permitted'))
                elif fn.attr in BANNED_METHODS:
                    out.append(Violation(node.lineno, 'method',
                                         f'.{fn.attr}(…) writes or reaches out — not permitted '
                                         f'in a strategy file'))

    out.sort(key=lambda v: (v.line, v.kind, v.detail))
    return out


def lint_file(path) -> list:
    p = Path(path)
    try:
        source = p.read_text(encoding='utf-8')
    except Exception as e:
        return [Violation(0, 'io', f'cannot read {p}: {e}')]
    return lint_source(source, filename=str(p))


def format_violations(violations) -> list:
    return [f'[import-lint] line {v.line}: {v.kind}: {v.detail}' for v in violations]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print('Usage: strategy_lint.py <file> [<file>...]', file=sys.stderr)
        return 2
    rows = []
    for path in argv:
        for v in lint_file(path):
            rows.append({'file': path, 'line': v.line, 'kind': v.kind, 'detail': v.detail})
    print(json.dumps({'ok': not rows, 'violations': rows}, indent=2))
    return 1 if rows else 0


if __name__ == '__main__':
    sys.exit(main())
```

Note `collections` and `re` are in `ALLOWED_ROOTS` although the current census does not show them at module level: both appear in the spec's list, both are pure stdlib with no I/O, and `re` is used inside function bodies across the fleet. Adding them cannot make the fleet test fail; removing them would reject an obvious future candidate.

- [ ] **Step 4: Run it — expect PASS, including the fleet sweep**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest -q tests/strategies/test_strategy_lint.py
```

Expected: `17 passed`. **If `TestFleetIsClean` fails, the offender list names the file and line — widen `ALLOWED_ROOTS` deliberately (and record why in the commit message) or fix the strategy. Do not weaken the test.**

- [ ] **Step 5: Wire into `validate_strategy.py`.** Insert immediately after the `# ── 2. Syntax / import ──` comment block's `abs_path = os.path.abspath(filepath)` (`:70`) and BEFORE `module_name` is computed at `:71`:

```python
    # ── 2a. Import allowlist (QD spec §5 E4) ──────────────────────────────────
    # Runs BEFORE the first import, so a candidate that reaches for subprocess
    # / requests / open() never gets executed. unified_backtest.load_strategy_class
    # calls validate() first, so this covers that path too.
    from strategies.strategy_lint import lint_file, format_violations
    lint_violations = lint_file(abs_path)
    if lint_violations:
        return {'ok': False,
                'errors': format_violations(lint_violations),
                'signal_count': 0}
```

- [ ] **Step 6: Wire the pre-flight into `research-orchestrator.js`.** In `_runGateChain`, replace `:1298-1299`:

```javascript
    onPhase('validate', 40);
    const validResult = await this._validateFn(implPath, opts);
```

with:

```javascript
    onPhase('validate', 40);

    // QD E4: AST import lint BEFORE the 60s validate_strategy spawn. A file
    // that reaches for subprocess/requests/open() is rejected here, attributed
    // as import_violation rather than the generic contract_violation, and never
    // gets imported by anything.
    const lint = await _spawnPython(['src/strategies/strategy_lint.py', implPath],
                                    { cwd: OPENCLAW_DIR, timeoutMs: 20_000, onChild: opts.onChild });
    let lintOut = null;
    try { lintOut = JSON.parse(lint.stdout); } catch (_) { lintOut = null; }
    if (lintOut && lintOut.ok === false) {
      const lintLog = (lintOut.violations || [])
        .map(v => `line ${v.line}: ${v.kind}: ${v.detail}`).join('\n');
      const lPaperId = await paperIdForCandidate(candidate_id);
      if (!suppressQueueWrite) {
        await this._query(
          `UPDATE implementation_queue SET status = 'validation_failed', error_log = $1 WHERE candidate_id = $2`,
          [lintLog, candidate_id]
        );
      }
      await this._emitDecisionFn({
        paperId:      lPaperId,
        candidateId:  candidate_id,
        strategyId:   stratId,
        gateName:     'validate',
        outcome:      'reject',
        reasonCode:   'import_violation',
        reasonDetail: lintLog,
        metadata:     { violations: lintOut.violations || [] },
      });
      notify?.(`  ❌ ${stratId} import lint failed: ${lintLog.slice(0, 200)}`);
      channelNotify?.(`❌ **${stratId}** rejected — disallowed imports/calls (see implementation_queue).`);
      return { ok: false, result: { promoted: false, reasonCode: 'import_violation', error: lintLog } };
    }
    // lintOut === null means the lint itself failed to run (infra) — fail OPEN
    // and let validate_strategy.py's own in-process lint be the authority.

    const validResult = await this._validateFn(implPath, opts);
```

- [ ] **Step 7: Document it in the prompt.** In `src/agent/prompts/subagents/strategycoder.md`, in the `Rules:` list at `:106-118`, after the `- No naked class-body imports` line insert:

```markdown
- **Imports are enforced by an AST allowlist** (`src/strategies/strategy_lint.py`), run before the file is ever imported. Permitted roots: `strategies`, `src`, `backtest`, `lib`, `base`, `_extra_panels`, `pandas`, `numpy`, `scipy`, `sklearn`, `statsmodels`, `pyarrow`, `__future__`, `typing`, `sys`, `os`, `json`, `pathlib`, `traceback`, `math`, `datetime`, `itertools`, `functools`, `logging`, `dataclasses`, `enum`, `statistics`, `collections`, `re`, plus relative imports. `os` is limited to `os.environ` / `os.path` / `os.getenv`; `sys` to `sys.stderr` / `sys.stdout` / `sys.path` / `sys.exit` / `sys.argv`.
- **Never** `subprocess`, `socket`, `requests`, `urllib`, `http`, `shutil`, `importlib`, `pickle`, `ctypes`, `multiprocessing` — and never `open()`, `eval()`, `exec()`, `compile()`, `__import__()`, `.to_csv()`, `.to_parquet()`, `.write_text()`. A strategy reads the panels it is handed and returns `Signal`s; it never touches the network or the filesystem. Violations reject the candidate before validation runs.
```

- [ ] **Step 8: Run the tests — expect PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest -q tests/strategies/test_strategy_lint.py tests/strategies/test_base_stops_targets.py
```

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 src/strategies/strategy_lint.py src/strategies/implementations/S_coint_pairs_sector_v2.py
```

Expected: pytest green; the CLI prints `{"ok": true, "violations": []}` and exits 0.

- [ ] **Step 9: Commit**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/strategies/strategy_lint.py tests/strategies/test_strategy_lint.py src/strategies/validate_strategy.py src/agent/research/research-orchestrator.js src/agent/prompts/subagents/strategycoder.md
git commit -m "feat(strategies): AST import allowlist for LLM-written strategies (QD E4)

Allowlist is the measured import census of all 156 fleet files, not a wish
list — TestFleetIsClean re-derives it every run and asserts zero violations.
os and sys are allowed as modules but restricted by attribute (the
strategycoder prompt mandates print(..., file=sys.stderr)); os.system,
os.remove, sys.modules and 21 write/network method names are rejected.
DataFrame.rename is deliberately not banned.

Two hooks: validate_strategy.py before its first import (authoritative — it
also covers unified_backtest.load_strategy_class), and a pre-flight in
research-orchestrator.js so a violating candidate never pays for the 60s
python spawn, rejected as import_violation. Guardrail, not a sandbox.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

<!-- END TASK 9 -->

---

### Task 10 — E5: live KPIs on the portfolio page

Three spec corrections, all verified today:

1. **The route is `/api/portfolio/summary` (`server.js:1166`), not `/api/portfolio/stats`.** The SQL the spec points at is `:1224-1246`.
2. **`trade_daily_marks` is not a table.** `grep -rn trade_daily_marks src/database/migrations` returns nothing; it is a *function* at `src/backtest/backtest_panel.py:106` operating on backtest trades. There is no live per-signal high/low store. **Resolution:** live MAE = `LEAST(MIN(signal_pnl.unrealized_pnl_pct), 0)` per `signal_id` — a **close-to-close** MAE, not intraday. It needs no schema change (spec §5 E5 allows "no schema change except MAE if it needs a column"; it doesn't), and it is honest about what it measures. The tile is labelled "Median MAE (close-to-close)".
3. **Every new number MUST be scoped to the account epoch, SQL and NAV alike.** The two new queries repeat the same three WHERE clauses as the query beside them — `sp.status='closed'`, `sp.close_reason IS DISTINCT FROM 'rolled_continuation'` (SP-6 D1 roll segments are not trades), `es.signal_date >= $1::date` (the 2026-09-04 account epoch) — or the new tiles silently disagree with `avg_realized` and `win_rate` rendered next to them. **And the NAV-derived numbers get the same filter**: `logs/pnl_daily_ohlc.json` is a raw store with no epoch awareness, and on 2026-09-08 exactly that gap produced a fake +8.75 % cutover jump on the P&L chart (changelog 2026-09-08 20:40 UTC) — `_buildCandles` gained an epoch filter for it (`server.js:2892`). The file is pruned clean today, so an unfiltered read would not misreport *now*; it would misreport silently at the next cutover. `monthly_returns` and `win_days/lose_days` therefore read only days `>= epoch`.

Also: `expectancy_pct` is arithmetically identical to the existing `avg_realized` (`AVG(realized_pnl_pct)` over the same rows). It is surfaced under its trading name because that is what an operator looks for, and the identity is stated in the code comment rather than papered over with an invented weighting.

**Files:**
- Create: `src/channels/api/nav_kpis.js`
- Create: `tests/channels/test_nav_kpis.test.js`
- Modify: `src/channels/api/server.js` (`/api/portfolio/summary` :1166-1282; the stat-card markup :4763-4768; `renderPortfolioSummary` :7426)

**Interfaces:**
- Produces (`src/channels/api/nav_kpis.js`):
  - `TRADE_KPI_SQL: string` — the aggregate query (one `$1` = epoch)
  - `MAE_SQL: string` — the per-signal MAE query (one `$1` = epoch)
  - `deriveTradeKpis(row) -> {profit_factor, expectancy_pct, payoff_ratio, avg_win_pct, avg_loss_pct, gross_win_pct, gross_loss_pct}`
  - `navMonthlyReturns(days, {limit = 12}) -> [{month: 'YYYY-MM', return_pct: number|null, days: number}]`
  - `navDayCounts(days) -> {win_days, lose_days, flat_days}`
- Consumes: `signal_pnl` + `execution_signals` (read-only), `_loadOhlcStore()` (`server.js:2550`) → `{days: {'YYYY-MM-DD': {open, high, low, close}}}`, `_accountEpoch()` (`server.js:552`).
- Response (`/api/portfolio/summary`) gains: `profit_factor`, `expectancy_pct`, `payoff_ratio`, `avg_win_pct`, `avg_loss_pct`, `mae_median_pct`, `mae_n`, `win_days`, `lose_days`, `monthly_returns`. Every existing field is unchanged.

- [ ] **Step 1: Write the failing test** — create `tests/channels/test_nav_kpis.test.js`:

```javascript
'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const k    = require(path.join(ROOT, 'src/channels/api/nav_kpis.js'));

// ── deriveTradeKpis ─────────────────────────────────────────────────────────
test('profit factor, payoff and expectancy from one aggregate row', () => {
  // 3 winners totalling +0.30 (avg +0.10), 2 losers totalling -0.10 (avg -0.05)
  const out = k.deriveTradeKpis({
    gross_win: '0.30', gross_loss: '0.10',
    avg_win: '0.10', avg_loss: '0.05',
    expectancy_pct: '0.04', n_closed: '5',
  });
  assert.equal(out.profit_factor, 3);
  assert.equal(out.payoff_ratio, 2);
  assert.equal(out.expectancy_pct, 0.04);
  assert.equal(out.avg_win_pct, 0.10);
  assert.equal(out.avg_loss_pct, 0.05);
});

test('no losers means profit factor and payoff are null, not Infinity', () => {
  const out = k.deriveTradeKpis({
    gross_win: '0.30', gross_loss: '0', avg_win: '0.10', avg_loss: null,
    expectancy_pct: '0.10', n_closed: '3',
  });
  assert.equal(out.profit_factor, null);
  assert.equal(out.payoff_ratio, null);
});

test('an empty book yields nulls throughout, never NaN', () => {
  const out = k.deriveTradeKpis({
    gross_win: null, gross_loss: null, avg_win: null, avg_loss: null,
    expectancy_pct: null, n_closed: '0',
  });
  for (const v of Object.values(out)) assert.ok(v === null, JSON.stringify(out));
});

test('deriveTradeKpis tolerates a missing row', () => {
  const out = k.deriveTradeKpis(undefined);
  assert.equal(out.profit_factor, null);
  assert.equal(out.expectancy_pct, null);
});

// ── NAV strip ───────────────────────────────────────────────────────────────
const FIXTURE_DAYS = {
  '2026-07-30': { open: 100000, high: 100000, low: 100000, close: 100000 },
  '2026-07-31': { open: 100000, high: 101000, low:  99000, close: 101000 },
  '2026-08-03': { open: 101000, high: 101500, low: 100000, close: 100500 },
  '2026-08-31': { open: 100500, high: 103000, low: 100000, close: 102510 },
  '2026-09-01': { open: 102510, high: 102600, low: 101000, close: 101000 },
  '2026-09-02': { open: 101000, high: 101000, low: 101000, close: 101000 },
};

test('navMonthlyReturns is last-close over previous-month-last-close', () => {
  const rows = k.navMonthlyReturns(FIXTURE_DAYS, { limit: 12 });
  const by = Object.fromEntries(rows.map(r => [r.month, r]));
  assert.equal(by['2026-07'].return_pct, null);          // no prior month anchor
  // Aug: 102510 / 101000 - 1 = +1.4950495…%
  assert.ok(Math.abs(by['2026-08'].return_pct - 1.4950495) < 1e-6, by['2026-08'].return_pct);
  // Sep: 101000 / 102510 - 1 = -1.4730270…%
  assert.ok(Math.abs(by['2026-09'].return_pct + 1.4730270) < 1e-6, by['2026-09'].return_pct);
  assert.equal(by['2026-08'].days, 2);
});

test('navMonthlyReturns returns at most `limit` months, newest last', () => {
  const rows = k.navMonthlyReturns(FIXTURE_DAYS, { limit: 2 });
  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map(r => r.month), ['2026-08', '2026-09']);
});

test('navMonthlyReturns on an empty store is an empty array', () => {
  assert.deepEqual(k.navMonthlyReturns({}, { limit: 12 }), []);
  assert.deepEqual(k.navMonthlyReturns(null, { limit: 12 }), []);
});

test('navDayCounts splits up / down / flat sessions', () => {
  const c = k.navDayCounts(FIXTURE_DAYS);
  // 07-31 up, 08-03 down, 08-31 up, 09-01 down, 09-02 flat (first day has no prior)
  assert.equal(c.win_days, 2);
  assert.equal(c.lose_days, 2);
  assert.equal(c.flat_days, 1);
});

test('navDayCounts on an empty store is all zeroes', () => {
  assert.deepEqual(k.navDayCounts({}), { win_days: 0, lose_days: 0, flat_days: 0 });
});

test('pre-epoch NAV days must be filtered out before these helpers see them', () => {
  // The endpoint filters the store by pipeline_config.account_epoch exactly as
  // _buildCandles does (server.js:2892). This pins the arithmetic that filter
  // protects: treat everything before 2026-08-01 as the OLD account.
  const epoch = '2026-08-01';
  const filtered = Object.fromEntries(
    Object.entries(FIXTURE_DAYS).filter(([d]) => d >= epoch));

  const kept = k.navMonthlyReturns(filtered, { limit: 12 });
  assert.deepEqual(kept.map(r => r.month), ['2026-08', '2026-09']);
  // August has no in-epoch anchor, so it must render as "—", not as a number
  // computed against an old-account close.
  assert.equal(kept[0].return_pct, null);

  // Unfiltered, August IS anchored to the pre-epoch 07-31 close — the exact
  // shape of the 2026-09-08 fake-jump bug.
  const leaked = k.navMonthlyReturns(FIXTURE_DAYS, { limit: 12 })
    .find(r => r.month === '2026-08');
  assert.notEqual(leaked.return_pct, null);

  // Day counts must not span the cutover either.
  assert.deepEqual(k.navDayCounts(filtered), { win_days: 1, lose_days: 1, flat_days: 1 });
});

// ── SQL guards: the new aggregates must be scoped exactly like the old ones ──
test('TRADE_KPI_SQL carries the closed / rolled / epoch clauses', () => {
  const s = k.TRADE_KPI_SQL;
  assert.match(s, /sp\.status\s*=\s*'closed'/);
  assert.match(s, /close_reason IS DISTINCT FROM 'rolled_continuation'/);
  assert.match(s, /es\.signal_date >= \$1::date/);
  assert.match(s, /JOIN execution_signals es ON es\.id = sp\.signal_id/);
  assert.ok(!/DELETE|UPDATE|INSERT/i.test(s), 'read-only');
});

test('MAE_SQL is scoped to closed, non-rolled, in-epoch signals', () => {
  const s = k.MAE_SQL;
  assert.match(s, /sp\.status\s*=\s*'closed'/);
  assert.match(s, /close_reason IS DISTINCT FROM 'rolled_continuation'/);
  assert.match(s, /es\.signal_date >= \$1::date/);
  assert.match(s, /LEAST\(MIN\(sp\.unrealized_pnl_pct\), 0\)/);
  assert.ok(!/DELETE|UPDATE|INSERT/i.test(s), 'read-only');
});
```

- [ ] **Step 2: Run it — expect failure**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/channels/test_nav_kpis.test.js
```

Expected: `Cannot find module '.../src/channels/api/nav_kpis.js'`.

- [ ] **Step 3: Write `src/channels/api/nav_kpis.js`**

```javascript
'use strict';
/**
 * nav_kpis.js — live trading KPIs for the portfolio page (QD spec §5 E5).
 *
 * Pure functions + two read-only SQL strings, kept out of server.js so they
 * can be unit-tested without a database or an HTTP server.
 *
 * SCOPING IS LOAD-BEARING. Both queries repeat the exact three predicates the
 * existing summary aggregates use (server.js:1224-1246):
 *   sp.status = 'closed'
 *   sp.close_reason IS DISTINCT FROM 'rolled_continuation'   (SP-6 D1 roll
 *       segments are segments of an ongoing position, not trades)
 *   es.signal_date >= $1::date                               (account epoch)
 * Drop any one of them and the new tiles disagree with the tiles beside them.
 *
 * MAE NOTE: there is no live per-signal intraday high/low store —
 * `trade_daily_marks` is a backtest-side FUNCTION
 * (src/backtest/backtest_panel.py:106), not a table. The live MAE here is
 * therefore CLOSE-TO-CLOSE: the worst daily mark a signal ever printed,
 * floored at 0. The tile says so.
 */

// One row. gross_loss is returned POSITIVE (negated in SQL) so the JS never
// has to reason about the sign of a sum of negatives.
const TRADE_KPI_SQL = `
  SELECT COUNT(*)                                                                   AS n_closed,
         COALESCE( SUM(sp.realized_pnl_pct) FILTER (WHERE sp.realized_pnl_pct > 0), 0) AS gross_win,
         COALESCE(-SUM(sp.realized_pnl_pct) FILTER (WHERE sp.realized_pnl_pct < 0), 0) AS gross_loss,
                   AVG(sp.realized_pnl_pct) FILTER (WHERE sp.realized_pnl_pct > 0)     AS avg_win,
                  -AVG(sp.realized_pnl_pct) FILTER (WHERE sp.realized_pnl_pct < 0)     AS avg_loss,
                   AVG(sp.realized_pnl_pct)                                            AS expectancy_pct
    FROM signal_pnl sp
    JOIN execution_signals es ON es.id = sp.signal_id
   WHERE sp.status = 'closed'
     AND sp.realized_pnl_pct IS NOT NULL
     AND sp.close_reason IS DISTINCT FROM 'rolled_continuation'
     AND es.signal_date >= $1::date
`;

// Per-signal worst daily mark (floored at 0 — a trade that never went under
// water has an MAE of zero, not a positive number), then the median.
const MAE_SQL = `
  WITH closed AS (
    SELECT DISTINCT sp.signal_id
      FROM signal_pnl sp
      JOIN execution_signals es ON es.id = sp.signal_id
     WHERE sp.status = 'closed'
       AND sp.realized_pnl_pct IS NOT NULL
       AND sp.close_reason IS DISTINCT FROM 'rolled_continuation'
       AND es.signal_date >= $1::date
  ),
  marks AS (
    SELECT sp.signal_id, LEAST(MIN(sp.unrealized_pnl_pct), 0) AS mae_pct
      FROM signal_pnl sp
      JOIN closed c ON c.signal_id = sp.signal_id
     WHERE sp.unrealized_pnl_pct IS NOT NULL
     GROUP BY sp.signal_id
  )
  SELECT ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY mae_pct)::numeric, 4) AS mae_median_pct,
         COUNT(*)::int                                                           AS mae_n
    FROM marks
`;

function _num(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/**
 * @param {object} row one TRADE_KPI_SQL row
 * @returns {{profit_factor:number|null, expectancy_pct:number|null,
 *            payoff_ratio:number|null, avg_win_pct:number|null,
 *            avg_loss_pct:number|null, gross_win_pct:number|null,
 *            gross_loss_pct:number|null}}
 */
function deriveTradeKpis(row) {
  const r         = row || {};
  const n         = _num(r.n_closed) || 0;
  const grossWin  = _num(r.gross_win);
  const grossLoss = _num(r.gross_loss);
  const avgWin    = _num(r.avg_win);
  const avgLoss   = _num(r.avg_loss);
  // expectancy == AVG(realized_pnl_pct) over exactly the rows `avg_realized`
  // already averages. Same number, surfaced under the name an operator looks
  // for; not a separately weighted statistic.
  const expectancy = _num(r.expectancy_pct);

  const round4 = (v) => (v === null ? null : Math.round(v * 1e4) / 1e4);

  return {
    profit_factor: (n > 0 && grossLoss !== null && grossLoss > 0 && grossWin !== null)
      ? Math.round((grossWin / grossLoss) * 1e4) / 1e4 : null,
    payoff_ratio: (avgWin !== null && avgLoss !== null && avgLoss > 0)
      ? Math.round((avgWin / avgLoss) * 1e4) / 1e4 : null,
    expectancy_pct: n > 0 ? round4(expectancy) : null,
    avg_win_pct:    n > 0 ? round4(avgWin)  : null,
    avg_loss_pct:   n > 0 ? round4(avgLoss) : null,
    gross_win_pct:  n > 0 ? round4(grossWin)  : null,
    gross_loss_pct: n > 0 ? round4(grossLoss) : null,
  };
}

/**
 * Month-over-month NAV return from the OHLC store's closes.
 * Anchor = the previous CALENDAR month's last close (not the month's own
 * first open), so a gap over a month boundary is attributed to the new month.
 * The first month in the series has no anchor and returns null.
 */
function navMonthlyReturns(days, { limit = 12 } = {}) {
  if (!days || typeof days !== 'object') return [];
  const dates = Object.keys(days).filter(d => days[d] && days[d].close != null).sort();
  if (!dates.length) return [];

  const lastCloseByMonth = new Map();   // 'YYYY-MM' -> close of its last session
  const countByMonth     = new Map();
  for (const d of dates) {
    const m = d.slice(0, 7);
    lastCloseByMonth.set(m, Number(days[d].close));
    countByMonth.set(m, (countByMonth.get(m) || 0) + 1);
  }

  const months = [...lastCloseByMonth.keys()].sort();
  const rows = months.map((m, i) => {
    const prev = i > 0 ? lastCloseByMonth.get(months[i - 1]) : null;
    const cur  = lastCloseByMonth.get(m);
    const ret  = (prev != null && prev > 0 && Number.isFinite(cur))
      ? (cur / prev - 1) * 100 : null;
    return { month: m, return_pct: ret, days: countByMonth.get(m) || 0 };
  });
  return rows.slice(-Math.max(1, limit));
}

/** Session-over-session up / down / flat day counts from the same store. */
function navDayCounts(days) {
  const out = { win_days: 0, lose_days: 0, flat_days: 0 };
  if (!days || typeof days !== 'object') return out;
  const dates = Object.keys(days).filter(d => days[d] && days[d].close != null).sort();
  let prev = null;
  for (const d of dates) {
    const c = Number(days[d].close);
    if (prev != null && Number.isFinite(c)) {
      if (c > prev) out.win_days++;
      else if (c < prev) out.lose_days++;
      else out.flat_days++;
    }
    prev = Number.isFinite(c) ? c : prev;
  }
  return out;
}

module.exports = { TRADE_KPI_SQL, MAE_SQL, deriveTradeKpis, navMonthlyReturns, navDayCounts };
```

- [ ] **Step 4: Run it — expect PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/channels/test_nav_kpis.test.js
```

Expected: `# pass 12`.

- [ ] **Step 5: Extend the endpoint.** In `src/channels/api/server.js`, add the two queries to the existing `Promise.all` at `:1218-1247` (append two entries after `dbQuery(win30dSql, win30dArgs)`) and destructure them:

```javascript
    const _kpis = require('./nav_kpis');
    const [openRes, statsRes, winLifeRes, win30dRes, kpiRes, maeRes] = await Promise.all([
      /* …the four existing entries, unchanged… */
      dbQuery(win30dSql, win30dArgs),
      dbQuery(_kpis.TRADE_KPI_SQL, [epoch]).catch(() => ({ rows: [{}] })),
      dbQuery(_kpis.MAE_SQL,       [epoch]).catch(() => ({ rows: [{}] })),
    ]);
```

After `const w30 = win30dRes.rows[0];` (`:1252`) add:

```javascript
    // QD E5: profit factor / expectancy / payoff / MAE / NAV calendar.
    // Same closed + non-rolled + epoch scoping as the aggregates above —
    // see the comment block in nav_kpis.js.
    const tradeKpis = _kpis.deriveTradeKpis(kpiRes.rows[0]);
    const mae       = maeRes.rows[0] || {};
    // Epoch-filter the NAV store the same way _buildCandles does (server.js:2892).
    // The store keeps ~400 days and is NOT epoch-aware; an unfiltered read
    // fuses old-account days into the strip at the next cutover.
    let navDays = {};
    try {
      const _navAll = (_loadOhlcStore() || {}).days || {};
      navDays = Object.fromEntries(Object.entries(_navAll).filter(([d]) => d >= epoch));
    } catch (_) { navDays = {}; }
    const dayCounts = _kpis.navDayCounts(navDays);
    const monthly   = _kpis.navMonthlyReturns(navDays, { limit: 12 });
```

and add to the `res.json({...})` payload (after `avg_days_held: stats.avg_days_held,` at `:1279`):

```javascript
      // ── Live KPIs (QD E5) ───────────────────────────────────────────────
      profit_factor:    tradeKpis.profit_factor,
      // Identical to avg_realized by construction (same rows, same AVG);
      // surfaced under the name operators look for.
      expectancy_pct:   tradeKpis.expectancy_pct,
      payoff_ratio:     tradeKpis.payoff_ratio,
      avg_win_pct:      tradeKpis.avg_win_pct,
      avg_loss_pct:     tradeKpis.avg_loss_pct,
      // Close-to-close MAE: worst daily mark per signal, floored at 0.
      // No intraday store exists on the live side.
      mae_median_pct:   mae.mae_median_pct ?? null,
      mae_n:            mae.mae_n ?? 0,
      win_days:         dayCounts.win_days,
      lose_days:        dayCounts.lose_days,
      monthly_returns:  monthly,
```

- [ ] **Step 6: Add the tiles + strip.** In `getDashboardHtml()`, immediately after the closing `</div>` of `<div class="pf-summary-row" id="pf-summary">` (`:4763-4768`) insert:

```html
  <div class="pf-summary-row" id="pf-kpi-row" style="margin-top:12px;grid-template-columns:repeat(6,1fr)">
    <div class="pf-stat-card" title="Sum of winning trade returns / sum of losing trade returns, over closed positions since the account epoch. Above 1.0 means the winners outweigh the losers."><div class="pf-stat-label">Profit Factor</div><div class="pf-stat-value" id="pf-pf">—</div></div>
    <div class="pf-stat-card" title="Mean realized return per closed position. Arithmetically identical to the Avg Realized figure — shown under its trading name."><div class="pf-stat-label">Expectancy</div><div class="pf-stat-value" id="pf-expectancy">—</div><div class="pf-stat-sub" id="pf-expectancy-sub"></div></div>
    <div class="pf-stat-card" title="Average winning trade / average losing trade."><div class="pf-stat-label">Payoff Ratio</div><div class="pf-stat-value" id="pf-payoff">—</div><div class="pf-stat-sub" id="pf-payoff-sub"></div></div>
    <div class="pf-stat-card" title="Median maximum adverse excursion, measured CLOSE-TO-CLOSE: the worst daily mark each closed position ever printed, floored at 0. There is no intraday high/low store on the live side."><div class="pf-stat-label">Median MAE<br><span style="font-size:9px;color:var(--dim);font-weight:400">close-to-close</span></div><div class="pf-stat-value" id="pf-mae">—</div><div class="pf-stat-sub" id="pf-mae-sub"></div></div>
    <div class="pf-stat-card" title="NAV sessions closing up vs down, from the equity OHLC store."><div class="pf-stat-label">Win / Lose Days</div><div class="pf-stat-value" id="pf-daycounts">—</div><div class="pf-stat-sub" id="pf-daycounts-sub"></div></div>
    <div class="pf-stat-card" title="Average winning and losing trade return."><div class="pf-stat-label">Avg Win / Loss</div><div class="pf-stat-value" id="pf-avgwl">—</div></div>
  </div>
  <div class="pf-chart-wrap" id="pf-monthly-wrap" style="margin-top:12px">
    <div class="pf-chart-label" style="margin-bottom:8px">Monthly NAV Return (last 12 months)</div>
    <div id="pf-monthly-strip" style="display:flex;gap:4px;flex-wrap:wrap"></div>
  </div>
```

- [ ] **Step 7: Render them.** In `renderPortfolioSummary(s, valCurve)` (`:7426`), add before the closing brace:

```javascript
  // ── QD E5 live KPIs ────────────────────────────────────────────────────
  const num = (v, dp, suffix) => (v == null ? '—' : Number(v).toFixed(dp) + (suffix || ''));
  const pct = (v) => (v == null ? '—' : ((v >= 0 ? '+' : '') + (Number(v) * 100).toFixed(2) + '%'));

  document.getElementById('pf-pf').textContent         = num(s.profit_factor, 2);
  document.getElementById('pf-expectancy').textContent = pct(s.expectancy_pct);
  document.getElementById('pf-expectancy-sub').textContent = 'per closed position';
  document.getElementById('pf-payoff').textContent     = num(s.payoff_ratio, 2);
  document.getElementById('pf-payoff-sub').textContent =
    (s.avg_win_pct != null && s.avg_loss_pct != null)
      ? pct(s.avg_win_pct) + ' vs ' + pct(-s.avg_loss_pct) : '';
  document.getElementById('pf-mae').textContent        = pct(s.mae_median_pct);
  document.getElementById('pf-mae-sub').textContent    =
    s.mae_n ? s.mae_n + ' closed positions' : 'no marks yet';
  document.getElementById('pf-daycounts').innerHTML =
    '<span class="positive">' + (s.win_days ?? 0) + '</span>'
    + '<span style="color:var(--dim);font-weight:400">&nbsp;|&nbsp;</span>'
    + '<span class="negative">' + (s.lose_days ?? 0) + '</span>';
  const totDays = (s.win_days ?? 0) + (s.lose_days ?? 0);
  document.getElementById('pf-daycounts-sub').textContent =
    totDays ? Math.round((s.win_days / totDays) * 100) + '% up sessions' : 'no NAV history';
  document.getElementById('pf-avgwl').textContent =
    (s.avg_win_pct != null || s.avg_loss_pct != null)
      ? pct(s.avg_win_pct) + ' / ' + pct(s.avg_loss_pct == null ? null : -s.avg_loss_pct)
      : '—';

  // 12-month strip. A month with no prior-month anchor renders as an em dash
  // rather than a fake 0% — the live NAV store began 2026-09-05.
  const strip = document.getElementById('pf-monthly-strip');
  const months = Array.isArray(s.monthly_returns) ? s.monthly_returns : [];
  strip.innerHTML = months.length === 0
    ? '<span style="color:var(--dim)">No NAV history yet</span>'
    : months.map(m => {
        const cls = m.return_pct == null ? 'neutral' : (m.return_pct >= 0 ? 'positive' : 'negative');
        const val = m.return_pct == null ? '—'
                  : ((m.return_pct >= 0 ? '+' : '') + m.return_pct.toFixed(2) + '%');
        return '<div style="flex:1 1 70px;min-width:70px;padding:6px;border:1px solid var(--border2);'
             + 'border-radius:4px;text-align:center" title="' + m.days + ' sessions">'
             + '<div style="font-size:10px;color:var(--muted)">' + m.month + '</div>'
             + '<div class="' + cls + '" style="font-size:12px">' + val + '</div></div>';
      }).join('');
```

- [ ] **Step 8: Run the tests — expect PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/channels/test_nav_kpis.test.js tests/channels/test_api_strategies_backtest.test.js tests/channels/test_positions_grouped.test.js
```

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --check src/channels/api/server.js && echo "server.js parses"
```

Expected: tests pass; `server.js parses`. (`node --check` is a syntax parse only — it does not start the server or touch the DB.)

- [ ] **Step 9: Commit**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/channels/api/nav_kpis.js tests/channels/test_nav_kpis.test.js src/channels/api/server.js
git commit -m "feat(dashboard): profit factor, expectancy, payoff, MAE, NAV calendar (QD E5)

/api/portfolio/summary gains profit_factor, expectancy_pct, payoff_ratio,
avg_win_pct, avg_loss_pct, mae_median_pct, win_days/lose_days and a 12-month
monthly_returns strip; the portfolio page gains a 6-tile KPI row and the strip.

Both new queries repeat the exact closed / non-rolled_continuation /
account-epoch scoping the existing aggregates use, so the new tiles cannot
disagree with the ones beside them. MAE is close-to-close
(LEAST(MIN(unrealized_pnl_pct),0) per signal) and labelled as such: the spec's
trade_daily_marks is a backtest FUNCTION, not a live table, so no intraday
high/low exists on this side. No schema change. SQL is read-only.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

<!-- END TASK 10 -->

---

### Task 11 — changelog entry + stream verification

Spec §0: "Log to `docs/archive/changelog.md` (newest first) per stream, not to CLAUDE.md." Spec §7 sets the per-stream gate.

**Files:**
- Modify: `docs/archive/changelog.md` (one entry, inserted directly under the `## Recent Changes` heading at `:8`)

**Interfaces:** none (documentation).

- [ ] **Step 1: Run the stream verification.** All read-only; none of it starts a service.

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest -q tests/lib/test_run_lock.py tests/lib/test_capped_spawn.py tests/lib/test_proc_heartbeat.py tests/execution/test_run_lock_wiring.py tests/strategies/test_strategy_lint.py
```

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/lib/run_lock.test.js tests/lib/proc_heartbeat.test.js tests/channels/test_nav_kpis.test.js tests/test_daily_cycle_run_lock.test.js tests/test_daily_cycle_capped_spawn.test.js tests/test_daily_cycle_idle_watchdog.test.js tests/test_daily_cycle_helpers.test.js tests/test_daily_cycle_node.test.js tests/test_daily_cycle_abort_alert.test.js tests/test_daily_cycle_graph.test.js
```

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m system_checks --tag pipeline --tag agents
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 src/maintenance/doctor.py --quick
```

Expected: all green (or pre-existing WARNs only — record any WARN that predates this branch rather than "fixing" it here).

- [ ] **Step 2: Write the entry.** Insert immediately after the `## Recent Changes` line in `docs/archive/changelog.md`:

```markdown
- **2026-09-13: QuantDinger Stream E — reliability (items 10–13, 18), branch `worktree-qd-adoptions`.** Spec `docs/specs/2026-09-12-quantdinger-adoptions-spec.md` §5, plan `docs/superpowers/plans/2026-09-12-qd-stream-e-reliability.md`. Nothing here changes a signal, a size or a Sharpe. **E1 — one owned run lock.** There were TWO locks and neither was owned: Python `pipeline:running:<date>`=`'1'` and JS `engine:run_lock:<date>`, so the orchestrator and the LangGraph cycle could run at the same time; and a held lock made the Python side `return 0`, recording a "successful" cycle that never ran. Now ONE key `pipeline:run_lock:<date>` from `src/lib/run_lock_key.json`, value `host:pid:start_iso`, read by twinned `src/lib/run_lock.py` / `run_lock.js`: takeover only when the holder PID is dead ON THIS HOST, TTL renewed by the step runner to that step's own timeout + 120 s (inside `run_step`, so the bounded `signals` retry renews too), and every renew/release value-checked so a process whose lock was taken over cannot delete the new owner's key. Held lock ⇒ `rc=75` (EX_TEMPFAIL) + a `[lock] held by …` post; `--force-resume` still overrides, now loudly. **Four** readers were fixed, not two: the orchestrator, `daily-cycle.js`, `cron-schedule.js:210` (was reading the dead `pipeline:running:` key), and `doctor.check_orchestrator_lock`, which scanned `pipeline:lock:*` — a pattern matching NEITHER live key, so it had been reporting "no locks held" unconditionally since it was written. **E2 — MemoryMax + OnFailure.** New `src/lib/capped_spawn.py` (twin of `capped_spawn.js`, own `OPENCLAW_STEP_MEMORY_MAX`, default `4500M`, `'0'` disables, graceful pass-through when `systemd-run` is unavailable or uid≠0) wraps `run_step`'s Popen; `daily_cycle_helpers.runSubprocess` and both `cron-schedule.js` orchestrator spawns go through `wrapCapped` with the same var. Stated trade: an OOM now kills the step (`rc=137`, cycle aborts there) instead of letting the kernel's global OOM killer pick a victim — and only `signals` retries, so a capped `collect` OOM is a new hard-abort path. `OnFailure=openclaw-failure-notify@%n.service` drop-ins installed on the **22** units that genuinely lacked one — the spec's list was stale (it named a non-existent `afterhours-redeploy`, listed eight units that already had it, and missed eleven); the count is drop-in aware (`grep -L` over the unit files returns 38, but 15 already receive it from a `.service.d/onfailure.conf`, and `failure-notify@` itself is excluded). Both template units (`edgar-8k@`, `premarket-scan@`) take the same `%n` form — verified that systemd parses the resulting double-`@` instance name (`LoadState=loaded`). Snapshot in `docs/systemd/`. **E3 — identity.** `src/lib/proc_heartbeat.{py,js}` write `proc:<host>:<pid>` = {argv, step, rss_mb, started_at, updated_at} with a TTL; written by the orchestrator step runner from its EXISTING 30 s poll loop (no new thread — spec §0), by each fleet child once pre-spawn (the driver blocks in `spawnSync` for up to an hour, so a 60 s cadence is impossible there; TTL = per-timeout + 300 s instead), and by the premarket scan. New `proc_registry` system check lists live entries and WARNs on ghosts; new doctor `co_tenant_memory` names every process over 1 GB RSS by argv from `/proc`. The stdout-idle wedge detector was ported from `run_step` to `runSubprocess` — same `STEP_STDOUT_IDLE_MAX_S`, same 600 s default, distinct `rc=125` that `formatAbortAlert` names, so a silent-but-alive child no longer burns the full 9000 s collect budget. **E4 — import allowlist.** `src/strategies/strategy_lint.py` AST-lints candidate files BEFORE the first import, from `validate_strategy.py` (authoritative — `unified_backtest.load_strategy_class` calls `validate()` first) and as a pre-flight in `research-orchestrator.js` (rejects as `import_violation` without paying for the 60 s python spawn). The allowlist is the MEASURED census of all 156 fleet files, not the spec's list, which omitted twelve legitimate roots and rejected `sys` and `os` — used by 137 and 29 files, and `strategycoder.md` MANDATES `print(…, file=sys.stderr)`. Resolution: module allowed, attributes restricted (`os.{environ,path,getenv,…}`, `sys.{stderr,stdout,path,exit,…}`); `open`/`eval`/`exec`/`compile`/`__import__` and 21 write/network method names rejected; `DataFrame.rename` deliberately NOT banned (4 legitimate fleet uses). A test lints the whole fleet and asserts zero violations. It is a guardrail, not a sandbox — root `backtest` is allowlisted and transitively reaches everything. **E5 — live KPIs.** `/api/portfolio/summary` (the spec said `/api/portfolio/stats`; that route does not exist) gains profit factor, expectancy, payoff ratio, median MAE, win/lose days and a 12-month NAV return strip, rendered as a 6-tile row plus the strip on the portfolio page. Every new aggregate repeats the same `status='closed'` + `close_reason IS DISTINCT FROM 'rolled_continuation'` + account-epoch scoping the neighbouring tiles use. MAE is **close-to-close** (`LEAST(MIN(unrealized_pnl_pct), 0)` per signal) and labelled as such: the spec's `trade_daily_marks` is a backtest FUNCTION (`src/backtest/backtest_panel.py:106`), not a live table, so no intraday high/low exists on this side. Read-only SQL, no schema change. Expectancy is arithmetically identical to the existing `avg_realized` and the code says so. **Ops:** requires a user-scope johnbot restart for the JS runner changes (E1/E2/E3/E5) and an operator `systemctl daemon-reload` for the E2 drop-ins; land on a Sat/Sun per spec §6 item 1 so Monday's cycle picks the lot up together. Watch the first live cycle for `[lock] acquired`, `memory_max=`, the `proc:` registry in `python3 -m system_checks --check proc_registry`, and the new tiles on the portfolio page.
```

- [ ] **Step 3: Commit**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add docs/archive/changelog.md
git commit -m "docs(changelog): QuantDinger Stream E — reliability (E1-E5)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

<!-- END TASK 11 -->

---

## Self-review

### Spec coverage (§5 E1–E5 → tasks)

| Spec item | Requirement | Task(s) | Notes |
|---|---|---|---|
| E1 | ONE key `pipeline:run_lock:<date>` used by both twins | 1, 3 | Key lives in `src/lib/run_lock_key.json`; a test in each language asserts both read it. |
| E1 | Value `host:pid:start_iso` | 1, 3 | Identical format, cross-checked by twinned tests. |
| E1 | TTL = step timeout + 120 s, renewed by the step runner | 1, 2, 3 | `ttl_for()`; renew is called INSIDE `run_step` so the bounded `signals` retry renews too. The JS twin does NOT renew (no handle on the lock inside `daily_cycle_node.js`); it acquires at `ttlFor(9000)` — the longest step — and Task 3 says so in the code. The Python orchestrator owns the production cycle. |
| E1 | Takeover if the holder PID is dead on this host | 1, 3 | `manifest_lock.js:65-73` pattern; never touches another host's lock. |
| E1 | Held ⇒ exit rc=75 + `[lock] held by …` + Discord post; remove `return 0` | 2 | `LOCK_BUSY_RC = 75`; posts via the existing `notify(..., channel=…)`. |
| E1 | `--force-resume` keeps its bypass but logs loudly | 2 | Logs + posts the prior holder; writes an OWNED value (the old branch wrote `'1'`). |
| E1 | Tests: acquire/renew/takeover with a fake Redis | 1, 2, 3 | Fake Redis stubs defined in-file; no real Redis anywhere. |
| E2 | `run_step` wraps the child in `systemd-run --scope -p MemoryMax=` | 4 | `src/lib/capped_spawn.py`, `OPENCLAW_STEP_MEMORY_MAX` default `4500M`. |
| E2 | `daily_cycle_helpers.runSubprocess` likewise, via the `capped_spawn.js` contract | 5 | Plus both `cron-schedule.js` orchestrator spawns. |
| E2 | rc=137 maps to the bounded retry | 4 | Unchanged behaviour at `:869-885`; the trade is stated explicitly in Global Constraints. |
| E2 | `OnFailure=` drop-ins on the units that lack them; snapshot into `docs/systemd/` | 6 | 22 verified units (see below); OPERATOR-RUN. |
| E3 | `proc_heartbeat` py + JS twin, `proc:<host>:<pid>` hash, TTL 180 s | 7 | Seven string fields; never raises. |
| E3 | Called by the orchestrator step runner, fleet children, the premarket scan | 7 | Cadence differs per site and the plan says why (fleet driver blocks in `spawnSync`). |
| E3 | `proc_registry` system check; `co-tenant` doctor line naming python > 1 GB | 7 | `requires=['fs']` (the runner has no `redis` dep key; Redis failure ⇒ SKIP). |
| E3 | Port the stdout-idle wedge detector to `runSubprocess` | 8 | Same env var + default; distinct `rc=125`. |
| E4 | `strategy_lint.py` AST allowlist + reject list | 9 | Allowlist = measured census; deviations from the spec's list are argued in the task. |
| E4 | Run before the first import in `research-orchestrator.js` and `validate_strategy.py` | 9 | Two hooks, two distinct jobs; `import_violation` reason code. |
| E4 | Existing fleet lints with zero violations | 9 | `TestFleetIsClean` over all 156 files. |
| E5 | `profit_factor`, `expectancy_pct`, `payoff_ratio`, `mae_median_pct`, `monthly_returns`, `win_days/lose_days` | 10 | On `/api/portfolio/summary`. |
| E5 | Row of tiles + 12-month strip on the portfolio page | 10 | `pf-kpi-row` (6 tiles) + `pf-monthly-strip`. |
| E5 | Read-only SQL, no schema change | 10 | MAE resolved without a column (close-to-close). |
| §0 | Changelog entry, newest first | 11 | Plus the §7 per-stream verification commands. |

Not in scope (other streams): A1–A4, B1–B3, C1–C4, D1–D4.

### Verified `OnFailure`-missing unit list (2026-09-13, drop-in aware)

`afterhours-stop-monitor`, `afterhours-tp-postmarket`, `afterhours-tp-premarket`, `afterhours-tp-rth-reconcile`, `amcheck`, `edgar-8k@`, `fleet-overnight-resume`, `mastermind-critique`, `options-eligibility`, `options-surface-flip`, `premarket-realized-backfill`, `premarket-scan@`, `refresh-universe-sizes`, `research-commit`, `rf-flip`, `sp5-cleanup`, `stop-reattach`, `target-mode-flip`, `universe-recs`, `weekend-maintenance-sat`, `weekend-maintenance-sun`, `weekend-sunday` — **22 units**, all prefixed `openclaw-` and suffixed `.service`. Task 6 Step 1 re-derives this set and stops if it differs.

### Placeholder scan

No `TODO`, `FIXME`, `...`, `<placeholder>`, `# implement`, or "same as above" stands in for code anywhere in this plan. Every code block is complete and pasteable. Deliberate repetition: the `FakeRedis` stub appears in three test files (Tasks 1, 2, 7) with a slightly different surface each time (`set/get/delete/expire` vs. `+setex/publish` vs. `hset/expire/delete`) — each is the minimum that file needs, and sharing them across files would couple unrelated tests.

### Signature consistency

- `run_step` accretes exactly two parameters across the plan: `renew: Callable[[int], None]` (Task 2 Step 5, takes the step timeout) and `heartbeat: Callable[[int, str], None]` (Task 7 Step 8, takes `(child_pid, started_at_iso)`). Final signature `run_step(script, run_date, env, renew=None, heartbeat=None)`. Both default to `None`, and `grep` confirmed no test calls `run_step` directly, so nothing outside `main()` breaks. `started_at` is captured once per `run_step` call, not per tick — otherwise it would always equal `updated_at` and carry no information.
- `runSubprocess` accretes exactly two options: `memoryMax` (Task 5) and `stdoutIdleMaxSec` (Task 8). Task 8 restates the WHOLE function so the two edits cannot conflict. Return shape grows `memoryMax` (Task 5) then `wedged` (Task 8); `daily_cycle_node.js:59` reads only `rc`/`stdout`/`stderrTail`, and the four pre-existing tests in `test_daily_cycle_helpers.test.js` read only `rc`/`stdout`/`stderrTail`/`durationMs`/`timedOut`.
- `daily_cycle_helpers` exports grow twice: `stepMemoryMax` (Task 5), `stdoutIdleMaxSec` (Task 8). Task 8 restates the full `module.exports`.
- `run_lock` twins: `acquire/renew/release/lock_key/make_value/parse_value/ttl_for/pid_alive` — Python returns tuples (`(ok, holder)`), JS returns `{ok, value, holder}`. Deliberate: idiomatic in each language, and the twinned tests pin both. `_acquireRunLock` returns `{value, release}` only — no `renew`, because nothing in the JS graph could call it (see the E1 row above); an unused closure there would be exactly the kind of dead code this plan claims not to contain.
- `proc_heartbeat` twins: Python `write(r, *, step, argv, pid, host, started_at, ttl_s)`, JS `writeHeartbeat(r, {step, argv, pid, host, startedAt, ttlSec})`. Same key, same seven fields, same defaults.
- Everything referenced exists or is created here: `_resolve_script` (`pipeline_orchestrator.py:471`), `notify(msg, channel=…)` (`:265`), `post_channel` (`:343`), `wrapCapped` + `_internals.reset` (`capped_spawn.js:63,73`), `_spawnPython` (`research-orchestrator.js:233`) + `paperIdForCandidate` (imported `:23`), `_loadOhlcStore` (`server.js:2550`), `_accountEpoch` (`server.js:552`), `all_checks()` (`system_checks/registry.py:37`), `_check/_ok/_warn` (`doctor.py:85,99,103`), `@check(name, tags, requires)` (`system_checks/registry.py:23`), `formatAbortAlert` (`daily_cycle_helpers.js:87`).

### Line-number drift found against the spec (re-verified 2026-09-13 on `d5e6c235`)

| Spec citation | Actual |
|---|---|
| `release_lock` "only in `finally` (:826)" | FIVE sites: `:826, :840, :901, :939, :970`. All go through the value-checked helper. |
| `run_step` renew point `:603` | `run_step` starts at `:580`; `_resolve_script` returns at `:592`; the bare `Popen` is at `:603`. Renew goes at `:592`, not `:603`. |
| bounded retry `:871-882` | `:869-885`. |
| "already running" `:779-782` | `:779-782` confirmed; the surrounding comment `:775-778` wrongly says the key is `pipeline:lock:{date}` (it is `pipeline:running:`). Both replaced. |
| JS lock `daily-cycle.js:141-143` | `_acquireRunLock` spans `:137-155`; the `SET NX EX 7200` is at `:143`. |
| `manifest_lock.js:25` PID pattern | The prose is at `:38`; the code (`_isProcessAlive`) is at `:63-73`. |
| stdout-idle wedge `:600-633` | `:592-633` (the `stdout_idle_max_s` read is at `:598`). |
| `validate_strategy.py:80-87` | The lint hook belongs at `:70` (after `abs_path`, before `module_name` (`:71-77`) and `importlib.import_module` (`:80`)). |
| `research-orchestrator.js:1041` | `_runValidateStrategy` is defined at `:1039`; the CALL that must be preceded by the lint is `:1299` in `_runGateChain`. |
| `registry.py:400-450` "how strategy modules are imported" | `_discover_impl` `:402-425`, `load_strategy_class` `:428-446`. Untouched by this stream — `validate()` is the choke point. |
| `unified_backtest.py:185-225` | `_is_strategy_class` `:185`, `load_strategy_class` `:200` — and it calls `validate(filepath)` at `:202`, which is why one lint hook covers this path. |
| portfolio stats `server.js:1225-1269` | Route `/api/portfolio/summary` at `:1166`; the stats SQL is `:1224-1246`; the response object `:1258-1280`. There is no `/api/portfolio/stats`. |
| backtest stats `:1590-1600` | `:1583-1616` (`strategy_backtest_runs` / `_regimes`). Read for context only; unchanged. |
| `trade_daily_marks` in `src/database/migrations` | **Does not exist.** It is a function at `src/backtest/backtest_panel.py:106`. See Task 10. |
| `bench_realized.py:20-45` | `NAV_HISTORY_PATH` `:24`, `load_nav_history` `:39-42`. The dashboard reads the same file through `_loadOhlcStore` (`server.js:2546-2556`), which is what Task 10 uses. |
| "20 units" needing `OnFailure=` | **22**, and the spec's names are stale in three ways (see Task 6). |
| E4 allowlist | Spec omits 12 legitimate roots and rejects `sys`/`os`, which 137/29 fleet files use and the strategycoder prompt mandates. Resolved by an attribute-level policy. |
| `strategycoder.md:85-115` "prompt-only import rules" | The import guidance is at `:78-98`; the `Rules:` list is `:106-118` (`No naked class-body imports` at `:109`). The new lines go in the `Rules:` list. |
| system-checks `requires` keys | README lists `fs/db/broker/llm/discord`; the runner also accepts `systemd` (`runner.py:34`). There is **no `redis` key** — `proc_registry` uses `requires=['fs']` and SKIPs on a Redis error. |
| `tests/test_system_checks_framework.py` (README) | Lives at `tests/system_checks/test_system_checks_framework.py`. |
| JS test naming in `tests/lib/` | Local convention is `<module>.test.js` (`capped_spawn.test.js`), not `test_<module>.test.js`. Followed. |

### Ambiguities resolved (choices stated)

1. **E4 `os`/`sys`.** Spec says reject both; the fleet uses both legitimately and the strategycoder prompt mandates `sys.stderr`. **Chosen:** allow the modules, restrict the attributes to the census-verified set. Rejecting them would fail the "zero violations" test the spec itself requires.
2. **E5 MAE source.** `trade_daily_marks` is not a table. **Chosen:** close-to-close MAE from `signal_pnl.unrealized_pnl_pct`, no schema change, tile labelled with the limitation.
3. **E3 heartbeat cadence.** "Every 60 s" is unimplementable in a `spawnSync` driver. **Chosen:** poll-loop cadence where a loop exists (orchestrator), one pre-spawn write with a covering TTL where it does not (fleet driver).
4. **E1 lock release.** Spec is silent on release semantics once takeover exists. **Chosen:** value-checked release at all five Python sites and in the JS twin — an unconditional `delete` would let a taken-over process delete the new owner's lock.
5. **E1 initial TTL.** Spec says "TTL = current step's timeout + 120 s" but the lock is acquired before any step runs. **Chosen:** acquire with `ttl_for(max step timeout of the effective step list)`, then renew per step.
6. **E2 cap variable.** Reusing `OPENCLAW_BACKTEST_MEMORY_MAX` would couple cycle steps to research children. **Chosen:** a distinct `OPENCLAW_STEP_MEMORY_MAX`, same `4500M` default.
7. **E2 unit list.** **Chosen:** the drop-in-aware 22, with a re-derivation guard as the task's first step.
8. **E4 `from os import system`.** An allowlist keyed on the import ROOT lets `from os import system` bind the name locally, where the attribute policy never sees it. **Chosen:** the `ImportFrom` branch applies the same attribute allowlist to the imported names when the module is the bare root. Verified against the census (zero `from os` / `from sys` lines in the fleet), so `TestFleetIsClean` stays green.
9. **E5 NAV epoch.** The spec scopes the SQL to the account epoch and says nothing about the NAV store, which has no epoch awareness of its own. **Chosen:** filter `logs/pnl_daily_ohlc.json` by the same epoch before computing the strip and the day counts — the 2026-09-08 fake +8.75 % jump was exactly this gap on the P&L chart, and `_buildCandles` already carries the fix.
10. **E1 renewal on the JS side.** The spec says both twins renew; `daily_cycle_node.js` has no handle on the lock and threading one through the LangGraph state is out of proportion. **Chosen:** the JS runner acquires at the longest step's TTL and does not renew; the Python orchestrator — which owns the production cycle — renews per step. Stated in the code and in the coverage table rather than papered over.
11. **Task count.** The prompt expected ~9–12; this plan has 11, matching the prompt's own enumeration (E1×3, E2×3, E3×2, E4×1, E5×1, changelog×1).

