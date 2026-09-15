"""C1 pure rule engine (spec 2026-09-12 §3 C1, ruling R2).

  drawdown   alpha_nav / rolling_peak - 1        <= -0.10
  daily loss (equity - opening_equity)/opening   <= -0.03

alpha_nav is net of the benchmark sleeve; the daily rule is on TOTAL equity.
No DB, no CLI, no network in this file.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import account_breaker as ab  # noqa: E402

MIGRATION = ROOT / 'src' / 'database' / 'migrations' / '157_account_breaker.sql'


# ── migration ───────────────────────────────────────────────────────────────

def test_migration_creates_both_tables_idempotently():
    sql = MIGRATION.read_text()
    assert 'CREATE TABLE IF NOT EXISTS account_breaker_state' in sql
    assert 'CREATE TABLE IF NOT EXISTS account_daily_open' in sql
    # singleton row must exist before the first UPDATE
    assert re.search(r"INSERT INTO account_breaker_state\s*\(id\)\s*VALUES\s*\(1\)", sql)
    assert 'ON CONFLICT' in sql
    assert 'DROP ' not in sql.upper()          # append-only invariant
    assert 'DELETE ' not in sql.upper()        # append-only invariant
    assert 'TRUNCATE ' not in sql.upper()      # append-only invariant


def test_migration_number_does_not_collide_with_stream_b():
    names = sorted(p.name for p in MIGRATION.parent.glob('15*.sql'))
    assert MIGRATION.name in names
    # Stream B reserved 155 and 156; C1 must not reuse either.
    assert not MIGRATION.name.startswith(('155_', '156_'))


# ── alpha_nav ───────────────────────────────────────────────────────────────

def _pos(**mv):
    return {sym: {'qty': 1.0, 'side': 'long', 'market_value': str(v)}
            for sym, v in mv.items()}


def test_alpha_nav_subtracts_benchmark_market_value():
    alpha, bench_mv = ab.alpha_nav(200_000.0, _pos(SPY=40_000, AAPL=30_000), {'SPY'})
    assert bench_mv == 40_000.0
    assert alpha == 160_000.0


def test_alpha_nav_with_no_benchmark_ticker_equals_equity():
    alpha, bench_mv = ab.alpha_nav(200_000.0, _pos(AAPL=30_000), set())
    assert (alpha, bench_mv) == (200_000.0, 0.0)


def test_alpha_nav_handles_short_benchmark_and_junk_values():
    alpha, bench_mv = ab.alpha_nav(
        100_000.0,
        {'SPY': {'qty': -1, 'market_value': '-25000'},
         'XXX': {'qty': 1, 'market_value': None}},
        {'SPY', 'XXX'})
    assert bench_mv == -25_000.0
    assert alpha == 125_000.0


def test_alpha_nav_bench_ticker_match_is_case_insensitive():
    alpha, bench_mv = ab.alpha_nav(200_000.0, _pos(SPY=40_000, AAPL=30_000), {'spy'})
    assert bench_mv == 40_000.0
    assert alpha == 160_000.0


# ── evaluate ────────────────────────────────────────────────────────────────

def test_no_breach_inside_both_limits():
    st = ab.evaluate(97_000.0, 100_000.0, 199_000.0, 200_000.0)
    assert st['rule'] == 'none' and st['breach'] is False
    assert st['dd'] == pytest.approx(-0.03)
    assert st['daily'] == pytest.approx(-0.005)


def test_drawdown_rule_trips_at_minus_ten_percent():
    st = ab.evaluate(90_000.0, 100_000.0, 200_000.0, 200_000.0)
    assert st['rule'] == 'drawdown' and st['breach'] is True
    assert st['dd'] == pytest.approx(-0.10)


def test_drawdown_just_inside_the_limit_does_not_trip():
    st = ab.evaluate(90_001.0, 100_000.0, 200_000.0, 200_000.0)
    assert st['breach'] is False


def test_daily_loss_just_inside_the_limit_does_not_trip():
    st = ab.evaluate(100_000.0, 100_000.0, 194_020.0, 200_000.0)  # daily = -0.0299
    assert st['breach'] is False


def test_daily_loss_rule_trips_at_minus_three_percent():
    st = ab.evaluate(100_000.0, 100_000.0, 194_000.0, 200_000.0)
    assert st['rule'] == 'daily_loss' and st['breach'] is True
    assert st['daily'] == pytest.approx(-0.03)


def test_both_rules_are_reported_together():
    st = ab.evaluate(85_000.0, 100_000.0, 190_000.0, 200_000.0)
    assert st['rule'] == 'drawdown+daily_loss' and st['breach'] is True


def test_peak_ratchets_up_and_never_down():
    st = ab.evaluate(120_000.0, 100_000.0, 200_000.0, 200_000.0)
    assert st['peak'] == 120_000.0 and st['dd'] == 0.0
    st2 = ab.evaluate(110_000.0, 120_000.0, 200_000.0, 200_000.0)
    assert st2['peak'] == 120_000.0


def test_missing_peak_seeds_from_current_alpha_nav():
    st = ab.evaluate(150_000.0, None, 200_000.0, 200_000.0)
    assert st['peak'] == 150_000.0 and st['dd'] == 0.0 and st['breach'] is False


def test_missing_opening_equity_reports_daily_none_and_never_trips_it():
    st = ab.evaluate(100_000.0, 100_000.0, 1.0, None)
    assert st['daily'] is None and st['rule'] == 'none'


def test_non_positive_peak_is_not_a_division_by_zero():
    st = ab.evaluate(-500.0, 0.0, 100.0, 100.0)
    assert st['dd'] == 0.0 and st['rule'] == 'none'


# ── flag + regime independence ──────────────────────────────────────────────

def test_armed_reads_the_flag(monkeypatch):
    monkeypatch.delenv(ab.ARM_ENV, raising=False)
    assert ab.armed() is False
    monkeypatch.setenv(ab.ARM_ENV, '0')
    assert ab.armed() is False
    monkeypatch.setenv(ab.ARM_ENV, '1')
    assert ab.armed() is True


def test_module_never_reads_the_regime():
    """Ruling R2: all regimes. Nothing in this module may branch on regime."""
    src = MIGRATION.parent.parent.parent / 'execution' / 'account_breaker.py'
    text = src.read_text()
    for token in ('market_regime', 'regime_state', 'HIGH_VOL', 'CRISIS', 'LOW_VOL'):
        assert token not in text, f'regime coupling found: {token}'
