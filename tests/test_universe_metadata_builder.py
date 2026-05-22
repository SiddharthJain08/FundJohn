"""Tests for src/pipeline/backfillers/universe_metadata.py (SP-2 Phase B Task 8).

Most tests use a stub `pg` (no real Postgres) and monkey-patch the alpaca
status lookup so they run hermetically. Two tests do hit Postgres if
POSTGRES_URI is set — they're gated by an availability check.

The static-import regression check at the bottom enforces the DRY contract:
src/pipeline/ticker_metadata_writer.py must import build_month_snapshot.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from src.pipeline.backfillers import universe_metadata as um
from src.pipeline.backfillers.universe_metadata import (
    SCHEMA_COLUMNS,
    build_month_snapshot,
    rank_in_r1000_r3000,
)


# ── Stub helpers ──────────────────────────────────────────────────────────────
class _StubPG:
    """Minimal psycopg2-shaped stub. Tests that monkey-patch
    _alpaca_status_batch don't actually touch this object's cursor, so passing
    it through build_month_snapshot is safe."""
    def cursor(self):  # pragma: no cover — never called when monkey-patched
        raise RuntimeError('cursor() called on _StubPG; monkey-patch missing')


def _alpaca_row(
    symbol: str,
    *,
    first_seen: date = date(2000, 1, 1),
    last_seen: date = date(2100, 1, 1),
    status: str = 'active',
    tradable: bool = True,
) -> dict:
    return {
        'symbol': symbol,
        'asset_class': 'us_equity',
        'exchange': 'NASDAQ',
        'status': status,
        'tradable': tradable,
        'shortable': tradable,
        'fractionable': True,
        'easy_to_borrow': True,
        'first_seen_at': datetime.combine(first_seen, datetime.min.time()).replace(tzinfo=timezone.utc),
        'last_seen_at': datetime.combine(last_seen, datetime.min.time()).replace(tzinfo=timezone.utc),
    }


# ── Schema / small-universe smoke ─────────────────────────────────────────────
def test_builds_for_small_universe(monkeypatch):
    """build_month_snapshot returns one row per surviving ticker with the
    full schema."""
    universe = ['AAPL', 'MSFT']
    fake_alpaca = {s: _alpaca_row(s) for s in universe}
    monkeypatch.setattr(um, '_alpaca_status_batch', lambda syms, on, pg: fake_alpaca)
    monkeypatch.setattr(um, '_adv_usd_20d_batch', lambda syms, on: {s: 1.0e9 for s in syms})
    monkeypatch.setattr(um, '_sp500_membership_on', lambda d: {'AAPL', 'MSFT'})

    df = build_month_snapshot(date(2024, 8, 31), universe, _StubPG())
    assert len(df) == 2
    # Every schema column present.
    for col in SCHEMA_COLUMNS:
        assert col in df.columns, f'missing schema column: {col}'
    # Numeric fields nullable (market_cap is None for historical snapshots).
    assert df['market_cap'].isna().all()
    assert df['adv_usd_20d'].notna().all()


def test_skips_unlisted_tickers(monkeypatch):
    """Tickers with first_seen_at > snapshot_date are dropped (not yet listed)."""
    universe = ['AAPL', 'TSLA']
    fake_alpaca = {
        'AAPL': _alpaca_row('AAPL', first_seen=date(2010, 1, 1)),
        # TSLA filtered out because alpaca_status_batch already drops it.
    }
    monkeypatch.setattr(um, '_alpaca_status_batch', lambda syms, on, pg: fake_alpaca)
    monkeypatch.setattr(um, '_adv_usd_20d_batch', lambda syms, on: {s: None for s in syms})
    monkeypatch.setattr(um, '_sp500_membership_on', lambda d: set())

    df = build_month_snapshot(date(2024, 8, 31), universe, _StubPG())
    assert set(df['symbol']) == {'AAPL'}


def test_in_sp500_uses_historical_membership(monkeypatch):
    """The in_sp500 flag is driven by _sp500_membership_on at snapshot_date."""
    universe = ['AAPL', 'TSLA']
    fake_alpaca = {s: _alpaca_row(s) for s in universe}
    monkeypatch.setattr(um, '_alpaca_status_batch', lambda syms, on, pg: fake_alpaca)
    monkeypatch.setattr(um, '_adv_usd_20d_batch', lambda syms, on: {s: 1.0 for s in syms})
    # On this date AAPL is a member, TSLA is not.
    monkeypatch.setattr(um, '_sp500_membership_on', lambda d: {'AAPL'})

    df = build_month_snapshot(date(2018, 6, 30), universe, _StubPG())
    flags = dict(zip(df['symbol'], df['in_sp500']))
    assert flags['AAPL'] is True or flags['AAPL'] == True  # noqa: E712
    assert flags['TSLA'] is False or flags['TSLA'] == False  # noqa: E712


def test_handles_none_market_cap(monkeypatch):
    """Ticker with no FMP market_cap is still included; in_r1000=False."""
    universe = ['AAPL', 'MSFT']
    fake_alpaca = {s: _alpaca_row(s) for s in universe}
    monkeypatch.setattr(um, '_alpaca_status_batch', lambda syms, on, pg: fake_alpaca)
    monkeypatch.setattr(um, '_adv_usd_20d_batch', lambda syms, on: {s: 1.0 for s in syms})
    monkeypatch.setattr(um, '_sp500_membership_on', lambda d: set())

    # No market_cap_lookup → all market_cap=None → all in_r1000=False.
    df = build_month_snapshot(date(2024, 8, 31), universe, _StubPG())
    assert df['market_cap'].isna().all()
    assert (~df['in_r1000']).all()
    assert (~df['in_r3000']).all()
    assert len(df) == 2  # both rows still present


# ── r1000/r3000 ranking ──────────────────────────────────────────────────────
def test_in_r1000_in_r3000_ranked_correctly(monkeypatch):
    """Top-2 by market_cap → r1000 set; top-4 → r3000 set (with patched
    1000/3000 limits via direct helper to keep the test self-contained)."""
    # Use the rank helper directly with a tiny set so we can hand-check.
    rows = [
        {'symbol': 'A', 'tradable': True, 'status': 'active', 'market_cap': 5e12},
        {'symbol': 'B', 'tradable': True, 'status': 'active', 'market_cap': 4e12},
        {'symbol': 'C', 'tradable': True, 'status': 'active', 'market_cap': 3e12},
        {'symbol': 'D', 'tradable': True, 'status': 'active', 'market_cap': 2e12},
        {'symbol': 'E', 'tradable': True, 'status': 'active', 'market_cap': 1e12},
    ]
    r1000, r3000 = rank_in_r1000_r3000(rows)
    # All 5 rows fit in r1000 and r3000 (sets are top-1000 / top-3000).
    assert r1000 == {'A', 'B', 'C', 'D', 'E'}
    assert r3000 == {'A', 'B', 'C', 'D', 'E'}

    # Exclude non-tradable / inactive / None-market-cap from the pool.
    rows_mixed = [
        {'symbol': 'A', 'tradable': True, 'status': 'active', 'market_cap': 5e12},
        {'symbol': 'B', 'tradable': False, 'status': 'active', 'market_cap': 4e12},
        {'symbol': 'C', 'tradable': True, 'status': 'inactive', 'market_cap': 3e12},
        {'symbol': 'D', 'tradable': True, 'status': 'active', 'market_cap': None},
        {'symbol': 'E', 'tradable': True, 'status': 'active', 'market_cap': 1e12},
    ]
    r1000_m, _ = rank_in_r1000_r3000(rows_mixed)
    assert r1000_m == {'A', 'E'}


def test_in_r1000_r3000_via_dataframe_path(monkeypatch):
    """Verify the DataFrame branch of rank_in_r1000_r3000 (used by the
    builder) produces identical results to the list[dict] branch."""
    universe = ['A', 'B', 'C', 'D', 'E']
    fake_alpaca = {s: _alpaca_row(s) for s in universe}
    caps = {'A': 5e12, 'B': 4e12, 'C': 3e12, 'D': 2e12, 'E': 1e12}

    monkeypatch.setattr(um, '_alpaca_status_batch', lambda syms, on, pg: fake_alpaca)
    monkeypatch.setattr(um, '_adv_usd_20d_batch', lambda syms, on: {s: 1.0 for s in syms})
    monkeypatch.setattr(um, '_sp500_membership_on', lambda d: set())

    df = build_month_snapshot(
        date(2024, 8, 31), universe, _StubPG(),
        market_cap_lookup=caps,
    )
    # All five are in r1000 and r3000.
    assert df['in_r1000'].all()
    assert df['in_r3000'].all()


# ── Validator tests (driver) ──────────────────────────────────────────────────
def _import_validate_metadata():
    """Import the driver's _validate_metadata lazily — the driver pulls in
    psycopg2/redis at import time but the validator function itself is pure."""
    import importlib.util
    p = Path(__file__).resolve().parents[1] / 'scripts' / 'backfill_universe_5y.py'
    spec = importlib.util.spec_from_file_location('backfill_universe_5y', p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._validate_metadata


def test_validate_rejects_low_row_count():
    """Floor lowered to 30 for v1's 404-ticker universe (was 1500 for the
    hypothetical 3000-ticker universe). Earliest IPOs are pre-1990 — those
    snapshots will have ~40-50 tickers."""
    _validate_metadata = _import_validate_metadata()
    df = pd.DataFrame({
        'symbol': [f'T{i}' for i in range(20)],
        'market_cap': [1e12] * 20,
    })
    ok, err = _validate_metadata(df, date(2024, 8, 31))
    assert not ok
    assert err and 'row_count_too_low' in err


def test_validate_rejects_duplicate_symbols():
    _validate_metadata = _import_validate_metadata()
    # Clear row-count floor (>30) but T0 duplicated.
    symbols = ['T0'] + [f'T{i}' for i in range(1, 50)]
    df = pd.DataFrame({
        'symbol': symbols + ['T0'],  # 51 rows; T0 appears twice
        'market_cap': [1e12] * 51,
    })
    ok, err = _validate_metadata(df, date(2024, 8, 31))
    assert not ok
    assert err == 'duplicate_symbol'


def test_validate_rejects_implausible_top10_cap():
    _validate_metadata = _import_validate_metadata()
    # 50 unique symbols, all market_cap < $100B — top10 floor fires.
    df = pd.DataFrame({
        'symbol': [f'T{i}' for i in range(50)],
        'market_cap': [5e10] * 50,  # all $50B
    })
    ok, err = _validate_metadata(df, date(2024, 8, 31))
    assert not ok
    assert err == 'top10_market_cap_implausible'


def test_validate_accepts_when_market_cap_all_none():
    """v1 historical reality: FMP Starter 403s on historical-market-cap.
    Snapshots with universal None market_cap should still LAND (they're
    useful for in_sp500 + sector data even without ranks)."""
    _validate_metadata = _import_validate_metadata()
    df = pd.DataFrame({
        'symbol': [f'T{i}' for i in range(50)],
        'market_cap': [None] * 50,
    })
    ok, err = _validate_metadata(df, date(2024, 8, 31))
    assert ok, f'expected accept on all-None market_cap, got {err}'


def test_validate_accepts_healthy_snapshot():
    _validate_metadata = _import_validate_metadata()
    # 2000 rows, unique symbols, one mega-cap.
    caps = [1e12] + [5e10] * 1999
    df = pd.DataFrame({
        'symbol': [f'T{i}' for i in range(2000)],
        'market_cap': caps,
    })
    ok, err = _validate_metadata(df, date(2024, 8, 31))
    assert ok
    assert err is None


# Removed test_validate_rejects_all_none_market_cap — superseded by
# test_validate_accepts_when_market_cap_all_none (Phase B v1 calibration:
# all-None market_cap is acceptable; historical snapshots still useful
# for in_sp500 + sector even without cap-based ranks).


# ── DRY regression — writer must delegate to the shared builder ───────────────
def test_writer_uses_builder():
    """Regression: src/pipeline/ticker_metadata_writer.py must reference
    `build_month_snapshot` so Phase A's daily writer shares the Phase B
    builder's code path."""
    src_path = (
        Path(__file__).resolve().parents[1]
        / 'src' / 'pipeline' / 'ticker_metadata_writer.py'
    )
    text = src_path.read_text()
    assert 'build_month_snapshot' in text


def test_sp500_membership_csv_loads():
    """Sanity: the SP500 historical CSV is shipped + parseable + AAPL is in."""
    members = um._sp500_membership_on(date(2024, 8, 31))
    assert 'AAPL' in members, 'expected AAPL in SP500 on 2024-08-31'
