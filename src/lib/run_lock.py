"""run_lock.py — one owned, renewed daily-cycle run lock (QD spec §5 E1).

Before this module there were TWO locks and neither was owned:
  * Python  `pipeline:running:<date>` = '1'                      (pipeline_orchestrator.py:38)
  * JS      `engine:run_lock:<date>`  = 'daily-cycle:<pid>:<ms>'  (src/agent/graphs/daily-cycle.js:141)
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

Real safety envelope — no path here is a blind overwrite:
  * acquire  — `SET NX EX`. Only ever writes a key that was genuinely absent.
  * takeover — a Lua GET+SET compare-and-swap against the exact holder value
    we just observed dead. If that value has since changed (another same-
    host racer's CAS already won, or the real owner renewed in time), the
    CAS is a no-op and we report failure. The only remaining window is a
    holder that dies strictly between our GET and our CAS — and the CAS
    itself closes that window: at most one takeover per dead holder value
    can ever succeed, because the second CAS to run sees the first CAS's
    new value, not the stale one it was conditioned on.
  * renew    — `SET NX` to re-take a key that expired under us (never
    clobbers a key some other launcher's `acquire` legitimately grabbed in
    that same gap), else an owner-checked `EXPIRE` whose real return value
    is what renew reports.
  * release  — value-checked `DELETE`; never removes a key we no longer own.

`acquire` and `renew` propagate Redis errors — a lock decision must fail
closed, never guess. `release` swallows them: it runs on the way out
(typically from a `finally`) and must never mask the run's real exit status.
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

# Compare-and-swap used only for takeover: write `new` (with `ttl`) iff the
# key's current value still equals the dead-PID value we observed via GET.
# Atomic in real Redis (single EVAL); FakeRedis in the test suite emulates
# this exact script (see tests/lib/test_run_lock.py).
_TAKEOVER_CAS_SCRIPT = (
    "if redis.call('GET', KEYS[1]) == ARGV[1] then "
    "return redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3]) "
    "else return nil end"
)


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
    (possibly None if it is still absent after the one vanished-key retry
    below — vanishingly unlikely, and never retried beyond that once).

    Propagates whatever the Redis client raises: acquiring must fail closed.
    A connection error must never be read as "lock is free" or "lock is
    busy" — only the caller, treating the exception as fatal, gets to decide
    what "we don't know" means for the run.
    """
    key = lock_key(run_date)
    me_host = host or socket.gethostname()
    value = value or make_value(host=me_host)
    ttl = int(ttl_s)

    if r.set(key, value, nx=True, ex=ttl):
        return True, value

    holder_raw = _text(r.get(key))
    if holder_raw is None:
        # The key vanished between our SET NX (which found it present) and
        # this GET — its TTL lapsed, or its owner released it. That is
        # "free", not "busy": retry the NX once rather than reporting a
        # live holder that no longer exists.
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
        if r.eval(_TAKEOVER_CAS_SCRIPT, 1, key, holder_raw, value, int(ttl)):
            return True, value
        # Someone else's CAS (another same-host racer, or the real owner
        # renewing) won the gap between our GET and this eval. We lost the
        # race cleanly — report whoever holds it now.
        return False, _text(r.get(key))

    return False, holder_raw


def renew(r, run_date, value, ttl_s, *, log=None) -> bool:
    """Extend OUR lock's TTL. False (and no write) if someone else owns it.

    Propagates whatever the Redis client raises: renewing must fail closed
    — the caller has to treat an exception here as "we no longer know we
    hold the lock," never assume the run is still safely exclusive just
    because renew did not explicitly return False.
    """
    if not value:
        return False
    key = lock_key(run_date)
    ttl = int(ttl_s)
    cur = _text(r.get(key))
    if cur is None:
        # Expired under us (a step outran its own TTL). NX makes the re-take
        # conditional on the key still being absent: if another launcher's
        # acquire() won the same gap, its write stands and we fall through
        # to "held by someone else" below instead of clobbering it.
        if r.set(key, value, nx=True, ex=ttl):
            if log:
                log(f'[lock] {key} had expired — re-taken by {value} (ttl={ttl}s)')
            return True
        cur = _text(r.get(key))
    if cur != value:
        if log:
            log(f'[lock] renew skipped — {key} held by {cur}, not us ({value})')
        return False
    return bool(r.expire(key, ttl))


def release(r, run_date, value, *, log=None) -> bool:
    """Delete OUR lock. Never deletes a lock another owner took over.

    Swallows any Redis error instead of raising: release runs on the way
    out (typically a `finally`), and a network hiccup here must never mask
    or replace the run's real exit status. Logs and reports False instead.
    """
    if not value:
        return False
    key = lock_key(run_date)
    try:
        cur = _text(r.get(key))
        if cur is None:
            return False
        if cur != value:
            if log:
                log(f'[lock] release skipped — {key} held by {cur}, not us ({value})')
            return False
        r.delete(key)
        return True
    except Exception as exc:  # noqa: BLE001 - must never raise; see docstring.
        if log:
            log(f'[lock] release of {key} failed: {exc!r}')
        return False
