"""SP-5.3 — option expiry management + held-awareness.

C1: _occ_dte / _expiry_close_dte. C2: held-open suppression (the activation-safety
stacking guard). C3: __close_option_expiry__ emission. C4: held-fetch None contract.
Drives the REAL _sharpe_cadence_path (no stub) by monkeypatching only the DB/broker
seams — same conventions as tests/test_sizer_class_consolidation.py.
"""
import logging
from datetime import date, timedelta
from unittest import mock as _mock

import pytest

import execution.regime_blended_sizer as _sizer

_STRADDLE_SPEC = {'underlying': 'SPY', 'structure': 'straddle', 'hedge': 'delta',
                  'strike_rule': 'atm', 'right': 'both'}


def _occ(root, days_out, right='C', strike='00500000'):
    """OCC symbol expiring `days_out` calendar days from today."""
    exp = date.today() + timedelta(days=days_out)
    return f"{root}{exp.strftime('%y%m%d')}{right}{strike}"


# ===========================================================================
# C1 — _occ_dte + _expiry_close_dte
# ===========================================================================

def test_occ_dte_basic():
    today = date.today()
    assert _sizer._occ_dte(_occ('SPY', 7), today=today) == 7
    assert _sizer._occ_dte(_occ('SPY', 0), today=today) == 0
    assert _sizer._occ_dte(_occ('A', 30, right='P'), today=today) == 30   # 1-char root


def test_occ_dte_explicit_today():
    # SPY 2026-07-18 call seen from 2026-07-11 = 7 days.
    assert _sizer._occ_dte('SPY260718C00500000', today=date(2026, 7, 11)) == 7


def test_occ_dte_parse_failure_is_zero(caplog):
    """An unreadable expiry is an unmanageable position — DTE 0 (close it) + warn."""
    with caplog.at_level(logging.WARNING):
        assert _sizer._occ_dte('SPY999999C00500000') == 0
    assert any('unparseable' in r.message.lower() for r in caplog.records)


def test_expiry_close_dte_default_and_override(monkeypatch):
    monkeypatch.delenv('OPENCLAW_OPTION_EXPIRY_CLOSE_DTE', raising=False)
    assert _sizer._expiry_close_dte() == 7
    monkeypatch.setenv('OPENCLAW_OPTION_EXPIRY_CLOSE_DTE', '10')
    assert _sizer._expiry_close_dte() == 10
    monkeypatch.setenv('OPENCLAW_OPTION_EXPIRY_CLOSE_DTE', 'junk')
    assert _sizer._expiry_close_dte() == 7   # parse-guarded fallback


# ===========================================================================
# Harness — mirrors tests/test_sizer_class_consolidation.py (local convention:
# each option test file carries its own copy)
# ===========================================================================

def _account(equity=100_000, bp=None):
    a = {'equity': equity, 'regt_buying_power': 4 * equity,
         'long_market_value': 0, 'cash': equity}
    if bp is not None:
        a['buying_power'] = bp
    return a


def _params():
    return {'liquidity_param': 1.0, 'min_signal_notional_usd': 0,
            'min_cumulative_sharpe': 0.0}


def _sig(sid, ticker, direction=1, sharpe=5.0, weight=1.0, option_spec=None):
    sp = {}
    if option_spec is not None:
        sp['option_spec'] = option_spec
    return {
        'strategy_id': sid, 'ticker': ticker, 'direction': direction,
        'signal_date': date(2026, 6, 3), 'entry_price': 100.0, 'stop_loss': 95.0,
        'target_1': 110.0, 'target_2': None, 'signal_params': sp,
        '_w': weight, '_s': sharpe,
    }


def _run_sizer(monkeypatch, signals, broker=None, account=None, params=None,
               min_cum_sharpe=0.0):
    broker = broker or {}
    account = account or _account()
    params = params or _params()
    params = {**params, 'min_cumulative_sharpe': min_cum_sharpe}
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


