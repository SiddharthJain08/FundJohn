"""C1: while the account breaker is halted the sizer must refuse alpha OPENS
and ADDS. Exits, reductions and orphan closes are untouched, and benchmark
tickers are exempt (S_beta_spy positions and entries are never affected).

All inputs injected — no DB.
"""
from __future__ import annotations

import importlib

import pytest

rbs = importlib.import_module('execution.regime_blended_sizer')


def _gate(target, broker, *, halted=True, bench=None):
    return rbs._apply_account_breaker_gate(dict(target), broker, halted=halted,
                                           bench_tkrs=set(bench or ()))


# ── the shared only-shed primitive ──────────────────────────────────────────

def test_clamp_drops_an_unheld_open():
    out = {'AAPL': 5000.0}
    assert rbs._clamp_to_held(out, 'AAPL', {}) == 'blocked'
    assert 'AAPL' not in out


def test_clamp_converts_a_flip_to_close_only():
    out = {'AAPL': 5000.0}
    assert rbs._clamp_to_held(out, 'AAPL', {'AAPL': -3000.0}) == 'unflipped'
    assert out['AAPL'] == 0.0


def test_clamp_caps_an_add_at_the_held_size():
    out = {'AAPL': 9000.0}
    assert rbs._clamp_to_held(out, 'AAPL', {'AAPL': 4000.0}) == 'capped'
    assert out['AAPL'] == 4000.0


def test_clamp_caps_a_short_add_at_the_held_size():
    out = {'AMD': -9000.0}
    assert rbs._clamp_to_held(out, 'AMD', {'AMD': -4000.0}) == 'capped'
    assert out['AMD'] == -4000.0


def test_clamp_leaves_a_reduction_alone():
    out = {'AAPL': 1000.0}
    assert rbs._clamp_to_held(out, 'AAPL', {'AAPL': 4000.0}) == 'none'
    assert out['AAPL'] == 1000.0


# ── the gate ────────────────────────────────────────────────────────────────

def test_not_halted_is_byte_identical():
    target = {'AAPL': 9000.0, 'ZZTA': 1000.0}
    assert _gate(target, {}, halted=False) == target


def test_halted_blocks_a_new_alpha_open():
    out = _gate({'AAPL': 5000.0}, {})
    assert 'AAPL' not in out


def test_halted_caps_an_alpha_add_at_the_held_size():
    out = _gate({'AAPL': 9000.0}, {'AAPL': 4000.0})
    assert out['AAPL'] == 4000.0


def test_halted_never_blocks_a_reduction():
    out = _gate({'AAPL': 1000.0}, {'AAPL': 4000.0})
    assert out['AAPL'] == 1000.0


def test_halted_leaves_the_benchmark_sleeve_alone():
    out = _gate({'SPY': 90_000.0, 'AAPL': 9000.0}, {'SPY': 40_000.0}, bench=['SPY'])
    assert out['SPY'] == 90_000.0
    assert 'AAPL' not in out


def test_halted_ignores_option_and_crypto_symbols():
    target = {'AAPL260918C00250000': 400.0, 'BTC/USD': 30_000.0}
    assert _gate(target, {}) == target


def test_empty_targets_short_circuit():
    assert _gate({}, {'AAPL': 4000.0}) == {}


def test_halted_lookup_fails_open(monkeypatch):
    """A DB hiccup must not freeze the fleet — the flatten is the hard stop."""
    def _boom(*_a, **_k):
        raise RuntimeError('db down')

    monkeypatch.setattr(rbs.psycopg2, 'connect', _boom)
    assert rbs._load_account_breaker_halted() is False


def test_gate_is_wired_into_the_emission_tail(monkeypatch):
    """It must run AFTER entry hygiene and BEFORE the net-exposure cap."""
    order = []
    monkeypatch.setattr(rbs, '_apply_asset_eligibility_gate',
                        lambda t, b, **k: (order.append('asset'), t)[1])
    monkeypatch.setattr(rbs, '_apply_entry_hygiene_gate',
                        lambda t, b, **k: (order.append('hygiene'), t)[1])
    monkeypatch.setattr(rbs, '_apply_account_breaker_gate',
                        lambda t, b, **k: (order.append('breaker'), t)[1])
    monkeypatch.setattr(rbs, '_apply_net_exposure_cap',
                        lambda t: (order.append('netcap'), t)[1])
    monkeypatch.setattr(rbs, '_classify_position_deltas', lambda t, b, m: [])
    rbs._emit_orders_from_targets({}, {}, 100_000.0, None, None, {}, {}, [], {},
                                  1.0, {'equity': 100_000.0}, broker={})
    assert order == ['asset', 'hygiene', 'breaker', 'netcap']
