"""C1: while the account breaker is halted the sizer must refuse alpha OPENS
and ADDS. Exits, reductions and orphan closes are untouched, and benchmark
tickers are exempt (S_beta_spy positions and entries are never affected).

All inputs injected — no DB.
"""
from __future__ import annotations

import importlib

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


# ── the short side, through the gate (fix round 1 item 4) ──────────────────

def test_halted_blocks_a_new_short_open():
    out = _gate({'AMD': -5000.0}, {})
    assert 'AMD' not in out


def test_halted_caps_a_short_add_at_the_held_size():
    out = _gate({'AMD': -9000.0}, {'AMD': -4000.0})
    assert out['AMD'] == -4000.0


def test_halted_never_blocks_a_short_reduction():
    out = _gate({'AMD': -1000.0}, {'AMD': -4000.0})
    assert out['AMD'] == -1000.0


# ── conservation: shaved, never redistributed (fix round 1 item 4) ─────────

def test_conservation_untouched_targets_are_byte_identical_and_no_new_keys():
    """A halted run only ever removes or shrinks the tickers it touches.
    Nothing shed here is added back anywhere else, and the surviving values
    for tickers that ARE shed match _clamp_to_held's own contract exactly."""
    target = {
        'AAPL': 5000.0,    # unheld open -> blocked (shed entirely)
        'MSFT': 9000.0,    # add, held 4000 -> capped at the held size
        'GOOG': 1000.0,    # reduction, held 4000 -> untouched
        'SPY': 90_000.0,   # benchmark -> untouched regardless of held size
    }
    broker = {'MSFT': 4000.0, 'GOOG': 4000.0, 'SPY': 40_000.0}
    out = _gate(target, broker, bench=['SPY'])
    assert out == {'MSFT': 4000.0, 'GOOG': 1000.0, 'SPY': 90_000.0}


# ── halted=None resolves via the loader (fix round 1 item 4) ───────────────

def test_gate_resolves_halted_via_the_loader_when_none(monkeypatch):
    """Exercises the production path: halted left None -> the gate calls
    _load_account_breaker_halted() itself, not just the injected override."""
    monkeypatch.setattr(rbs, '_load_account_breaker_halted', lambda: True)
    out = rbs._apply_account_breaker_gate({'AAPL': 5000.0}, {}, halted=None)
    assert 'AAPL' not in out


def test_halted_ignores_option_and_crypto_symbols():
    target = {'AAPL260918C00250000': 400.0, 'BTC/USD': 30_000.0}
    assert _gate(target, {}) == target


def test_empty_targets_short_circuit():
    assert _gate({}, {'AAPL': 4000.0}) == {}


# ── the belt-and-braces flag gate (fix round 1 item 3) ──────────────────────
# Same context-manager idiom as test_ownership_sizer_block.py's
# _FakeCtxCursor/_FakeCtxConn: _load_account_breaker_halted does
# `with psycopg2.connect(...) as c, c.cursor() as cur:`, so both the
# connection AND the cursor need to be usable as context managers.

class _FakeBreakerCursor:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return self._row


class _FakeBreakerConn:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeBreakerCursor(self._row)


def test_flag_set_connect_uses_a_5s_timeout(monkeypatch):
    """Item 3: connect_timeout=5 on the psycopg2.connect call — a hung DB
    must not hang the sizer's emission tail."""
    monkeypatch.setenv('OPENCLAW_ACCOUNT_BREAKER', '1')
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://fake/db')
    seen = {}

    def _connect(*a, **k):
        seen.update(k)
        return _FakeBreakerConn((False,))

    monkeypatch.setattr(rbs.psycopg2, 'connect', _connect)
    rbs._load_account_breaker_halted()
    assert seen.get('connect_timeout') == 5


def test_flag_unset_short_circuits_with_no_connect_attempt(monkeypatch):
    """Item 3: with the flag unset, no DB read is even attempted."""
    monkeypatch.delenv('OPENCLAW_ACCOUNT_BREAKER', raising=False)
    calls = []

    def _boom(*_a, **_k):
        calls.append(1)
        raise RuntimeError('must not be called — the flag is unset')

    monkeypatch.setattr(rbs.psycopg2, 'connect', _boom)
    assert rbs._load_account_breaker_halted() is False
    assert calls == []


def test_flag_set_state_row_missing_is_false(monkeypatch):
    monkeypatch.setenv('OPENCLAW_ACCOUNT_BREAKER', '1')
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://fake/db')
    monkeypatch.setattr(rbs.psycopg2, 'connect', lambda *a, **k: _FakeBreakerConn(None))
    assert rbs._load_account_breaker_halted() is False


def test_flag_set_halted_row_is_true(monkeypatch):
    monkeypatch.setenv('OPENCLAW_ACCOUNT_BREAKER', '1')
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://fake/db')
    monkeypatch.setattr(rbs.psycopg2, 'connect', lambda *a, **k: _FakeBreakerConn((True,)))
    assert rbs._load_account_breaker_halted() is True


def test_flag_set_connect_raising_fails_open_and_logs(monkeypatch, caplog):
    """A DB hiccup must not freeze the fleet — the flatten is the hard stop.
    POSTGRES_URI is set explicitly so a missing-env KeyError can't reach the
    same fail-open branch without ever calling the patched connect."""
    monkeypatch.setenv('OPENCLAW_ACCOUNT_BREAKER', '1')
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://fake/db')

    def _boom(*_a, **_k):
        raise RuntimeError('db down')

    monkeypatch.setattr(rbs.psycopg2, 'connect', _boom)
    with caplog.at_level('WARNING', logger=rbs.logger.name):
        result = rbs._load_account_breaker_halted()
    assert result is False
    assert any('[account_breaker] halted lookup failed' in r.getMessage()
              for r in caplog.records)


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


def test_account_breaker_gate_has_exactly_one_call_site():
    """Same pin as test_entry_hygiene_gate.py:198 — the definition plus this
    single invocation in _emit_orders_from_targets, and nothing else."""
    import inspect
    module = inspect.getsource(rbs)
    assert module.count('_apply_account_breaker_gate(') == 2
