"""tests/test_option_close_path.py — SP-5 Phase 1b (G3 + G4).

G3 — malformed-spec fail-closed in regime_blended_sizer_live._build_sized_payload:
  an option-class order whose spec fails OptionSpec.from_dict (or, when NOT close_only,
  whose direction fails normalization) is DROPPED from payload['orders'] + loud log.
  Equity orders are byte-identical (no option markers → untouched).

G4a — executor close-dispatch generalization: close_only + any non-'single' structure
  (including the synthesized 'held_legs') routes through _route_mleg_close; close_only +
  'single' keeps the existing _route_option_close path; the 5 known structures are
  byte-identical (same branch).

G4b — option orphan-close emission in regime_blended_sizer._sharpe_cadence_path:
  _held_option_underlyings() groups held OCC legs by underlying root; the sizer emits a
  CLOSE order for each held option underlying with NO live option target, gated on
  OPENCLAW_OPTION_EXEC=1 (gate OFF ⇒ zero CLI calls, zero close orders).

E2E seam (G4 integration): an option close order flows sizer → sizer_live payload
  (close_only derived True, spec survives from_dict) → executor dispatch reaches
  _route_mleg_close, with ONLY the broker CLI mocked.
"""
from __future__ import annotations

import sys
import logging
from datetime import date
from pathlib import Path
import unittest.mock as _mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import execution.regime_blended_sizer as _sizer
import execution.regime_blended_sizer_live as _live
import execution.alpaca_executor as _ex
from strategies.base import OptionSpec


_STRADDLE_SPEC = {'underlying': 'SPY', 'structure': 'straddle', 'hedge': 'delta',
                  'strike_rule': 'atm', 'right': 'both'}


# ===========================================================================
# G3 — malformed-spec / unnormalizable-direction fail-closed in _build_sized_payload
# ===========================================================================

def _opt_open_order(ticker='SPY', spec=None, direction='long', target_usd=5000.0):
    """An option order as _consolidate_option_orders emits it: None brackets, so it
    lands in _build_sized_payload's order_oc (close_only) branch."""
    return {
        'ticker': ticker, 'strategy_id': 'S_opt', 'direction': direction,
        'notional_usd': 5000.0, 'pct_nav': 0.05, 'shares': 0,
        'entry': None, 'stop': None, 't1': None, 't2': None,
        'kelly_final': 0.05, 'ev': 0.0, 'p_t1': 0.5, 'source_mode': 'sharpe_cadence',
        'target_usd': target_usd, 'current_usd': 0.0,
        'contributing_strategies': ['S_opt'], 'flip_action': None, 'action': 'CLOSE',
        'instrument_class': 'option',
        'option_spec': spec if spec is not None else dict(_STRADDLE_SPEC),
    }


def _equity_orphan_order(ticker='AAPL'):
    """An equity orphan-close order (None brackets, no option markers)."""
    return {
        'ticker': ticker, 'strategy_id': '__close_orphan__', 'direction': 'long',
        'notional_usd': 3000.0, 'current_usd': 3000.0, 'target_usd': 0.0,
        'entry': None, 'stop': None, 't1': None, 't2': None,
        'source_mode': 'sharpe_cadence',
    }


def test_g3_malformed_spec_dropped(caplog):
    """Option order with a spec that fails from_dict (no underlying) → ABSENT from
    payload['orders'] + error logged."""
    bad = _opt_open_order(spec={'structure': 'straddle'})  # no underlying → from_dict None
    with caplog.at_level(logging.ERROR):
        payload = _live._build_sized_payload([bad], {'cycle_date': '2026-06-03', 'regime': {}})
    assert payload['orders'] == [], f'malformed-spec option order must be dropped, got {payload["orders"]}'
    assert any('SPY' in r.message and 'S_opt' in r.message for r in caplog.records), \
        'a loud error naming ticker + strategy_id must be logged'


def test_g3_wellformed_spec_kept_and_routed():
    """A well-formed option close order survives: instrument_class + reconstructed
    OptionSpec carried, close_only=True (None brackets), direction normalized."""
    good = _opt_open_order(spec=dict(_STRADDLE_SPEC), direction='long', target_usd=0.0)
    payload = _live._build_sized_payload([good], {'cycle_date': '2026-06-03', 'regime': {}})
    assert len(payload['orders']) == 1
    o = payload['orders'][0]
    assert o['instrument_class'] == 'option'
    assert isinstance(o['option_spec'], OptionSpec)
    assert o['option_spec'].underlying == 'SPY'
    assert o['close_only'] is True
    assert o['direction'] == 'long'