def _run_sizer_eod(monkeypatch, signals, broker=None, account=None, params=None,
                   min_cum_sharpe=0.0):
    broker = broker or {}
    account = account or _account()
    params = params or _params()
    params = {**params, 'min_cumulative_sharpe': min_cum_sharpe}
    seen = {}
    for s in signals:
        seen[s['strategy_id']] = (s.get('_w', 1.0), s.get('_s', 5.0))
    weight_rows = [{'strategy_id': sid, 'daily_weight': w, 'effective_sharpe': sh,
                    'cadence_days': 1.0} for sid, (w, sh) in seen.items()]
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals',
                        lambda wbs, cad=None, **_kw: list(signals))
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: dict(broker))
    # EOD lane = SIGNAL_REGISTER (timing model), not RECONCILE (also 1 in
    # same-day mode). See the 2026-07-31 lane fix in regime_blended_sizer.
    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.delenv('OPENCLAW_OPTION_DELTA_HEDGE', raising=False)
    with _mock.patch('execution.strategy_weights.load_current', return_value=weight_rows):
        return _sizer._sharpe_cadence_path(
            signals=[], account_state=account, regime_state='LOW_VOL',
            params=params, confirmer=None)


# ===========================================================================
# C2 — held-open suppression (THE ACTIVATION-SAFETY STACKING GUARD)
# ===========================================================================

def test_held_open_suppressed(monkeypatch):
    """A live option target whose underlying already has held legs must NOT re-emit
    an open. Pre-5.3 this stacked a fresh mleg structure EVERY trading day the signal
    persisted in its cadence window (confirmed by direct read 2026-06-04: carried set
    re-loads daily, _consolidate_option_orders emits plain opens, executor has no held
    check). Far-dated legs (DTE 44) so no expiry close interferes."""
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(_sizer, '_held_option_underlyings',
                        lambda: {'SPY': [_occ('SPY', 44), _occ('SPY', 44, right='P')]})
    orders = _run_sizer(monkeypatch, [
        _sig('S_eq_aapl', 'AAPL', direction=1),
        _sig('S_opt_spy', 'SPY', direction='BUY_VOL', option_spec=_STRADDLE_SPEC),
    ])
    opens = [o for o in orders if o.get('instrument_class') == 'option'
             and o.get('action') != 'CLOSE']
    assert opens == [], f'held SPY must suppress the open (stacking guard), got {opens}'
    # No close either: in-targets + far from expiry = hold as-is.
    closes = [o for o in orders if o.get('instrument_class') == 'option'
              and o.get('action') == 'CLOSE']
    assert closes == []
    # Equity unaffected.
    assert any(o['ticker'] == 'AAPL' for o in orders)


def test_held_open_suppressed_eod_production_path(monkeypatch):
    """Same guard through the PRODUCTION path (OPENCLAW_EOD_RECONCILE=1) — the only
    path SP-6 EOD trades through daily."""
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(_sizer, '_held_option_underlyings',
                        lambda: {'SPY': [_occ('SPY', 44)]})
    orders = _run_sizer_eod(monkeypatch, [
        _sig('S_eq_aapl', 'AAPL', direction=1),
        _sig('S_opt_spy', 'SPY', direction='BUY_VOL', option_spec=_STRADDLE_SPEC),
    ])
    opens = [o for o in orders if o.get('instrument_class') == 'option'
             and o.get('action') != 'CLOSE']
    assert opens == []


def test_not_held_open_flows(monkeypatch):
    """Flat book ({} — genuinely empty, NOT None) → the open emits unchanged."""
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(_sizer, '_held_option_underlyings', lambda: {})
    orders = _run_sizer(monkeypatch, [
        _sig('S_eq_aapl', 'AAPL', direction=1),
        _sig('S_opt_spy', 'SPY', direction='BUY_VOL', option_spec=_STRADDLE_SPEC),
    ])
    opens = [o for o in orders if o.get('instrument_class') == 'option']
    assert len(opens) == 1 and opens[0]['ticker'] == 'SPY'


def test_gate_off_no_suppression_no_cli(monkeypatch):
    """Gate OFF ⇒ byte-identical: _held_option_underlyings NEVER called, the open
    still emits (the executor's NIT-1 skip-dict handles it downstream)."""
    monkeypatch.delenv('OPENCLAW_OPTION_EXEC', raising=False)
    called = {'n': 0}

    def _spy():
        called['n'] += 1
        return {'SPY': [_occ('SPY', 44)]}
    monkeypatch.setattr(_sizer, '_held_option_underlyings', _spy)
    orders = _run_sizer(monkeypatch, [
        _sig('S_eq_aapl', 'AAPL', direction=1),
        _sig('S_opt_spy', 'SPY', direction='BUY_VOL', option_spec=_STRADDLE_SPEC),
    ])
    assert called['n'] == 0, 'gate OFF must make zero held-legs CLI calls'
    opens = [o for o in orders if o.get('instrument_class') == 'option']
    assert len(opens) == 1, 'gate OFF: open must still emit (byte-identical)'


