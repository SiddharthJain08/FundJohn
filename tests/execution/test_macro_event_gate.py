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
    # Fix round 1 item 2: the loader now returns (events, status); the stub
    # shape below is updated to match (was a bare `None`).
    monkeypatch.setattr(rbs, '_load_macro_event_gating', lambda s: (None, 'ok'))
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
    # Fix round 1 item 3 (hermeticity): this test passed today only because
    # the real master is absent AND SESSION (2026-09-16) happens to equal
    # this file's own EVENTS constant's CPI date — once Task 8's backfill
    # lands, the real loader would resolve a real event here and this
    # "non-event session" test would start blocking ZZTA for reasons
    # unrelated to what it tests. Stub the loader explicitly, same as every
    # other test in this file (module docstring: "every input injected —
    # no calendar read, no DB").
    monkeypatch.setattr(rbs, '_load_macro_event_gating', lambda s: (None, 'ok'))
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

def test_calendar_failure_does_not_block_anything(monkeypatch, caplog):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')

    def _boom(_session):
        raise RuntimeError('master unreadable')

    monkeypatch.setattr('lib.macro_events.gating_event', _boom)
    target = {'ZZTA': 1000.0}
    with caplog.at_level(logging.INFO, logger=rbs.logger.name):
        out = rbs._apply_macro_event_gate(dict(target), {}, session=SESSION)
    assert out == target
    event_gate_records = [r for r in caplog.records
                          if r.getMessage().startswith('[event_gate] ')]
    # Fix round 1 item 4: the loader's own failure line is ERROR-level and
    # [event_gate]-prefixed, for grep parity with the summary line below.
    assert any(r.levelno == logging.ERROR for r in event_gate_records)
    # Fix round 1 item 2: a raising loader must render distinguishably from
    # a healthy "no event today" — the summary line (logged last) says WHY.
    summary = event_gate_records[-1].getMessage()
    assert 'events=unavailable:failed' in summary


def test_missing_master_reports_unavailable_and_blocks_nothing(monkeypatch, caplog):
    # Item 2's OTHER status: the master parquet absent from disk, as
    # distinct from a raise. Armed (not shadow) so `out == target` actually
    # demonstrates "nothing was blocked", not merely shadow's unconditional
    # pass-through.
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    monkeypatch.setattr(rbs, '_load_macro_event_gating', lambda s: (None, 'missing'))
    target = {'ZZTA': 1000.0}
    with caplog.at_level(logging.INFO, logger=rbs.logger.name):
        out = rbs._apply_macro_event_gate(dict(target), {}, session=SESSION,
                                          events=None)
    assert out == target
    line = [m for m in (r.getMessage() for r in caplog.records)
            if m.startswith('[event_gate] ')][-1]
    assert 'events=unavailable:missing' in line


def test_loader_itself_reports_missing_when_the_master_is_absent(monkeypatch, tmp_path):
    """Narrow, deliberate exception to this file's "no calendar read, no DB"
    promise: this specifically exercises _load_macro_event_gating's own
    filesystem-presence check, so it points OPENCLAW_MACRO_EVENTS_PATH at a
    synthetic tmp_path file rather than mocking the check away."""
    monkeypatch.setenv('OPENCLAW_MACRO_EVENTS_PATH', str(tmp_path / 'nope.parquet'))
    assert rbs._load_macro_event_gating(SESSION) == (None, 'missing')


# ── session defaults to the ET date, not the UTC date ───────────────────────

def test_session_defaults_to_the_et_date_not_the_utc_date(monkeypatch):
    """Fix round 1 item 1: this host's clock is UTC. 2026-09-17 03:30 UTC is
    2026-09-16 23:30 Eastern (EDT, in effect this week) — late evening, but
    still calendar day T=2026-09-16 in New York. date.today() on this host
    would already read 2026-09-17 (T+1) at that instant; the gate must
    resolve session=2026-09-16 via the ET wall clock instead, exactly like
    account_breaker.py:902 (`datetime.now(_ET).date()`)."""
    frozen = dt.datetime(2026, 9, 17, 3, 30, tzinfo=dt.timezone.utc)

    class _FrozenDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz) if tz is not None else frozen

    monkeypatch.setattr(rbs, 'datetime', _FrozenDatetime)
    seen_sessions = []
    monkeypatch.setattr(rbs, '_load_macro_event_gating',
                        lambda s: (seen_sessions.append(s), (None, 'ok'))[1])
    rbs._apply_macro_event_gate({}, {})
    assert seen_sessions == [dt.date(2026, 9, 16)]


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
