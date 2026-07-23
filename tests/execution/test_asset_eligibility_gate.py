"""Asset-eligibility execution gate (operator directive 2026-07-23):
positions only in equities that are active + tradable + easy_to_borrow +
fractionable. The gate clamps targets pre-classification and must NEVER
block an exit."""
import importlib

import pytest

rbs = importlib.import_module("execution.regime_blended_sizer")


def _gate(target, broker, elig):
    return rbs._apply_asset_eligibility_gate(dict(target), broker, eligibility=elig)


def test_open_blocked_when_not_held():
    out = _gate({"XXII": 5000.0}, {}, {"XXII": False})
    assert "XXII" not in out


def test_eligible_open_untouched():
    out = _gate({"AAPL": 5000.0}, {}, {"AAPL": True})
    assert out == {"AAPL": 5000.0}


def test_flip_zeroed_close_leg_survives():
    # held long, ineligible, target flips short → target 0 so the classifier
    # emits a plain full close and no flip_open
    out = _gate({"CENN": -3000.0}, {"CENN": 4000.0}, {"CENN": False})
    assert out["CENN"] == 0.0
    emissions = rbs._classify_position_deltas(out, {"CENN": 4000.0}, {"CENN": {}})
    assert [(t, k) for t, _, k in emissions] == [("CENN", "delta")]
    assert emissions[0][1] == -4000.0


def test_increase_capped_at_held_size():
    out = _gate({"NVNO": -9000.0}, {"NVNO": -4000.0}, {"NVNO": False})
    assert out["NVNO"] == -4000.0            # hold allowed, growth blocked
    assert rbs._classify_position_deltas(out, {"NVNO": -4000.0}, {}) == []


def test_reduction_passes_through():
    out = _gate({"NVNO": -1000.0}, {"NVNO": -4000.0}, {"NVNO": False})
    assert out["NVNO"] == -1000.0            # shedding exposure is an exit


def test_orphan_close_unaffected():
    # held ineligible name absent from targets → orphan close still emitted
    out = _gate({}, {"OCGN": 2000.0}, {"OCGN": False})
    meta = {}
    emissions = rbs._classify_position_deltas(out, {"OCGN": 2000.0}, meta)
    assert [(t, k) for t, _, k in emissions] == [("OCGN", "orphan_close")]


def test_occ_and_crypto_out_of_scope():
    target = {"AAPL260117C00190000": 1000.0, "BTC/USD": 2000.0}
    out = _gate(target, {}, {})              # empty map = everything unknown
    assert out == target                     # non-equities never gated


def test_unknown_symbol_fails_closed():
    out = _gate({"ZZZQ": 1000.0}, {}, {})    # asked and absent → ineligible
    assert "ZZZQ" not in out


def test_lookup_failure_fails_open(monkeypatch):
    monkeypatch.setattr(rbs, "_load_asset_eligibility", lambda syms: None)
    out = rbs._apply_asset_eligibility_gate({"XXII": 5000.0}, {})
    assert out == {"XXII": 5000.0}


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("OPENCLAW_SIZER_ASSET_GATE", "0")
    out = _gate({"XXII": 5000.0}, {}, {"XXII": False})
    assert out == {"XXII": 5000.0}


def test_eligibility_predicate_sql_shape():
    # the eligibility predicate is the operator contract: active + tradable
    # + easy_to_borrow + fractionable, sourced from alpaca_tradable_universe
    import inspect
    src = inspect.getsource(rbs._load_asset_eligibility)
    for token in ("status = 'active'", "tradable", "easy_to_borrow",
                  "fractionable", "alpaca_tradable_universe"):
        assert token in src
