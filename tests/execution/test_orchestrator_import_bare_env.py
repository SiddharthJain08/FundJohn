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
