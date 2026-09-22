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
