"""B3 enforcement: ownership-blocked names can be REDUCED but never ADDED to.

The block reuses the entry-hygiene gate's `_shed` helper, so the guarantee is
structural: not held -> target dropped; flip -> close-only; same-sign increase ->
capped at held; reduce and flatten pass through untouched.
"""
from __future__ import annotations

import importlib

import pytest

rbs = importlib.import_module("execution.regime_blended_sizer")
po = importlib.import_module("execution.position_ownership")


@pytest.fixture(autouse=True)
def _hygiene_enabled(monkeypatch):
    # tests/execution/conftest.py defaults OPENCLAW_ENTRY_HYGIENE=0 for the e2e
    # sizer harnesses; this file tests the gate, so re-enable it (same as
    # tests/execution/test_entry_hygiene_gate.py).
    monkeypatch.setenv("OPENCLAW_ENTRY_HYGIENE", "1")


PARAMS = {
    'stopout_cooldown_days': 7.0,
    'risk_exit_cooldown_days': 7.0,
    'entry_min_price_usd': 2.0,
    'entry_min_adv_usd': 400_000.0,
    'entry_participation_frac': 0.01,
}


def _gate(target, broker, *, ownership_blocked=None):
    # Every lookup is injected: an omitted one falls through to the REAL
    # Postgres on this box (modules under src/execution load .env at import).
    return rbs._apply_entry_hygiene_gate(
        dict(target), broker,
        stopouts={}, liq=({}, {}), params=dict(PARAMS),
        risk_exits={}, premarket_vetoes=set(),
        ownership_blocked=set(ownership_blocked or ()))


# ── shed semantics ─────────────────────────────────────────────────────────

def test_blocked_ticker_not_held_is_dropped():
    assert "AAA" not in _gate({"AAA": 5000.0}, {}, ownership_blocked=["AAA"])


def test_blocked_ticker_add_is_capped_at_held_size():
    out = _gate({"AAA": 9000.0}, {"AAA": 4000.0}, ownership_blocked=["AAA"])
    assert out["AAA"] == 4000.0


def test_blocked_ticker_reduce_is_allowed():
    out = _gate({"AAA": 1000.0}, {"AAA": 4000.0}, ownership_blocked=["AAA"])
    assert out["AAA"] == 1000.0


def test_blocked_ticker_flatten_is_never_blocked():
    out = _gate({"AAA": 0.0}, {"AAA": 4000.0}, ownership_blocked=["AAA"])
    assert out["AAA"] == 0.0


def test_blocked_ticker_flip_becomes_close_only():
    out = _gate({"AAA": -5000.0}, {"AAA": 4000.0}, ownership_blocked=["AAA"])
    assert out["AAA"] == 0.0


def test_blocked_short_add_is_capped_at_held_size():
    out = _gate({"AAA": -9000.0}, {"AAA": -4000.0}, ownership_blocked=["AAA"])
    assert out["AAA"] == -4000.0


def test_unblocked_ticker_is_untouched():
    out = _gate({"BBB": 5000.0}, {}, ownership_blocked=["AAA"])
    assert out["BBB"] == 5000.0


def test_empty_blocklist_is_a_passthrough():
    out = _gate({"AAA": 5000.0}, {}, ownership_blocked=[])
    assert out["AAA"] == 5000.0


# ── blocklist resolution ───────────────────────────────────────────────────

_STATUSES = {'AAA': po.STATUS_UNALLOCATED, 'BBB': po.STATUS_OK,
             'CCC': po.STATUS_SHORTFALL}


def test_blocklist_is_empty_without_the_flag(monkeypatch):
    monkeypatch.delenv("OPENCLAW_OWNERSHIP_BLOCK", raising=False)
    assert rbs._ownership_blocklist_from(_STATUSES) == set()


def test_blocklist_is_empty_when_the_flag_is_not_exactly_one(monkeypatch):
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "true")
    assert rbs._ownership_blocklist_from(_STATUSES) == set()


def test_blocklist_holds_every_non_ok_ticker_with_the_flag(monkeypatch):
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    assert rbs._ownership_blocklist_from(_STATUSES) == {"AAA", "CCC"}


def test_blocklist_of_an_empty_status_map_is_empty(monkeypatch):
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    assert rbs._ownership_blocklist_from({}) == set()
    assert rbs._ownership_blocklist_from(None) == set()


def test_blocklist_logs_the_ownership_line_in_report_only_mode(monkeypatch, caplog):
    monkeypatch.delenv("OPENCLAW_OWNERSHIP_BLOCK", raising=False)
    with caplog.at_level("INFO", logger=rbs.logger.name):
        rbs._ownership_blocklist_from(_STATUSES)
    assert any("[ownership]" in r.getMessage() for r in caplog.records)


# ── the gate itself must never reach the DB ────────────────────────────────

def test_gate_defaults_to_no_block_and_never_loads():
    """An omitted ownership_blocked must resolve to the empty set, NOT to a DB
    read: tests/execution/test_entry_hygiene_gate.py and
    test_sameday_premarket_protection.py call this gate without the kwarg and
    their contract is 'no DB access in tests'. Enforcement is supplied by the
    single production call site instead."""
    import inspect
    src = inspect.getsource(rbs._apply_entry_hygiene_gate)
    assert '_load_ownership_blocklist' not in src, \
        'the gate must not load the blocklist; the call site supplies it'
    out = rbs._apply_entry_hygiene_gate(
        {"AAA": 5000.0}, {}, stopouts={}, liq=({}, {}), params=dict(PARAMS),
        risk_exits={}, premarket_vetoes=set())
    assert out["AAA"] == 5000.0


def test_the_single_production_call_site_supplies_the_blocklist():
    """test_entry_hygiene_gate.py:198 already pins the gate to exactly ONE
    invocation (definition + call = 2 occurrences). Pin that the one invocation
    is the one that passes the blocklist, so a second emission route can never
    silently skip ownership enforcement."""
    import inspect
    tail = inspect.getsource(rbs._emit_orders_from_targets)
    assert 'ownership_blocked=_load_ownership_blocklist()' in tail
    assert inspect.getsource(rbs).count('_apply_entry_hygiene_gate(') == 2
