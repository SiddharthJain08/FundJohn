"""Neutralize prod-state env leakage for system-check tests.

Production .env carries OPENCLAW_SAMEDAY_SIGNAL_TARGET=1 (§8, 2026-08-06)
and import-time load_dotenv during collection pulls it into os.environ. The
signal_target_mode resolver lets it win over the legacy
OPENCLAW_EOD_SIGNAL_REGISTER, so pre-§8 tests that enable the T+1 mode via
the legacy flag alone would silently stay in same-day mode (13 combined-run
failures, 2026-08-06). Same pattern as tests/execution/conftest.py.
"""
import pytest


@pytest.fixture(autouse=True)
def _clear_sameday_signal_target(monkeypatch):
    monkeypatch.delenv('OPENCLAW_SAMEDAY_SIGNAL_TARGET', raising=False)
