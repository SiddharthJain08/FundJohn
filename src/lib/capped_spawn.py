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
import re
import subprocess

DEFAULT_MEMORY_MAX = '4500M'
MEMORY_MAX_ENV     = 'OPENCLAW_STEP_MEMORY_MAX'

# A well-formed systemd size string: a bare '0' (our own disables-the-cap
# sentinel) or digits followed by a K/M/G/T unit. A bare non-zero number
# (e.g. '4500') is the footgun this guards against: systemd-run reads an
# unsuffixed MemoryMax= as raw BYTES, which would OOM-kill every step at
# spawn. Case-insensitive on the unit letter.
_CAP_RE = re.compile(r'^(?:0|\d+[KMGT])$', re.IGNORECASE)


def _real_probe() -> bool:
    try:
        r = subprocess.run(
            ['systemd-run', '--scope', '--collect', '--quiet',
             '-p', 'MemoryMax=64M', '--', '/bin/true'],
            timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return r.returncode == 0
    except Exception:
        return False


_STATE = {'probe': _real_probe, 'uid': os.getuid, 'available': None, 'warned': False,
          'cap_warned': False}


def _validate_cap(cap: str, log=None) -> str:
    """Return `cap` if it matches `_CAP_RE`; otherwise fall back to
    DEFAULT_MEMORY_MAX and log a warning ONCE per process (shared with the
    availability warning's once-only discipline, tracked separately so a
    malformed-cap warning and an unavailable-scopes warning don't suppress
    each other)."""
    if _CAP_RE.match(cap):
        return cap
    if not _STATE['cap_warned']:
        _STATE['cap_warned'] = True
        msg = (f'[capped_spawn] malformed {MEMORY_MAX_ENV}={cap!r} (no unit '
               f'suffix — systemd-run would read this as raw bytes); '
               f'falling back to default {DEFAULT_MEMORY_MAX}')
        (log or print)(msg)
    return DEFAULT_MEMORY_MAX


def _resolve_env_cap(log=None) -> str:
    v = os.environ.get(MEMORY_MAX_ENV)
    if v is None or not str(v).strip():
        return DEFAULT_MEMORY_MAX
    return _validate_cap(str(v).strip(), log=log)


def default_memory_max() -> str:
    return _resolve_env_cap()


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
    # Env-sourced cap: route through `_resolve_env_cap(log=log)` (not the
    # bare `default_memory_max()`) so a malformed OPENCLAW_STEP_MEMORY_MAX
    # warns into the CALLER's log (run_step's cycle log on the real path)
    # instead of only reaching raw stdout — `default_memory_max()` itself
    # has no `log` param (frozen public signature) and falls back to
    # print(), which a direct caller of that function still gets.
    if memory_max is None:
        cap = _resolve_env_cap(log=log)
    else:
        # Explicit memory_max bypasses the env path entirely and has not
        # been validated yet — same guard, same caller `log`. Already-valid
        # input matches _CAP_RE and returns unchanged (no-op). A blank
        # value stays blank (disables below) rather than being flagged as
        # malformed — blank means "no cap requested", not "bad cap".
        cap = str(memory_max or '').strip()
        if cap:
            cap = _validate_cap(cap, log=log)
    plain = list(cmd)
    if not cap or cap == '0':
        return plain, None
    if not _available(log=log):
        return plain, None
    return (['systemd-run', '--scope', '--collect', '--quiet',
             '-p', f'MemoryMax={cap}', '--', *plain], cap)


def _reset(available=None, probe=None, uid=None):
    """Test hook: pin the availability decision; None restores the real
    probe/uid. `probe=`/`uid=` let a test inject a fake probe function or
    uid getter while leaving `available` unresolved (None) so `_available()`
    actually calls them — used to test probe caching and uid gating without
    ever invoking a real systemd-run. `_reset()` / `_reset(available=...)`
    with no `probe`/`uid` behave exactly as before (real probe, real uid)."""
    _STATE['probe'] = probe if probe is not None else _real_probe
    _STATE['uid'] = uid if uid is not None else os.getuid
    _STATE['available'] = available
    _STATE['warned'] = False
    _STATE['cap_warned'] = False