def test_g3_equity_orders_byte_identical():
    """An equity orphan-close order (no option markers) is untouched: no
    instrument_class / option_spec keys injected, close_only=True as before."""
    eq = _equity_orphan_order('AAPL')
    payload = _live._build_sized_payload([eq], {'cycle_date': '2026-06-03', 'regime': {}})
    assert len(payload['orders']) == 1
    o = payload['orders'][0]
    assert o['ticker'] == 'AAPL'
    assert 'instrument_class' not in o
    assert 'option_spec' not in o
    assert o['close_only'] is True


def test_g3_unnormalizable_direction_open_dropped(caplog):
    """Option OPEN order (target_usd != 0 ⇒ NOT close) with an unnormalizable direction
    ('WAT') → dropped + error logged (would otherwise let a raw direction route)."""
    bad = _opt_open_order(spec=dict(_STRADDLE_SPEC), direction='WAT', target_usd=5000.0)
    with caplog.at_level(logging.ERROR):
        payload = _live._build_sized_payload([bad], {'cycle_date': '2026-06-03', 'regime': {}})
    assert payload['orders'] == [], f'unnormalizable-direction option open must be dropped, got {payload["orders"]}'
    assert any('SPY' in r.message and 'S_opt' in r.message for r in caplog.records)


def test_g3_close_only_exempt_from_direction_normalization():
    """A close_only option order (target_usd == 0) with a non-normalizable direction
    survives — closes don't depend on direction (the held-legs close is direction-free).
    The synthesized 'held_legs' spec survives from_dict (underlying present)."""
    close = _opt_open_order(spec={'underlying': 'SPY', 'structure': 'held_legs'},
                            direction='WAT', target_usd=0.0)
    payload = _live._build_sized_payload([close], {'cycle_date': '2026-06-03', 'regime': {}})
    assert len(payload['orders']) == 1, 'close_only option order must NOT be dropped on direction'
    o = payload['orders'][0]
    assert o['instrument_class'] == 'option'
    assert isinstance(o['option_spec'], OptionSpec)
    assert o['option_spec'].structure == 'held_legs'
    assert o['close_only'] is True


# ===========================================================================
# G4a — executor close-dispatch generalization
# ===========================================================================

_HELD = ['SPY260718C00500000', 'SPY260718C00515000']


def _close_order(structure):
    spec = OptionSpec(underlying='SPY', structure=structure, right='call', strike_rule='atm')
    return {'ticker': 'SPY', 'instrument_class': 'option', 'direction': 'long',
            'option_spec': spec, 'close_only': True}, spec


@pytest.mark.parametrize('structure', ['straddle', 'strangle', 'vertical',
                                       'credit_vertical', 'iron_condor'])
def test_g4a_known_structures_route_mleg_close(monkeypatch, structure):
    """Each of the 5 known structures + close_only → _route_mleg_close (byte-identical
    to before: same branch taken)."""
    order, spec = _close_order(structure)
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(_ex, '_options_session_gate', lambda: (True, ''))
    called = {}
    monkeypatch.setattr(_ex, '_route_mleg_close',
                        lambda o, s, c: called.update(spec=s, coid=c) or {'status': 'submitted', 'structure': s.structure})
    res = _ex._route_option_order(order, equity=100_000.0, coid=f'c_{structure}')
    assert called.get('spec') is spec, f'{structure} close must dispatch to _route_mleg_close'
    assert res['structure'] == structure


def test_g4a_held_legs_routes_mleg_close(monkeypatch):
    """The synthesized 'held_legs' structure + close_only → _route_mleg_close (the
    generalized condition: close_only and structure != 'single')."""
    order, spec = _close_order('held_legs')
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(_ex, '_options_session_gate', lambda: (True, ''))
    called = {}
    monkeypatch.setattr(_ex, '_route_mleg_close',
                        lambda o, s, c: called.update(spec=s) or {'status': 'submitted'})
    res = _ex._route_option_order(order, equity=100_000.0, coid='hl1')
    assert called.get('spec') is spec, "'held_legs' close must dispatch to _route_mleg_close"
    assert res['status'] == 'submitted'


