"""Entry-hygiene gate (fix 5, 2026-07-27): stop-out cooldown + liquidity floor
+ ADV participation cap, applied to targets with only-shed semantics — exits
are never blocked. All inputs injectable; no DB/artifact access in tests."""
import importlib

import pytest

rbs = importlib.import_module("execution.regime_blended_sizer")


@pytest.fixture(autouse=True)
def _hygiene_enabled(monkeypatch):
    # conftest defaults the gate OFF for e2e harnesses; this file tests it.
    monkeypatch.setenv("OPENCLAW_ENTRY_HYGIENE", "1")


PARAMS = {
    'stopout_cooldown_days': 7.0,
    'entry_min_price_usd': 2.0,
    'entry_min_adv_usd': 400_000.0,
    'entry_participation_frac': 0.01,
}


def _gate(target, broker, *, stopouts=None, liq=None):
    return rbs._apply_entry_hygiene_gate(
        dict(target), broker,
        stopouts=stopouts or {}, liq=liq or ({}, {}), params=dict(PARAMS))


# ── stop-out cooldown ────────────────────────────────────────────────────────

def test_cooldown_blocks_same_direction_reentry():
    out = _gate({"CENN": -3000.0}, {}, stopouts={"CENN": -1})
    assert "CENN" not in out


def test_cooldown_allows_opposite_direction():
    out = _gate({"CENN": 3000.0}, {}, stopouts={"CENN": -1})
    assert out["CENN"] == 3000.0


def test_cooldown_caps_add_at_held_size():
    out = _gate({"NVNO": -9000.0}, {"NVNO": -4000.0}, stopouts={"NVNO": -1})
    assert out["NVNO"] == -4000.0


def test_cooldown_never_blocks_reduction():
    out = _gate({"NVNO": -1000.0}, {"NVNO": -4000.0}, stopouts={"NVNO": -1})
    assert out["NVNO"] == -1000.0


# ── liquidity floor ──────────────────────────────────────────────────────────

def test_subdollar_entry_blocked():
    liq = ({"ELME": 900_000}, {"ELME": 1.48})
    out = _gate({"ELME": 2000.0}, {}, liq=liq)
    assert "ELME" not in out


def test_thin_adv_entry_blocked():
    liq = ({"ACCL": 55_000}, {"ACCL": 5.00})
    out = _gate({"ACCL": 2000.0}, {}, liq=liq)
    assert "ACCL" not in out


def test_liquid_name_passes():
    liq = ({"AAPL": 9e9}, {"AAPL": 210.0})
    out = _gate({"AAPL": 5000.0}, {}, liq=liq)
    assert out["AAPL"] == 5000.0


def test_unknown_ticker_not_liquidity_gated():
    # absent from the artifact → resolver/asset-gate remain the authority
    out = _gate({"ZZZQ": 2000.0}, {}, liq=({"AAPL": 9e9}, {"AAPL": 210.0}))
    assert out["ZZZQ"] == 2000.0


def test_illiquid_held_position_flip_becomes_close_only():
    liq = ({"ELME": 90_000}, {"ELME": 1.48})
    out = _gate({"ELME": -2000.0}, {"ELME": 1500.0}, liq=liq)
    assert out["ELME"] == 0.0


# ── participation cap ────────────────────────────────────────────────────────

def test_participation_cap_shrinks_oversized_entry():
    liq = ({"SKYA": 500_000}, {"SKYA": 6.0})     # cap = 1% × 500k = $5k
    out = _gate({"SKYA": 9000.0}, {}, liq=liq)
    assert out["SKYA"] == 5000.0


def test_participation_cap_preserves_sign_short():
    liq = ({"SKYA": 500_000}, {"SKYA": 6.0})
    out = _gate({"SKYA": -9000.0}, {}, liq=liq)
    assert out["SKYA"] == -5000.0


def test_participation_cap_never_forces_shrink_of_held():
    # held above cap → limit = |held|, target passes at held size (no churn)
    liq = ({"SKYA": 500_000}, {"SKYA": 6.0})
    out = _gate({"SKYA": 8000.0}, {"SKYA": 8000.0}, liq=liq)
    assert out["SKYA"] == 8000.0


def test_within_cap_untouched():
    liq = ({"WW": 2_000_000}, {"WW": 30.0})      # cap $20k
    out = _gate({"WW": 4000.0}, {}, liq=liq)
    assert out["WW"] == 4000.0


# ── scope + kill switch ──────────────────────────────────────────────────────

def test_occ_and_crypto_symbols_out_of_scope():
    out = _gate({"AAPL260821C00200000": 2000.0, "BTC/USD": 2000.0}, {},
                stopouts={"AAPL260821C00200000": 1, "BTC/USD": 1})
    assert len(out) == 2


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("OPENCLAW_ENTRY_HYGIENE", "0")
    out = rbs._apply_entry_hygiene_gate(
        {"CENN": -3000.0}, {}, stopouts={"CENN": -1},
        liq=({}, {}), params=dict(PARAMS))
    assert out["CENN"] == -3000.0
