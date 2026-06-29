"""Tests for doctor.run(only=...) check-name filter.

Added in the W1 reconcile pass so systemd ExecStartPre gates (e.g. the
options-archive preflight) can require only the checks relevant to that job,
instead of the full --required-only sweep that fails on unrelated subsystem
drift. alpaca_cli_binary is a pure local filesystem check (no network), so
these tests need no mocks.

Run:
    pytest tests/test_doctor_only_filter.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import doctor  # noqa: E402


def test_only_runs_just_named_check():
    results, _ = doctor.run(only={'alpaca_cli_binary'})
    assert [r['name'] for r in results] == ['alpaca_cli_binary']


def test_only_unknown_name_runs_nothing_and_passes():
    results, code = doctor.run(only={'no_such_check'})
    assert results == []
    assert code == 0


def test_only_overrides_quick_slow_skip():
    # an explicitly-named check runs even under quick=True (no implicit skip)
    results, _ = doctor.run(only={'alpaca_cli_binary'}, quick=True)
    assert [r['name'] for r in results] == ['alpaca_cli_binary']
