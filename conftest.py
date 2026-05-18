"""Repo-wide pytest fixtures + isolation guards.

Why this exists: several production scripts call `dotenv.load_dotenv()`
from inside their `main()` function so the CLI inherits ALPACA / POSTGRES
creds when invoked outside systemd. When a test calls that `main()`
(e.g. tests/test_dry_run_dataflow.py::test_dry_run_skips_parquet_merge
invokes `alpaca_corporate_actions.main()`), load_dotenv populates
`os.environ` for the REST OF THE PYTEST SESSION — that pollution makes
production env gates like OPENCLAW_SHARPE_CADENCE_SIZER=1 appear true
in subsequent tests, routing the regime_blended_sizer through the
DB-reading sharpe_cadence path and breaking 9 unit tests that mock the
input-signal-only independent path.

Rather than chase every script and conditionally guard each
load_dotenv, this conftest snapshots os.environ before each test and
restores it after. Cheaper than rewriting 5+ production CLIs, and
makes the test suite robust to future env-mutating tests.

The trade-off: tests that *intend* to set env vars (e.g. patch.dict
with clear=False) must use the patch.dict context manager — which
already handles its own teardown correctly. This guard adds a
second layer that catches any leak via raw os.environ.__setitem__.
"""
from __future__ import annotations

import os
import pytest


@pytest.fixture(autouse=True)
def _restore_os_environ():
    """Snapshot os.environ before each test; restore after.

    autouse=True means every test gets isolated automatically — no
    per-test opt-in needed. The snapshot is shallow but os.environ
    values are strings (immutable), so a shallow copy is sufficient.
    """
    saved = dict(os.environ)
    try:
        yield
    finally:
        # Remove any keys added during the test
        for k in list(os.environ):
            if k not in saved:
                del os.environ[k]
        # Restore any keys mutated during the test
        for k, v in saved.items():
            if os.environ.get(k) != v:
                os.environ[k] = v
