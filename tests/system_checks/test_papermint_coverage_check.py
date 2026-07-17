"""SP-2 Phase D — papermint_predicate_coverage system_check tests.

Fast and deterministic: no real DB writes, no LLM calls.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 1. Gate off → check returns PASS (or SKIP if POSTGRES_URI not set), exit 0
# ---------------------------------------------------------------------------

def test_check_gate_off_passes(monkeypatch):
    """With OPENCLAW_PHASE_D_PREDICATE_AT_MINT unset, check must return PASS/SKIP, exit 0."""
    env = {**os.environ}
    env.pop('OPENCLAW_PHASE_D_PREDICATE_AT_MINT', None)
    r = subprocess.run(
        [sys.executable, '-m', 'src.system_checks',
         '--check', 'papermint_predicate_coverage', '--json'],
        capture_output=True, text=True, timeout=30,
        cwd=str(REPO_ROOT), env=env,
    )
    assert r.returncode == 0, (
        f"Expected returncode 0, got {r.returncode}.\n"
        f"stdout: {r.stdout!r}\nstderr: {r.stderr!r}"
    )
    data = json.loads(r.stdout)
    results = data.get('results', [])
    assert results, f"Expected at least one result, got: {data}"
    status = results[0]['status']
    assert status in ('PASS', 'SKIP'), (
        f"Expected PASS or SKIP when gate off, got {status!r}. detail: {results[0].get('detail')}"
    )


# ---------------------------------------------------------------------------
# 2. Gate on + no POSTGRES_URI → SKIP (dep not available), exit 0
# ---------------------------------------------------------------------------

def test_check_gate_on_no_db_skips(monkeypatch):
    """With gate ON but no POSTGRES_URI, check must SKIP (dep unavailable), exit 0."""
    env = {k: v for k, v in os.environ.items() if k != 'POSTGRES_URI'}
    env['OPENCLAW_PHASE_D_PREDICATE_AT_MINT'] = '1'
    r = subprocess.run(
        [sys.executable, '-m', 'src.system_checks',
         '--check', 'papermint_predicate_coverage', '--json'],
        capture_output=True, text=True, timeout=30,
        cwd=str(REPO_ROOT), env=env,
    )
    assert r.returncode == 0, (
        f"Expected returncode 0, got {r.returncode}.\n"
        f"stdout: {r.stdout!r}\nstderr: {r.stderr!r}"
    )
    data = json.loads(r.stdout)
    results = data.get('results', [])
    assert results, f"Expected at least one result, got: {data}"
    status = results[0]['status']
    assert status == 'SKIP', (
        f"Expected SKIP when no POSTGRES_URI, got {status!r}. detail: {results[0].get('detail')}"
    )


# ---------------------------------------------------------------------------
# 3. Check is registered in the registry
# ---------------------------------------------------------------------------

def test_check_registered():
    """papermint_predicate_coverage must appear in the check registry."""
    r = subprocess.run(
        [sys.executable, '-m', 'src.system_checks', '--list', '--json'],
        capture_output=True, text=True, timeout=30,
        cwd=str(REPO_ROOT),
    )
    assert r.returncode == 0, f"--list failed: {r.stderr!r}"
    registry = json.loads(r.stdout)
    assert 'papermint_predicate_coverage' in registry, (
        f"Check not found in registry. Keys: {list(registry.keys())}"
    )
    meta = registry['papermint_predicate_coverage']
    assert 'agents' in meta['tags'], f"Expected 'agents' tag, got {meta['tags']}"
    assert 'strategies' in meta['tags'], f"Expected 'strategies' tag, got {meta['tags']}"
    assert 'db' in meta['requires'], f"Expected 'db' requirement, got {meta['requires']}"
