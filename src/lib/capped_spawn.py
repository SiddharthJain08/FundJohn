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
