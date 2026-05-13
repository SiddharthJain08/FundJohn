"""system_checks — runnable diagnostic probes for live OpenClaw state.

These are NOT unit tests. They check that the production system is
functioning correctly *right now*: pipeline ran, signals persisted with
clean geometry, Alpaca session works, regime gate eligible at least one
strategy, etc.

Tagged by domain (`pipeline` / `broker` / `regime` / `strategies` /
`agents` / `storage`). Skips gracefully when deps are unavailable
(POSTGRES_URI not set → DB checks SKIP, not FAIL).

Usage (CLI):
    python3 -m system_checks                       # run all
    python3 -m system_checks --tag pipeline broker # run subset
    python3 -m system_checks --check pipeline_completed_today
    python3 -m system_checks --json                # machine-readable

Usage (library):
    from system_checks import run_one, run_all, registry
    results = run_all(tags=['pipeline'])
"""
from __future__ import annotations

from .registry import check, all_checks, get
from .runner import run_one, run_all, summarize
from .types import CheckResult, Status

# Importing the checks subpackage triggers @check registration as a side
# effect — every module in checks/ that imports `check` and decorates a
# function adds it to the registry on import.
from . import checks as _checks  # noqa: F401

__all__ = [
    'check', 'all_checks', 'get',
    'run_one', 'run_all', 'summarize',
    'CheckResult', 'Status',
]