# ===========================================================================
# C4 — held-fetch error (None) ≠ flat book ({})
# ===========================================================================

def test_held_fetch_error_returns_none(monkeypatch):
    """Contract change: ANY broker-fetch error → None (was {}), so callers can
    distinguish 'cannot prove flat' from 'genuinely flat'."""
    def _boom(*a, **k):
        raise RuntimeError('cli down')
    monkeypatch.setattr('subprocess.run', _boom)
    assert _sizer._held_option_underlyings() is None


def test_held_fetch_error_suppresses_all_opens(monkeypatch, caplog):
    """Fetch FAILED → fail-closed for OPENS: suppress ALL option opens this cycle
    (a broker hiccup at 3:55 must not stack an open onto a held underlying).
    Equity is untouched."""
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(_sizer, '_held_option_underlyings', lambda: None)
    with caplog.at_level(logging.ERROR):
        orders = _run_sizer(monkeypatch, [
            _sig('S_eq_aapl', 'AAPL', direction=1),
            _sig('S_opt_spy', 'SPY', direction='BUY_VOL', option_spec=_STRADDLE_SPEC),
        ])
    assert [o for o in orders if o.get('instrument_class') == 'option'] == []
    assert any('suppressing all' in r.message.lower() for r in caplog.records)
    assert any(o['ticker'] == 'AAPL' for o in orders), 'equity must be unaffected'


# ===========================================================================
# C3 — expiry close: held + still-targeted + min DTE <= 7 → forced close
# ===========================================================================

def _opt_sigs():
    return [
        _sig('S_eq_aapl', 'AAPL', direction=1),
        _sig('S_opt_spy', 'SPY', direction='BUY_VOL', option_spec=_STRADDLE_SPEC),
    ]


def test_expiry_close_at_dte_7(monkeypatch):
    """min DTE == 7 (boundary, <=) → __close_option_expiry__ with the proven
    held_legs shape; the open stays suppressed (no same-cycle close+open)."""
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.delenv('OPENCLAW_OPTION_EXPIRY_CLOSE_DTE', raising=False)
    monkeypatch.setattr(_sizer, '_held_option_underlyings',
                        lambda: {'SPY': [_occ('SPY', 7), _occ('SPY', 44, right='P')]})
    orders = _run_sizer(monkeypatch, _opt_sigs())
    closes = [o for o in orders if o.get('strategy_id') == '__close_option_expiry__']
    assert len(closes) == 1, f'expiry close expected at DTE 7, got {orders}'
    c = closes[0]
    assert c['ticker'] == 'SPY'
    assert c['instrument_class'] == 'option'
    assert c['option_spec'] == {'underlying': 'SPY', 'structure': 'held_legs'}
    assert c['action'] == 'CLOSE'
    assert c['target_usd'] == 0.0 and c['current_usd'] == 0.0
    assert c['contributing_strategies'] == ['__close_option_expiry__']
    opens = [o for o in orders if o.get('instrument_class') == 'option'
             and o.get('action') != 'CLOSE']
    assert opens == [], 'open must stay suppressed on the expiry-close day'


def test_no_expiry_close_at_dte_8(monkeypatch):
    """min DTE == 8 → hold: no close, open suppressed."""
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.delenv('OPENCLAW_OPTION_EXPIRY_CLOSE_DTE', raising=False)
    monkeypatch.setattr(_sizer, '_held_option_underlyings',
                        lambda: {'SPY': [_occ('SPY', 8)]})
    orders = _run_sizer(monkeypatch, _opt_sigs())
    assert [o for o in orders if o.get('instrument_class') == 'option'] == [], \
        'DTE 8: neither close nor open expected'


def test_expiry_close_eod_production_path(monkeypatch):
    """The expiry close fires through the PRODUCTION path (OPENCLAW_EOD_RECONCILE=1)."""
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.delenv('OPENCLAW_OPTION_EXPIRY_CLOSE_DTE', raising=False)
    monkeypatch.setattr(_sizer, '_held_option_underlyings',
                        lambda: {'SPY': [_occ('SPY', 3)]})
    orders = _run_sizer_eod(monkeypatch, _opt_sigs())
    closes = [o for o in orders if o.get('strategy_id') == '__close_option_expiry__']
    assert len(closes) == 1 and closes[0]['ticker'] == 'SPY'


