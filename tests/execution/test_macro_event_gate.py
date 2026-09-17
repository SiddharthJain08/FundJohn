"""C3 (ruling R3): T-1..T macro-event entry block.

Blocks NEW opens/adds only; exits, reductions and orphan closes are untouched;
all regimes; benchmark included unless OPENCLAW_EVENT_GATE_EXEMPT_BENCH=1.
Every input injected — no calendar read, no DB.
"""
from __future__ import annotations

import datetime as dt
import importlib
import logging

import pytest

rbs = importlib.import_module('execution.regime_blended_sizer')

SESSION = dt.date(2026, 9, 16)
EVENTS = 'CPI@2026-09-16'


@pytest.fixture(autouse=True)
def _shadow_by_default(monkeypatch):
    monkeypatch.delenv('OPENCLAW_EVENT_GATE', raising=False)
    monkeypatch.delenv('OPENCLAW_EVENT_GATE_EXEMPT_BENCH', raising=False)


def _gate(target, broker, *, events=EVENTS, bench=None):
    return rbs._apply_macro_event_gate(dict(target), broker, session=SESSION,
                                       events=events, bench_tkrs=set(bench or ()))


# ── shadow ──────────────────────────────────────────────────────────────────

def test_shadow_returns_the_targets_untouched():
    target = {'AAPL': 9000.0, 'ZZTA': 1000.0}
    assert _gate(target, {'AAPL': 4000.0}) == target


def test_shadow_line_reports_what_would_have_been_blocked(caplog):
    with caplog.at_level(logging.INFO, logger=rbs.logger.name):
        _gate({'AAPL': 9000.0, 'ZZTA': 1000.0}, {'AAPL': 4000.0})
    line = [m for m in (r.getMessage() for r in caplog.records)
            if m.startswith('[event_gate] ')][-1]
    assert line == ('[event_gate] shadow session=2026-09-16 events=CPI@2026-09-16 '
                    'blocked=1 capped=1 tickers=AAPL,ZZTA bench_exempt=0')


def test_line_is_emitted_on_a_non_event_session(monkeypatch, caplog):
    # Deviation from the brief: the brief's version of this test passed
    # events=None and relied on the real data/master/macro_events.parquet
    # being absent (so the real lib.macro_events.gating_event(SESSION)
    # resolves to None) to exercise the "events=none" line. That is not
    # hermetic — this module's own docstring promises "every input
    # injected — no calendar read, no DB", and SESSION (2026-09-16) is the
    # exact date this file's own EVENTS constant assigns a CPI release, so
    # once Task 8's 2017-2027 backfill lands this test would start reading
    # real data and could flip to failing. Stub the loader explicitly
    # instead, same as test_calendar_failure_does_not_block_anything below.
    monkeypatch.setattr(rbs, '_load_macro_event_gating', lambda s: None)
    with caplog.at_level(logging.INFO, logger=rbs.logger.name):
        _gate({'AAPL': 9000.0}, {}, events=None)
    line = [m for m in (r.getMessage() for r in caplog.records)
            if m.startswith('[event_gate] ')][-1]
    assert line == ('[event_gate] shadow session=2026-09-16 events=none '
                    'blocked=0 capped=0 tickers= bench_exempt=0')


# ── armed ───────────────────────────────────────────────────────────────────

def test_armed_blocks_a_new_open(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    assert 'ZZTA' not in _gate({'ZZTA': 1000.0}, {})


def test_armed_caps_an_add_at_the_held_size(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    assert _gate({'AAPL': 9000.0}, {'AAPL': 4000.0})['AAPL'] == 4000.0


def test_armed_never_blocks_a_reduction(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    assert _gate({'AAPL': 1000.0}, {'AAPL': 4000.0})['AAPL'] == 1000.0


def test_armed_converts_a_flip_to_close_only(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    assert _gate({'AAPL': 5000.0}, {'AAPL': -3000.0})['AAPL'] == 0.0


def test_armed_on_a_non_event_session_changes_nothing(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    target = {'ZZTA': 1000.0}
    assert _gate(target, {}, events=None) == target


# ── R3: benchmark inclusion + regime independence ───────────────────────────

def test_benchmark_is_blocked_by_default(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    assert 'SPY' not in _gate({'SPY': 90_000.0}, {}, bench=['SPY'])


def test_benchmark_is_exempt_only_behind_the_switch(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    monkeypatch.setenv('OPENCLAW_EVENT_GATE_EXEMPT_BENCH', '1')
    out = _gate({'SPY': 90_000.0, 'ZZTA': 1000.0}, {}, bench=['SPY'])
    assert out['SPY'] == 90_000.0 and 'ZZTA' not in out


def test_bench_exempt_token_tracks_the_switch(monkeypatch, caplog):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE_EXEMPT_BENCH', '1')
    with caplog.at_level(logging.INFO, logger=rbs.logger.name):
        _gate({'ZZTA': 1000.0}, {}, bench=['SPY'])
    assert 'bench_exempt=1' in [m for m in (r.getMessage() for r in caplog.records)
                                if m.startswith('[event_gate] ')][-1]


def test_gate_never_reads_the_regime():
    import inspect
    src = inspect.getsource(rbs._apply_macro_event_gate)
    for token in ('regime', 'HIGH_VOL', 'CRISIS'):
        assert token not in src


def test_options_and_crypto_are_out_of_scope(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    target = {'AAPL260918C00250000': 400.0, 'BTC/USD': 30_000.0}
    assert _gate(target, {}) == target


# ── calendar failure is inert ───────────────────────────────────────────────

def test_calendar_failure_does_not_block_anything(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')

    def _boom(_session):
        raise RuntimeError('master unreadable')

    monkeypatch.setattr('lib.macro_events.gating_event', _boom)
    target = {'ZZTA': 1000.0}
    assert rbs._apply_macro_event_gate(dict(target), {}, session=SESSION) == target


def test_gate_is_wired_after_the_breaker_and_before_the_net_cap(monkeypatch):
    order = []
    monkeypatch.setattr(rbs, '_apply_asset_eligibility_gate',
                        lambda t, b, **k: (order.append('asset'), t)[1])
    monkeypatch.setattr(rbs, '_apply_entry_hygiene_gate',
                        lambda t, b, **k: (order.append('hygiene'), t)[1])
    monkeypatch.setattr(rbs, '_apply_account_breaker_gate',
                        lambda t, b, **k: (order.append('breaker'), t)[1])
    monkeypatch.setattr(rbs, '_apply_macro_event_gate',
                        lambda t, b, **k: (order.append('event'), t)[1])
    monkeypatch.setattr(rbs, '_apply_net_exposure_cap',
                        lambda t: (order.append('netcap'), t)[1])
    monkeypatch.setattr(rbs, '_classify_position_deltas', lambda t, b, m: [])
    rbs._emit_orders_from_targets({}, {}, 100_000.0, None, None, {}, {}, [], {},
                                  1.0, {'equity': 100_000.0}, broker={})
    assert order == ['asset', 'hygiene', 'breaker', 'event', 'netcap']


def test_macro_event_gate_has_exactly_one_call_site():
    """Same pin as test_account_breaker_sizer_gate.py — the definition plus
    this single invocation in _emit_orders_from_targets, and nothing else."""
    import inspect
    module = inspect.getsource(rbs)
    assert module.count('_apply_macro_event_gate(') == 2
