"""memory_footprint.py — RSS + co-tenant diagnostics for pipeline steps.

Why: the signals step logs its peak RSS + co-resident processes at DONE
(engine.py:_memory_footprint) because OOM post-mortems from kernel dumps
only name processes that survive to the kill. That helped when the ENGINE
survived — but when a step is itself the OOM victim (handoff, 2026-08-12
14:09Z, rc=137), its DONE line never prints and the co-tenant escapes
unidentified again (2.39GB python3, second occurrence after 08-05).

So steps that can be the victim need PERIODIC samples: a daemon thread
emitting a footprint line every few seconds means the step log retains a
recent snapshot of who shared the box even when the step dies mid-run.

engine.py keeps its own local copy of the footprint formatter (the live
signals path is deliberately untouched); consolidate on a quiet day.

Best-effort throughout — diagnostics must never fail or slow a run.
"""

from __future__ import annotations

import os
import threading


def memory_footprint() -> str:
    """' peak_rss=1.9GB avail=3.1GB | co-tenants: node 217MB, uvicorn 487MB'.

    Mirrors engine.py:_memory_footprint. Never raises.
    """
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
        avail = None
        try:
            for line in open('/proc/meminfo'):
                if line.startswith('MemAvailable:'):
                    avail = int(line.split()[1]) / 1024 / 1024
                    break
        except OSError:
            pass
        others = []
        me = os.getpid()
        for pid_dir in os.listdir('/proc'):
            if not pid_dir.isdigit() or int(pid_dir) == me:
                continue
            try:
                with open(f'/proc/{pid_dir}/statm') as f:
                    rss_mb = int(f.read().split()[1]) * 4096 / 1048576
                if rss_mb < 150:          # only things big enough to matter
                    continue
                with open(f'/proc/{pid_dir}/comm') as f:
                    others.append((rss_mb, f.read().strip(), pid_dir))
            except (OSError, ValueError, IndexError):
                continue
        others.sort(reverse=True)
        tail = ', '.join(f'{n}[{p}] {r:.0f}MB' for r, n, p in others[:5]) or 'none >150MB'
        av = f' avail={avail:.1f}GB' if avail is not None else ''
        return f' peak_rss={peak:.1f}GB{av} | co-tenants: {tail}'
    except Exception:
        return ''


def start_periodic_logger(tag: str, interval_s: float = 15.0) -> None:
    """Emit '<tag> memwatch<footprint>' every interval_s from a daemon
    thread. The thread dies with the process; there is nothing to stop or
    join. Output goes to stdout (flush=True) so step-log capture keeps the
    last sample even when the process is SIGKILLed."""
    def _loop() -> None:
        import time
        while True:
            try:
                print(f'{tag} memwatch{memory_footprint()}', flush=True)
            except Exception:
                pass
            time.sleep(interval_s)
    try:
        threading.Thread(target=_loop, daemon=True, name='memwatch').start()
    except Exception:
        pass