def test_g4a_single_close_uses_single_path(monkeypatch):
    """close_only + structure='single' keeps the existing _route_option_close path —
    NOT _route_mleg_close."""
    spec = OptionSpec(underlying='SPY', structure='single', right='call', strike_rule='atm')
    order = {'ticker': 'SPY', 'instrument_class': 'option', 'direction': 'long',
             'option_spec': spec, 'close_only': True}
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(_ex, '_options_session_gate', lambda: (True, ''))
    mleg_called = {'n': 0}
    single_called = {'n': 0}
    monkeypatch.setattr(_ex, '_route_mleg_close',
                        lambda o, s, c: mleg_called.update(n=mleg_called['n'] + 1) or {'status': 'submitted'})
    monkeypatch.setattr(_ex, '_route_option_close',
                        lambda o, occ, c: single_called.update(n=single_called['n'] + 1) or {'status': 'submitted'})
    # single needs strike/expiry resolution before the close branch; stub them.
    monkeypatch.setattr(_ex, '_resolve_expiry', lambda s, t: date(2026, 7, 18))
    monkeypatch.setattr(_ex, '_resolve_strike', lambda s, t, e: 500.0)
    _ex._route_option_order(order, equity=100_000.0, coid='s1')
    assert mleg_called['n'] == 0, 'single close must NOT route through _route_mleg_close'
    assert single_called['n'] == 1, 'single close must use _route_option_close'


# ===========================================================================
# G4b — _held_option_underlyings parsing + sizer orphan-close emission
# ===========================================================================

def test_g4b_held_option_underlyings_parses_fixture(monkeypatch):
    """Parses a position-list fixture (OCC legs of 2 underlyings + equity + crypto)
    into {underlying: [occ, ...]}, ignoring non-OCC rows."""
    positions = [
        {'symbol': 'SPY260718C00500000', 'qty': '1', 'market_value': '500'},
        {'symbol': 'SPY260718C00515000', 'qty': '-1', 'market_value': '-300'},
        {'symbol': 'IWM260815P00200000', 'qty': '2', 'market_value': '800'},
        {'symbol': 'AAPL', 'qty': '100', 'market_value': '20000'},   # equity
        {'symbol': 'BTC/USD', 'qty': '0.5', 'market_value': '30000'},  # crypto
        {'symbol': 'SPY260718C00505000', 'qty': '0', 'market_value': '0'},  # zero qty → skip
    ]

    class _Proc:
        returncode = 0
        stdout = __import__('json').dumps(positions).encode()
        stderr = b''

    monkeypatch.setattr('subprocess.run', lambda *a, **k: _Proc())
    out = _sizer._held_option_underlyings()
    assert set(out.keys()) == {'SPY', 'IWM'}, f'only OCC underlyings expected, got {out}'
    assert sorted(out['SPY']) == ['SPY260718C00500000', 'SPY260718C00515000']
    assert out['IWM'] == ['IWM260815P00200000']


def test_g4b_held_option_underlyings_failsafe(monkeypatch):
    """Any subprocess error → None (SP-5.3 C4: distinguishable from a flat book {};
    callers fail-closed for opens). Never raises."""
    def _boom(*a, **k):
        raise RuntimeError('cli down')
    monkeypatch.setattr('subprocess.run', _boom)
    assert _sizer._held_option_underlyings() is None


def _sig(sid, ticker, direction=1, sharpe=5.0, weight=1.0, option_spec=None):
    sp = {}
    if option_spec is not None:
        sp['option_spec'] = option_spec
    return {'strategy_id': sid, 'ticker': ticker, 'direction': direction,
            'signal_date': date(2026, 6, 3), 'entry_price': 100.0, 'stop_loss': 95.0,
            'target_1': 110.0, 'target_2': None, 'signal_params': sp,
            '_w': weight, '_s': sharpe}


