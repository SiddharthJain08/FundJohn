"""Phase 1F follow-up #13 — pure-function tests for the DBnomics period parser.

`_period_to_date` is the helper that converts DBnomics `period` strings into
sortable DATEs so dbnomics_observations.period_start can range-filter cleanly
across mixed frequencies.
"""
from datetime import date

from src.ingestion.dbnomics_client import _period_to_date


def test_yearly():
    assert _period_to_date("2026") == date(2026, 1, 1)
    assert _period_to_date("1955") == date(1955, 1, 1)


def test_quarterly():
    assert _period_to_date("2026-Q1") == date(2026, 1, 1)
    assert _period_to_date("2026-Q2") == date(2026, 4, 1)
    assert _period_to_date("2026-Q3") == date(2026, 7, 1)
    assert _period_to_date("2026-Q4") == date(2026, 10, 1)


def test_monthly():
    assert _period_to_date("2026-01") == date(2026, 1, 1)
    assert _period_to_date("2026-12") == date(2026, 12, 1)
    assert _period_to_date("1955-02") == date(1955, 2, 1)


def test_daily():
    assert _period_to_date("2026-01-15") == date(2026, 1, 15)
    assert _period_to_date("2026-12-31") == date(2026, 12, 31)


def test_malformed_returns_none():
    # Empty / None / non-strings
    assert _period_to_date("") is None
    assert _period_to_date(None) is None  # type: ignore[arg-type]
    # Garbage
    assert _period_to_date("not-a-period") is None
    assert _period_to_date("2026-Q5") is None         # invalid quarter
    assert _period_to_date("2026-13") is None         # invalid month
    assert _period_to_date("2026-02-30") is None      # invalid day
    # Extra junk after a valid prefix — the regex is anchored, so this fails too
    assert _period_to_date("2026-01-15-extra") is None


def test_weekly_iso_week():
    # DBnomics uses YYYY-W## for weekly series. ISO week 1 of 2026 starts
    # 2025-12-29 (Monday); ISO week 2 of 2026 starts 2026-01-05.
    assert _period_to_date("2026-W01") == date(2025, 12, 29)
    assert _period_to_date("2026-W02") == date(2026, 1, 5)
    # Single-digit week tolerated.
    assert _period_to_date("2026-W2") == date(2026, 1, 5)
    # Out-of-range week falls through to None.
    assert _period_to_date("2026-W54") is None


def test_semester_half():
    # DBnomics uses YYYY-S# (semester) or YYYY-H# (half) for semi-annual data.
    assert _period_to_date("2026-S1") == date(2026, 1, 1)
    assert _period_to_date("2026-S2") == date(2026, 7, 1)
    assert _period_to_date("2026-H1") == date(2026, 1, 1)
    assert _period_to_date("2026-H2") == date(2026, 7, 1)
