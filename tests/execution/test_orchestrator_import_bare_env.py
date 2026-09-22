"""Regression: the orchestrator (and everything that imports it) must import
under a BARE environment — no PYTHONPATH — because the pre-market scan unit
(`openclaw-premarket-scan@.service`) runs `python3 src/pipeline/run_premarket_scan.py`
with only `EnvironmentFile=/root/openclaw/.env`, which sets no PYTHONPATH.

2026-09-16..22: wave 1 added `from lib import run_lock` to
`src/execution/pipeline_orchestrator.py`; `lib` lives under `<ROOT>/src`, which
the module put on `sys.path` only via pytest/johnbot, so both pre-market panic
scans failed with ModuleNotFoundError on every trading day for a week.

Run: python3 -m pytest tests/execution/test_orchestrator_import_bare_env.py -q
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _bare_import(module: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH',)}
    env['OPENCLAW_STEP_MEMORY_MAX'] = '0'
    return subprocess.run(
        [sys.executable, '-c', f'import {module}'],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
    )


def test_orchestrator_imports_without_pythonpath():
    r = _bare_import('src.execution.pipeline_orchestrator')
    assert r.returncode == 0, r.stderr[-800:]


def test_premarket_helpers_import_without_pythonpath():
    r = _bare_import('src.pipeline.premarket_helpers')
    assert r.returncode == 0, r.stderr[-800:]


# QD E3 fix round 1 (this branch only — proc_heartbeat doesn't exist on
# main): `run_premarket_scan.main()` does `from lib import proc_heartbeat`,
# deferred inside the function body rather than at module import time, so
# merely importing `src.pipeline.run_premarket_scan` (as the two tests above
# import their modules) does NOT exercise that statement — it only proves
# the module *parses* under a bare environment. This test explicitly imports
# both `src.pipeline.run_premarket_scan` (which, via premarket_helpers,
# transitively imports pipeline_orchestrator and so runs the sys.path fix
# above) AND `lib.proc_heartbeat` in the same bare-environment subprocess,
# proving the fix actually reaches run_premarket_scan's own heartbeat call
# site, not just the module that carries the fix.
def test_premarket_scan_and_proc_heartbeat_import_without_pythonpath():
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH',)}
    env['OPENCLAW_STEP_MEMORY_MAX'] = '0'
    r = subprocess.run(
        [sys.executable, '-c',
         'import src.pipeline.run_premarket_scan; import lib.proc_heartbeat'],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr[-800:]