def _run_sizer(monkeypatch, signals, broker=None, min_cum_sharpe=0.0):
    broker = broker or {}
    account = {'equity': 100_000, 'regt_buying_power': 400_000,
               'long_market_value': 0, 'cash': 100_000, 'buying_power': 400_000}
    params = {'liquidity_param': 1.0, 'min_signal_notional_usd': 0,
              'min_cumulative_sharpe': min_cum_sharpe}
    seen = {}
    for s in signals:
        seen[s['strategy_id']] = (s.get('_w', 1.0), s.get('_s', 5.0))
    weight_rows = [{'strategy_id': sid, 'daily_weight': w, 'effective_sharpe': sh,
                    'cadence_days': 1.0} for sid, (w, sh) in seen.items()]
    monkeypatch.setattr(_sizer, '_load_active_window_signals',
                        lambda regime_state, wbs, cbs: list(signals))
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: dict(broker))
    monkeypatch.delenv('OPENCLAW_EOD_RECONCILE', raising=False)
    monkeypatch.delenv('OPENCLAW_OPTION_DELTA_HEDGE', raising=False)
    with _mock.patch('execution.strategy_weights.load_current', return_value=weight_rows):
        return _sizer._sharpe_cadence_path(
            signals=[], account_state=account, regime_state='LOW_VOL',
            params=params, confirmer=None)


def _run_sizer_eod(monkeypatch, signals, broker=None, min_cum_sharpe=0.0):
    """Drive _sharpe_cadence_path through the PRODUCTION path (OPENCLAW_EOD_RECONCILE=1
    ⇒ _load_approved_carried_signals) — the only path that runs in production (SP-6 EOD)."""
    broker = broker or {}
    account = {'equity': 100_000, 'regt_buying_power': 400_000,
               'long_market_value': 0, 'cash': 100_000, 'buying_power': 400_000}
    params = {'liquidity_param': 1.0, 'min_signal_notional_usd': 0,
              'min_cumulative_sharpe': min_cum_sharpe}
    seen = {}
    for s in signals:
        seen[s['strategy_id']] = (s.get('_w', 1.0), s.get('_s', 5.0))
    weight_rows = [{'strategy_id': sid, 'daily_weight': w, 'effective_sharpe': sh,
                    'cadence_days': 1.0} for sid, (w, sh) in seen.items()]
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals', lambda wbs, cad=None, **_kw: list(signals))
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: dict(broker))
    # The EOD lane is defined by SIGNAL_REGISTER (the timing model), not by
    # RECONCILE (which only means "the premarket reconcile job is enabled" and
    # is ALSO 1 in same-day mode). Real EOD mode sets both; this fixture drove
    # _load_approved_carried_signals while setting only RECONCILE, so it passed
    # for the wrong reason until the 2026-07-31 lane fix.
    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.delenv('OPENCLAW_OPTION_DELTA_HEDGE', raising=False)
    with _mock.patch('execution.strategy_weights.load_current', return_value=weight_rows):
        return _sizer._sharpe_cadence_path(
            signals=[], account_state=account, regime_state='LOW_VOL',
            params=params, confirmer=None)


def test_g4b_emits_close_in_eod_production_path(monkeypatch):
    """PRODUCTION-PATH (OPENCLAW_EOD_RECONCILE=1): the orphan-close emission fires
    identically — it sits AFTER the mode-specific load, so the carried-set path emits
    the same option CLOSE order. This guards the only path that runs in SP-6 EOD."""
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(_sizer, '_held_option_underlyings',
                        lambda: {'SPY': ['SPY260718C00500000', 'SPY260718C00515000']})
    orders = _run_sizer_eod(monkeypatch, [_sig('S_eq', 'AAPL', direction=1)])
    closes = [o for o in orders if o.get('strategy_id') == '__close_option_orphan__']
    assert len(closes) == 1, f'orphan-close must emit in EOD mode, got {orders}'
    assert closes[0]['ticker'] == 'SPY'
    assert closes[0]['option_spec'] == {'underlying': 'SPY', 'structure': 'held_legs'}


def test_g4b_emits_close_for_orphan_held_underlying(monkeypatch):
    """OPENCLAW_OPTION_EXEC=1 + held legs for SPY + NO option target for SPY ⇒ emit an
    option CLOSE order (the documented shape)."""
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(_sizer, '_held_option_underlyings',
                        lambda: {'SPY': ['SPY260718C00500000', 'SPY260718C00515000']})
    # Only an equity AAPL signal — no option target on SPY.
    orders = _run_sizer(monkeypatch, [_sig('S_eq', 'AAPL', direction=1)])
    closes = [o for o in orders if o.get('strategy_id') == '__close_option_orphan__']
    assert len(closes) == 1, f'one option orphan-close expected, got {orders}'
    c = closes[0]
    assert c['ticker'] == 'SPY'
    assert c['instrument_class'] == 'option'
    assert c['option_spec'] == {'underlying': 'SPY', 'structure': 'held_legs'}
    assert c['action'] == 'CLOSE'
    assert c['target_usd'] == 0.0 and c['current_usd'] == 0.0
    assert c['entry'] is None and c['stop'] is None and c['t1'] is None
    assert c['contributing_strategies'] == ['__close_option_orphan__']


