"""Deterministic surfaces for sizer end-to-end tests (2026-07-27).

Modules under src/execution load .env at import, so pytest on the prod box
reaches the REAL Postgres + derived artifacts. Post-hoc target gates
(asset-eligibility a2bdb3e, entry-hygiene + net-exposure cap fix 5/8) then
judge the synthetic fixture tickers against production state — AAA/BBB are
"not in today's Alpaca universe" and every all-long fixture book trips the
net cap. Sizer e2e tests exercise OTHER mechanisms; these gates default OFF
here. Their own unit tests re-enable them explicitly (monkeypatch.setenv /
direct calls with injected inputs).
"""
import importlib

import pytest

rbs = importlib.import_module("execution.regime_blended_sizer")


@pytest.fixture(autouse=True)
def _deterministic_sizer_gates(monkeypatch):
    # Asset gate: lookup-failure semantics (None → fail-open), matching a box
    # with no DB. Tests that exercise the gate pass `eligibility=` directly.
    monkeypatch.setattr(rbs, '_load_asset_eligibility', lambda symbols: None)
    monkeypatch.setenv('OPENCLAW_NET_EXPOSURE_CAP', '0')
    monkeypatch.setenv('OPENCLAW_ENTRY_HYGIENE', '0')
