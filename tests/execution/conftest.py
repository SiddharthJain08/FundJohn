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
    # §8 (2026-08-06): production .env carries OPENCLAW_SAMEDAY_SIGNAL_TARGET=1
    # and some test module's import-time load_dotenv pulls it into os.environ
    # during collection. The resolver lets the new flag WIN over the legacy
    # OPENCLAW_EOD_SIGNAL_REGISTER, so every pre-§8 test that enables the T+1
    # mode by monkeypatching only the legacy flag silently stayed in same-day
    # mode (45 combined-run failures, 2026-08-06). Clearing it here restores
    # pure legacy-flag semantics for the suite; §8's own tests set both
    # explicitly.
    monkeypatch.delenv('OPENCLAW_SAMEDAY_SIGNAL_TARGET', raising=False)
    # A1 (final fix wave, 2026-08-29): _sharpe_cadence_path now calls these
    # two on every cycle. The S_m provider (regime_benchmark_sharpe_for_sizing)
    # opens its own psycopg2.connect(POSTGRES_URI) on a cache miss and does an
    # INSERT ... ON CONFLICT DO UPDATE + commit() on pipeline_config; the
    # sleeve loader (load_benchmark_sleeve_ids) also opens a connection. Stub
    # both so sizer tests that don't already patch them stay DB-free and
    # deterministic (no benchmark tickers, no hurdle). Tests that need the
    # real behaviour patch these explicitly (`with _mock.patch(...)`), which
    # overrides this fixture for the duration of the `with` block.
    monkeypatch.setattr('execution.benchmark_sleeve.load_benchmark_sleeve_ids', lambda conn=None: set())
    monkeypatch.setattr('execution.benchmark_sizing.regime_benchmark_sharpe_for_sizing', lambda *a, **k: None)