def test_g4b_no_close_when_live_option_target(monkeypatch):
    """Held legs for SPY WITH a live option target for SPY ⇒ NO orphan-close (the
    structure is still wanted; it will be refreshed, not closed).

    An equity signal is included because option consolidation sizes at the equity-path
    scale (5.1b-ii hedge model): with no equity signal the option order is fail-closed
    dropped (no scale) — so a live option target REQUIRES a non-empty equity book."""
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    # SP-5.3 C3 (c40b475, 2026-06-04): held+still-targeted structures with min DTE <=
    # OPTION_EXPIRY_CLOSE_DTE (7) now force-close as '__close_option_expiry__'. The old
    # hardcoded leg SPY260718C00500000 date-rotted into that window; build a far-dated
    # leg (today+90d) so this test keeps pinning the NOT-near-expiry branch: keep held,
    # suppress open, emit NO close of any kind.
    _exp = date.today() + __import__('datetime').timedelta(days=90)
    _far_leg = f'SPY{_exp.strftime("%y%m%d")}C00500000'
    monkeypatch.setattr(_sizer, '_held_option_underlyings',
                        lambda: {'SPY': [_far_leg]})
    orders = _run_sizer(monkeypatch, [
        _sig('S_eq_aapl', 'AAPL', direction=1),
        _sig('S_opt_spy', 'SPY', direction='BUY_VOL', option_spec=_STRADDLE_SPEC),
    ])
    closes = [o for o in orders if o.get('strategy_id') in
              ('__close_option_orphan__', '__close_option_expiry__')]
    assert closes == [], f'no close when SPY has a live target and is far from expiry, got {orders}'
    # SP-5.3 (C2): the open for held SPY is SUPPRESSED (stacking guard) — the
    # held structure is simply kept; it is neither closed nor re-opened.
    opt = [o for o in orders if o.get('instrument_class') == 'option' and o['ticker'] == 'SPY']
    assert opt == [], f'held SPY open must be suppressed (SP-5.3), got {opt}'


def test_g4b_gate_off_no_cli_no_close(monkeypatch):
    """Gate OFF (OPENCLAW_OPTION_EXEC unset) ⇒ _held_option_underlyings is NEVER called
    (zero extra CLI work) and zero option orphan-close orders."""
    monkeypatch.delenv('OPENCLAW_OPTION_EXEC', raising=False)
    called = {'n': 0}

    def _spy():
        called['n'] += 1
        return {'SPY': ['SPY260718C00500000']}
    monkeypatch.setattr(_sizer, '_held_option_underlyings', _spy)
    orders = _run_sizer(monkeypatch, [_sig('S_eq', 'AAPL', direction=1)])
    assert called['n'] == 0, '_held_option_underlyings must NOT be called when gate OFF'
    closes = [o for o in orders if o.get('strategy_id') == '__close_option_orphan__']
    assert closes == []


# ===========================================================================
# E2E seam — sizer → sizer_live payload → executor dispatch (only CLI mocked)
# ===========================================================================

