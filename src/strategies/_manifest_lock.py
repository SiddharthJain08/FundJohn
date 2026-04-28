"""
_manifest_lock.py — Python counterpart to src/lib/manifest_lock.js.

Both implementations MUST agree on:
  - Lockfile path:    `<target>.lock`
  - Lockfile content: 3 lines = "<PID>\\n<ISO timestamp>\\n<actor>\\n"
  - Acquire mode:     atomic O_EXCL creation
  - Stale detection:  mtime > 60s OR PID not alive
  - Atomic write:     write `<target>.tmp` then `os.replace(tmp, target)`

If you change the contract here, update manifest_lock.js too.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

LOCK_TIMEOUT_S    = 60.0    # stale-lock cutoff
ACQUIRE_TIMEOUT_S = 30.0    # give up acquiring after 30s
POLL_BASE_S       = 0.025
POLL_MAX_S        = 0.5


def _lock_path(target_path: str | Path) -> str:
    return str(target_path) + ".lock"


def _is_process_alive(pid: int) -> bool:
    if not pid or pid <= 1:
        return False
    try:
        os.kill(pid, 0)   # signal 0 = check existence
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True       # exists, just not ours
    except OSError:
        return False


def _read_lock_meta(lockfile: str) -> Optional[dict]:
    try:
        st = os.stat(lockfile)
        with open(lockfile, "r", encoding="utf-8") as f:
            txt = f.read()
        parts = txt.split("\n")
        pid_str = parts[0] if parts else ""
        ts      = parts[1] if len(parts) > 1 else ""
        actor   = parts[2] if len(parts) > 2 else "?"
        return {
            "pid":      int(pid_str) if pid_str.isdigit() else 0,
            "ts":       ts,
            "actor":    actor or "?",
            "mtime_s":  st.st_mtime,
        }
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None


def _maybe_clear_stale_lock(lockfile: str) -> bool:
    meta = _read_lock_meta(lockfile)
    if not meta:
        return False
    age_s = time.time() - meta["mtime_s"]
    stale = age_s > LOCK_TIMEOUT_S or not _is_process_alive(meta["pid"])
    if not stale:
        return False
    try:
        os.unlink(lockfile)
        import sys
        sys.stderr.write(
            f"[_manifest_lock] cleared stale lock at {lockfile} "
            f"(age={age_s:.0f}s, pid={meta['pid']}/{meta['actor']}, "
            f"alive={_is_process_alive(meta['pid'])})\n"
        )
        return True
    except OSError:
        return False


def _acquire_lock(target_path: str | Path, *, actor: str = "unknown",
                  timeout_s: float = ACQUIRE_TIMEOUT_S) -> Callable[[], None]:
    """Acquire the lock; returns a release callable. Raises TimeoutError on
    timeout."""
    lockfile = _lock_path(target_path)
    Path(lockfile).parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout_s
    backoff  = POLL_BASE_S

    while True:
        try:
            fd = os.open(lockfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                ts = datetime.now(timezone.utc).isoformat()
                payload = f"{os.getpid()}\n{ts}\n{actor}\n".encode()
                os.write(fd, payload)
            finally:
                os.close(fd)

            def release() -> None:
                try:
                    os.unlink(lockfile)
                except OSError:
                    pass
            return release

        except FileExistsError:
            _maybe_clear_stale_lock(lockfile)
            if time.time() > deadline:
                meta = _read_lock_meta(lockfile)
                held_by = (
                    f"pid={meta['pid']} actor={meta['actor']} ts={meta['ts']}"
                    if meta else "unknown"
                )
                raise TimeoutError(
                    f"manifest_lock acquire timed out after {timeout_s}s; "
                    f"held by {held_by}"
                )
            time.sleep(backoff)
            backoff = min(backoff * 2, POLL_MAX_S)


@contextlib.contextmanager
def manifest_lock(target_path: str | Path, *, actor: str = "unknown",
                  timeout_s: float = ACQUIRE_TIMEOUT_S) -> Iterator[None]:
    """Context manager. Acquires the lock; releases on exit (success or
    exception). Use this around the entire read-modify-write cycle."""
    release = _acquire_lock(target_path, actor=actor, timeout_s=timeout_s)
    try:
        yield
    finally:
        release()


def write_atomic(target_path: str | Path, payload: Any, *, indent: int = 2,
                 encoding: str = "utf-8") -> None:
    """Atomic JSON write: write to <target>.tmp then rename. Caller MUST hold
    the manifest_lock when calling this; otherwise the read-modify-write
    cycle is racy even though the write itself is atomic."""
    target = Path(target_path)
    tmp = Path(str(target) + ".tmp")
    tmp.write_text(json.dumps(payload, indent=indent), encoding=encoding)
    os.replace(tmp, target)


def read_under_lock(target_path: str | Path, *, actor: str = "reader",
                    timeout_s: float = ACQUIRE_TIMEOUT_S) -> dict:
    """Read + parse JSON under lock. Useful when a caller needs a coherent
    snapshot without write intent."""
    with manifest_lock(target_path, actor=actor, timeout_s=timeout_s):
        with open(target_path, "r", encoding="utf-8") as f:
            return json.load(f)


def with_manifest_lock(target_path: str | Path, mutator: Callable[[dict], Optional[dict]],
                       *, actor: str = "unknown", timeout_s: float = ACQUIRE_TIMEOUT_S) -> dict:
    """High-level read-modify-write convenience. Equivalent to:

        with manifest_lock(target_path):
            data = json.load(open(target_path))
            result = mutator(data) or data
            write_atomic(target_path, result)

    Returns the written payload."""
    with manifest_lock(target_path, actor=actor, timeout_s=timeout_s):
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        result = mutator(data)
        if result is None:
            result = data
        write_atomic(target_path, result)
        return result
