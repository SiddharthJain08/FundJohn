"""tests/test_sizer_live_eod_main.py — SP-6 Phase A driver-level EOD wiring.

Covers the gap that caused the first-live-fill abort (2026-06-02): the DRIVER
`regime_blended_sizer_live.main()` was never made EOD-aware. The library
(`regime_blended_sizer.size_positions`) self-loads the APPROVED carried set when
OPENCLAW_EOD_RECONCILE=1, but the driver bailed at `read_handoff() -> None ->
return 1` BEFORE ever calling size_positions, so the library's EOD path was
unreachable from the 3:55pm into-close cron.

The library EOD path itself is tested in test_sizer_sp6_eod_mode.py. THIS file
pins the driver wiring:

  1. eod-reaches-sizer: gate ON + no handoff ⇒ main() does NOT return 1; it
     resolves regime from market_regime and calls size_positions(signals=[]).
  2. gate-off-byte-identical: gate OFF + no handoff ⇒ main() returns 1 and never
     calls size_positions (verbatim legacy behaviour).
  3. payload-carries-regime: in EOD mode the synthetic handoff starts with
     regime={}, but the persisted payload must carry the RESOLVED regime (so the
     trade report's `sized.get('regime')` isn't '?'). Guards the regime backfill.

All DB / Alpaca / sizer calls are stubbed — no Postgres or broker required.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import execution.regime_blended_sizer_live as drv


class _FakeCursor:
    """Minimal RealDictCursor stand-in. Returns a regime row only for the
    market_regime fallback query; None/[] for everything else so params fall
    back to defaults and strategy_state/recs are empty."""
    def __init__(self):
        self._last = ''

    def execute(self, sql, args=None):
        self._last = sql

    def fetchone(self):
        if 'market_regime' in self._last:
            return {'state': 'LOW_VOL'}
        return None

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeConn:
    def cursor(self, *a, **k):
        return _FakeCursor()

    def close(self):
        pass


@pytest.fixture
def driver_env(monkeypatch):
    """Stub POSTGRES_URI, psycopg2.connect, the Alpaca account fetch, and
    read_handoff. Returns the read_handoff mock so tests can assert call state.
    size_positions / finalize_sized_payload are patched per-test."""
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://test/test')
    monkeypatch.setattr(drv.psycopg2, 'connect', lambda *a, **k: _FakeConn())
    # Account fetch is imported lazily inside main() from execution.alpaca_trader.
    import execution.alpaca_trader as _at
    monkeypatch.setattr(_at, '_alpaca_session', lambda: object(), raising=False)
    monkeypatch.setattr(_at, '_fetch_account_state',
                        lambda sess: {'equity': 100_000.0, 'regt_buying_power': 400_000.0,
                                      'long_market_value': 0.0, 'cash': 100_000.0},
                        raising=False)
    rh = mock.Mock(return_value=None)
    monkeypatch.setattr(drv, 'read_handoff', rh)
    monkeypatch.setattr(sys, 'argv', ['regime_blended_sizer_live', '--date', '2026-06-03'])
    return rh


def test_eod_mode_no_handoff_reaches_size_positions(driver_env, monkeypatch):
    """Gate ON + no handoff: main() must reach size_positions (not return 1),
    with regime resolved from the market_regime DB fallback, and must NOT
    depend on read_handoff."""
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    captured = {}

    def capturing_size_positions(**kwargs):
        captured.update(kwargs)
        return []   # empty → main returns 0 before finalize

    monkeypatch.setattr(drv, 'size_positions', capturing_size_positions)

    rc = drv.main()

    assert rc == 0, 'EOD mode with no handoff must proceed, not abort with rc=1'
    assert captured, 'size_positions must be called in EOD mode'
    assert captured['signals'] == [], 'driver passes empty signals; library self-loads APPROVED'
    assert captured['regime'] == {'state': 'LOW_VOL'}, 'regime must come from market_regime fallback'
    assert not driver_env.called, 'EOD mode must not depend on read_handoff'


def test_gate_off_no_handoff_returns_1(driver_env, monkeypatch):
    """Gate OFF + no handoff: verbatim legacy — return 1, never size."""
    monkeypatch.delenv('OPENCLAW_EOD_RECONCILE', raising=False)
    called = {'n': 0}

    def must_not_size(**kwargs):
        called['n'] += 1
        return []

    monkeypatch.setattr(drv, 'size_positions', must_not_size)

    rc = drv.main()

    assert rc == 1, 'gate-off legacy path must still return 1 when no handoff'
    assert called['n'] == 0, 'size_positions must not be called when bailing on no handoff'
    assert driver_env.called, 'legacy path reads the handoff'


def test_eod_mode_payload_carries_resolved_regime(driver_env, monkeypatch):
    """EOD mode: the synthetic handoff starts regime={}, but the persisted
    payload must carry the resolved regime so the trade report isn't '?'."""
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')

    one_order = {
        'ticker': 'AAPL', 'strategy_id': 'S_a', 'direction': 'long',
        'notional_usd': 10_000.0, 'pct_nav': 0.1, 'shares': 0,
        'entry': 100.0, 'stop': 95.0, 't1': 110.0, 't2': None,
        'kelly_final': 0.1, 'ev': 0.0, 'p_t1': 0.5, 'source_mode': 'sharpe_cadence',
        'target_usd': 10_000.0, 'current_usd': 0.0, 'contributing_strategies': ['S_a'],
    }
    monkeypatch.setattr(drv, 'size_positions', lambda **k: [one_order])

    captured = {}

    def capturing_finalize(run_date, payload, source):
        captured['payload'] = payload
        return True

    monkeypatch.setattr(drv, 'finalize_sized_payload', capturing_finalize)

    rc = drv.main()

    assert rc == 0
    assert captured['payload']['regime'] == {'state': 'LOW_VOL'}, \
        'persisted payload must carry the resolved regime, not the synthetic {}'