def test_e2e_option_close_reaches_mleg_close(monkeypatch):
    """The seam mocked-both-sides bugs hide in: an option orphan-close flows from the
    REAL sizer through the REAL _build_sized_payload into the REAL _route_option_order,
    landing in _route_mleg_close. Only the broker `position list`/`position close` CLI
    is mocked."""
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(_sizer, '_held_option_underlyings',
                        lambda: {'SPY': ['SPY260718C00500000', 'SPY260718C00515000']})
    # Sizer: equity-only signal so SPY is an orphan-held option underlying.
    orders = _run_sizer(monkeypatch, [_sig('S_eq', 'AAPL', direction=1)])
    raw_close = next(o for o in orders if o.get('strategy_id') == '__close_option_orphan__')

    # Sizer_live: build the executor-facing payload (real _build_sized_payload).
    payload = _live._build_sized_payload([raw_close], {'cycle_date': '2026-06-03', 'regime': {}})
    sized = payload['orders'][0]
    assert sized['instrument_class'] == 'option'
    assert isinstance(sized['option_spec'], OptionSpec)
    assert sized['close_only'] is True

    # Executor: only the broker CLI is mocked; the real dispatch must reach _route_mleg_close.
    monkeypatch.setattr(_ex, '_options_session_gate', lambda: (True, ''))
    monkeypatch.setattr(_ex, '_held_option_legs',
                        lambda und: ['SPY260718C00500000', 'SPY260718C00515000'])
    cli_calls = []
    monkeypatch.setattr(_ex, '_run_alpaca_cli',
                        lambda args, **kw: cli_calls.append(args) or (True, {'status': 'accepted'}, None))
    # Avoid a real DB connect in the hedge-deactivation call.
    monkeypatch.setattr(_ex, '_deactivate_hedge_for_underlying_guarded', lambda und: 0)

    res = _ex._route_option_order(sized, equity=100_000.0, coid='e2e1')
    assert res is not None and res['status'] == 'submitted'
    assert res['structure'] == 'held_legs'
    # Each held leg was closed per-leg.
    assert all(c[:2] == ['position', 'close'] for c in cli_calls)
    assert len(cli_calls) == 2


# ===========================================================================
# Phase 1b follow-up — option OPEN through the sizer must NOT be close_only
# (the implementer-surfaced pre-promotion gap: _consolidate_option_orders emits
# None brackets, so opens landed in the close-only branch → _route_mleg_close
# no-op → a promoted option strategy would never open a position)
# ===========================================================================

def test_option_open_emits_open_not_close():
    """An option order with target_usd != 0 (an OPEN) emits WITHOUT close_only:
    spec carried, direction normalized, notional preserved — the executor sizes
    qty from notional via _resolve_option_qty."""
    o = _opt_open_order(spec=dict(_STRADDLE_SPEC), direction='BUY_VOL', target_usd=5000.0)
    o['action'] = 'open_long'
    payload = _live._build_sized_payload([o], {'cycle_date': '2026-06-03', 'regime': {}})
    assert len(payload['orders']) == 1
    out = payload['orders'][0]
    assert not out.get('close_only'), 'an option OPEN must not be close_only'
    assert out['instrument_class'] == 'option'
    assert isinstance(out['option_spec'], OptionSpec)
    assert out['direction'] == 'long', 'BUY_VOL must normalize to long'
    assert out['notional_usd'] == 5000.0
    assert out['action'] == 'open_long'


def test_option_open_dispatches_to_open_route(monkeypatch):
    """Dispatch-level: an open option order (no close_only) does NOT hit
    _route_mleg_close — it proceeds into the open mleg path."""
    import execution.alpaca_executor as ex
    from strategies.base import OptionSpec as _OS
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(ex, '_options_session_gate', lambda: (True, ''))
    monkeypatch.setattr(ex, '_resolve_expiry', lambda spec, today: __import__('datetime').date(2026, 7, 18))
    called = {'close': 0}
    monkeypatch.setattr(ex, '_route_mleg_close', lambda *a, **k: called.__setitem__('close', called['close'] + 1) or {'status': 'submitted'})
    monkeypatch.setattr(ex, '_resolve_structure_legs', lambda s, t, e: [('call', 500.0, 'buy'), ('put', 500.0, 'buy')])
    monkeypatch.setattr(ex, '_structure_net_quote', lambda s, l, e: (20.0, [('SPY260718C00500000', 'call', 'buy'), ('SPY260718P00500000', 'put', 'buy')]))
    monkeypatch.setattr(ex, '_resolve_option_qty', lambda d, lim: (1, None))
    monkeypatch.setattr(ex, '_build_mleg_legs_json', lambda lq: '[]')
    monkeypatch.setattr(ex, '_run_alpaca_cli', lambda args: (True, {'id': 'ord-9'}, None))
    monkeypatch.delenv('POSTGRES_URI', raising=False)
    order = {'ticker': 'SPY', 'instrument_class': 'option', 'direction': 'long',
             'notional_usd': 5000.0,
             'option_spec': _OS(underlying='SPY', structure='straddle', hedge='none', strike_rule='atm')}
    res = ex._route_option_order(order, equity=100_000.0, coid='c-open')
    assert called['close'] == 0, 'open must not dispatch to _route_mleg_close'
    assert res is not None and res.get('status') == 'submitted'