def test_not_in_targets_near_expiry_is_orphan_only(monkeypatch):
    """Held + NOT in targets at DTE<=7 → exactly ONE close, the ORPHAN close (the
    truth-table rows are disjoint — never a double close)."""
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(_sizer, '_held_option_underlyings',
                        lambda: {'SPY': [_occ('SPY', 3)]})
    # Equity-only signals — SPY has no live option target.
    orders = _run_sizer(monkeypatch, [_sig('S_eq_aapl', 'AAPL', direction=1)])
    closes = [o for o in orders if o.get('instrument_class') == 'option'
              and o.get('action') == 'CLOSE']
    assert len(closes) == 1
    assert closes[0]['strategy_id'] == '__close_option_orphan__'


def test_unparseable_expiry_closes(monkeypatch):
    """A held leg with an unreadable expiry (DTE→0) is unmanageable → expiry close."""
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(_sizer, '_held_option_underlyings',
                        lambda: {'SPY': ['SPY999999C00500000']})
    orders = _run_sizer(monkeypatch, _opt_sigs())
    closes = [o for o in orders if o.get('strategy_id') == '__close_option_expiry__']
    assert len(closes) == 1


# ===========================================================================
# C3 — executor dispatch: __close_option_expiry__ rides _route_mleg_close
# (dispatch keys on close_only + structure != 'single', NOT strategy_id)
# ===========================================================================

def test_expiry_close_dispatches_to_mleg_close(monkeypatch):
    import execution.alpaca_executor as _ex
    from strategies.base import OptionSpec
    spec = OptionSpec.from_dict({'underlying': 'SPY', 'structure': 'held_legs'})
    order = {'ticker': 'SPY', 'instrument_class': 'option', 'direction': 'long',
             'strategy_id': '__close_option_expiry__', 'option_spec': spec,
             'close_only': True}
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(_ex, '_options_session_gate', lambda: (True, ''))
    called = {}
    monkeypatch.setattr(_ex, '_route_mleg_close',
                        lambda o, s, c: called.update(spec=s) or {'status': 'submitted'})
    res = _ex._route_option_order(order, equity=100_000.0, coid='exp1')
    assert called.get('spec') is spec, \
        '__close_option_expiry__ must dispatch to _route_mleg_close'
    assert res['status'] == 'submitted'


def test_already_expired_leg_closes(monkeypatch):
    """A parseable but already-expired leg (negative DTE) must also force-close."""
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(_sizer, '_held_option_underlyings',
                        lambda: {'SPY': [_occ('SPY', -2)]})
    orders = _run_sizer(monkeypatch, _opt_sigs())
    closes = [o for o in orders if o.get('strategy_id') == '__close_option_expiry__']
    assert len(closes) == 1


def test_expiry_close_threads_as_close_only(monkeypatch):
    """The emitted expiry close (target_usd==0) must become close_only=True in the
    sized payload — the discriminator the executor's close dispatch keys on."""
    import execution.regime_blended_sizer_live as _live
    from strategies.base import OptionSpec
    order = {
        'ticker': 'SPY', 'strategy_id': '__close_option_expiry__', 'direction': 'long',
        'notional_usd': 0.0, 'pct_nav': 0.0, 'shares': 0, 'entry': None, 'stop': None,
        't1': None, 't2': None, 'kelly_final': 0.0, 'ev': 0.0, 'p_t1': 0.5,
        'source_mode': 'sharpe_cadence', 'target_usd': 0.0, 'current_usd': 0.0,
        'contributing_strategies': ['__close_option_expiry__'], 'flip_action': None,
        'action': 'CLOSE', 'instrument_class': 'option',
        'option_spec': {'underlying': 'SPY', 'structure': 'held_legs'},
    }
    payload = _live._build_sized_payload([order], {'cycle_date': '2026-06-03', 'regime': {}})
    assert len(payload['orders']) == 1
    o = payload['orders'][0]
    assert o['close_only'] is True
    assert o['instrument_class'] == 'option'
    assert isinstance(o['option_spec'], OptionSpec)
    assert o['option_spec'].structure == 'held_legs'
