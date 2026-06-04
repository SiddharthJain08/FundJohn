"""Tests for src/execution/universe_clamp.py — SP-7 Phase A4."""
from datetime import date

import pytest

from src.execution.universe_clamp import clamp_universe


META = {
    # symbol -> (asset_class, in_sp500)  — minimal metadata view
    "DELL": ("us_equity", True),
    "RDDT": ("us_equity", False),   # liquid but not SP500
    "SPY":  ("us_equity", False),   # ETF — must pass through via category
}
EQUITY_CATEGORY = {"DELL": "equity", "RDDT": "equity", "SPY": "etf"}


def fake_meta_fetch():
    return META


def fake_category_fetch():
    return EQUITY_CATEGORY


def test_clamp_off_is_identity(monkeypatch):
    monkeypatch.delenv("OPENCLAW_ENGINE_UNIVERSE_CLAMP", raising=False)
    u = ["DELL", "RDDT", "SPY", "BTC-USD"]
    assert clamp_universe(u, fake_meta_fetch, fake_category_fetch) == u


def test_clamp_sp500_filters_equities_only(monkeypatch):
    monkeypatch.setenv("OPENCLAW_ENGINE_UNIVERSE_CLAMP", "sp500")
    u = ["DELL", "RDDT", "SPY", "BTC-USD", "VIX"]
    out = clamp_universe(u, fake_meta_fetch, fake_category_fetch)
    assert "DELL" in out          # sp500 equity passes
    assert "RDDT" not in out      # non-sp500 equity clamped
    assert "SPY" in out           # ETF category → pass-through
    assert "BTC-USD" in out       # not in metadata → pass-through
    assert "VIX" in out           # not in metadata → pass-through


def test_unknown_predicate_fails_open(monkeypatch, capsys):
    monkeypatch.setenv("OPENCLAW_ENGINE_UNIVERSE_CLAMP", "no_such_predicate")
    u = ["DELL", "RDDT"]
    assert clamp_universe(u, fake_meta_fetch, fake_category_fetch) == u


def test_meta_fetch_exception_fails_open(monkeypatch):
    """meta_fetch raising an exception must fail-open and return input unchanged."""
    monkeypatch.setenv("OPENCLAW_ENGINE_UNIVERSE_CLAMP", "sp500")
    u = ["DELL", "RDDT", "SPY"]

    def exploding_meta_fetch():
        raise RuntimeError("DB connection refused")

    result = clamp_universe(u, exploding_meta_fetch, fake_category_fetch)
    assert result == u
