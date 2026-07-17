"""tests/test_sizer_class_consolidation.py — SP-5 Phase 1a (G1+G2) prongs 1+2.

Drives the REAL _sharpe_cadence_path (no stub) by monkeypatching only the DB/broker
seams (strategy_weights.load_current, _load_active_window_signals, _load_broker_positions_usd)
— the same pattern as tests/test_sizer_sp6_eod_mode.py. This exercises the partition,
the new _consolidate_option_orders helper, the headroom guard, and the dead-block deletion.

Prong 1: option contributors get their OWN per-underlying net + emission; equity orders
         on the same underlying never carry option_spec/instrument_class and net only the
         equity weight. Option orders never net the broker map (current_usd==0.0).
Prong 2: _load_broker_positions_usd excludes OCC option legs (and _classify_position_deltas
         therefore never orphan-closes them).
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


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_STRADDLE_SPEC = {'underlying': 'SPY', 'structure': 'straddle', 'hedge': 'delta',
                  'strike_rule': 'atm', 'right': 'both'}
_SINGLE_SPEC = {'underlying': 'QQQ', 'structure': 'single', 'hedge': 'none',
                'strike_rule': 'atm', 'right': 'call'}


def _account(equity=100_000, bp=None):
    a = {'equity': equity, 'regt_buying_power': 4 * equity,
         'long_market_value': 0, 'cash': equity}
    if bp is not None:
        a['buying_power'] = bp
    return a


def _params():
    # min_cum_sharpe default 3.0 is from pipeline_config; we pass it explicitly via
    # _resolve_min_cumulative_sharpe's param keys. Keep notional floor at 0 so the
    # `new_scale` renorm branch never fires — `scale` (line 741) stays the true
    # per-unit-weight USD rate that the option helper must reuse for parity.
    return {'liquidity_param': 1.0, 'min_signal_notional_usd': 0,
            'min_cumulative_sharpe': 0.0}


def _sig(sid, ticker, direction=1, sharpe=5.0, weight=1.0, option_spec=None):
    """Active-window signal dict shape (matches _load_active_window_signals output)."""
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
    """Drive the real _sharpe_cadence_path. weight/sharpe come from each signal's
    _w/_s so load_current can be synthesized from the signal set."""
    broker = broker or {}
    account = account or _account()
    params = params or _params()
    params = {**params, 'min_cumulative_sharpe': min_cum_sharpe}

    # Synthesize a load_current row per distinct strategy_id from the signals.
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
    """Drive _sharpe_cadence_path through the PRODUCTION path (OPENCLAW_EOD_RECONCILE=1
    ⇒ _load_approved_carried_signals). This is the path SP-6 EOD trades through daily."""
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
                        lambda wbs: list(signals))
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: dict(broker))
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.delenv('OPENCLAW_OPTION_DELTA_HEDGE', raising=False)
    with _mock.patch('execution.strategy_weights.load_current', return_value=weight_rows):
        return _sizer._sharpe_cadence_path(
            signals=[], account_state=account, regime_state='LOW_VOL',
            params=params, confirmer=None)


# ===========================================================================
# Test 1 — mixed-class same-ticker: equity stays clean, option is separate
# ===========================================================================

def test_mixed_class_same_ticker_split(monkeypatch):
    """Equity SPY long + equity AAPL long + option SPY straddle ⇒ clean equity orders
    (no option keys; SPY's notional reflects ONLY the equity weight — NOT the option
    weight leaked in) PLUS a separate option SPY order (instrument_class='option', spec
    carried, option-only sid, notional == w_opt × equity-scale).

    TWO equity tickers make the normalization weight-SENSITIVE: clean ⇒ SPY==AAPL==
    λ·NAV/2; if the option weight (1.0) contaminated SPY's equity weight (→2.0), SPY
    would be ⅔λ·NAV and AAPL ⅓λ·NAV. The assertion below discriminates."""
    sigs = [
        _sig('S_eq_spy', 'SPY', direction=1, sharpe=5.0, weight=1.0),
        _sig('S_eq_aapl', 'AAPL', direction=1, sharpe=5.0, weight=1.0),
        _sig('S_opt_spy', 'SPY', direction='BUY_VOL', sharpe=5.0, weight=1.0,
             option_spec=_STRADDLE_SPEC),
    ]
    orders = _run_sizer(monkeypatch, sigs)
    eq_spy = [o for o in orders if o.get('instrument_class') != 'option' and o['ticker'] == 'SPY']
    eq_aapl = [o for o in orders if o.get('instrument_class') != 'option' and o['ticker'] == 'AAPL']
    op = [o for o in orders if o.get('instrument_class') == 'option' and o['ticker'] == 'SPY']
    assert len(eq_spy) == 1 and len(eq_aapl) == 1, f'two clean equity orders expected, got {orders}'
    assert len(op) == 1, f'one option SPY order expected, got {orders}'
    # Equity orders are byte-clean: no option keys.
    for o in eq_spy + eq_aapl:
        assert 'option_spec' not in o and 'instrument_class' not in o
    # Equity gross = |1.0|(SPY) + |1.0|(AAPL) = 2.0 ⇒ scale = lam*nav/2.0; each = lam*nav/2.
    lam = _sizer._load_lambda() * 1.0  # liquidity_param=1.0
    scale = lam * 100_000 / 2.0
    assert eq_spy[0]['notional_usd'] == pytest.approx(scale, rel=1e-9), \
        'SPY equity notional must reflect ONLY the equity weight (no option leak)'
    assert eq_aapl[0]['notional_usd'] == pytest.approx(scale, rel=1e-9)
    # Option order carries the spec + option-only sid; size == w_opt(1.0) × equity scale.
    assert op[0]['instrument_class'] == 'option'
    assert op[0]['option_spec'] == _STRADDLE_SPEC
    assert op[0]['strategy_id'] == 'S_opt_spy'
    assert op[0]['direction'] == 'long'  # BUY_VOL → +1
    assert op[0]['source_mode'] == 'sharpe_cadence'
    assert op[0]['notional_usd'] == pytest.approx(1.0 * scale, rel=1e-9), \
        'option notional must be w_opt × equity per-unit scale (USD parity)'
    # Option order never nets the broker map.
    assert op[0]['current_usd'] == 0.0


def test_mixed_class_split_in_eod_mode(monkeypatch):
    """PRODUCTION-PATH test: with OPENCLAW_EOD_RECONCILE=1 (the daily SP-6 path,
    _load_approved_carried_signals), the partition + option consolidation behaves
    identically — equity SPY is clean, a separate option SPY order is emitted. This
    proves the carried loader preserves signal_params.option_spec so the partition
    fires in the only path that runs in production (guards the G1 mis-trade)."""
    sigs = [
        _sig('S_eq_aapl', 'AAPL', direction=1, sharpe=5.0, weight=1.0),
        _sig('S_opt_spy', 'SPY', direction='BUY_VOL', sharpe=5.0, weight=1.0,
             option_spec=_STRADDLE_SPEC),
    ]
    orders = _run_sizer_eod(monkeypatch, sigs)
    op = [o for o in orders if o.get('instrument_class') == 'option']
    eq = [o for o in orders if o.get('instrument_class') != 'option']
    assert len(op) == 1, f'option order must emerge in EOD mode, got {orders}'
    assert op[0]['ticker'] == 'SPY'
    assert op[0]['option_spec'] == _STRADDLE_SPEC
    assert op[0]['strategy_id'] == 'S_opt_spy'
    assert op[0]['current_usd'] == 0.0
    # Equity AAPL order is clean (no option keys); no equity SPY order (SPY is option-only).
    for o in eq:
        assert 'option_spec' not in o and 'instrument_class' not in o
    assert not any(o['ticker'] == 'SPY' for o in eq), \
        'option SPY must NOT route as equity in EOD mode (the G1 mis-trade guard)'


# ===========================================================================
# Test 2 — option-only underlying: no broker netting / orphan interaction
# ===========================================================================

def test_option_only_no_broker_netting(monkeypatch):
    """Option SPY (sole signal on SPY) + an equity AAPL to establish scale. A broker
    SPY EQUITY position must NOT net against the option order (current_usd==0.0) and
    must NOT trigger any flip on the option order."""
    sigs = [
        _sig('S_eq_aapl', 'AAPL', direction=1, sharpe=5.0, weight=1.0),
        _sig('S_opt_spy', 'SPY', direction='BUY_VOL', sharpe=5.0, weight=1.0,
             option_spec=_STRADDLE_SPEC),
    ]
    # Broker holds a SHORT equity SPY — would flip/net the option if mis-classed.
    broker = {'SPY': -25_000.0}
    orders = _run_sizer(monkeypatch, sigs, broker=broker)
    op = [o for o in orders if o.get('instrument_class') == 'option']
    assert len(op) == 1
    assert op[0]['ticker'] == 'SPY'
    assert op[0]['strategy_id'] == 'S_opt_spy'
    assert op[0]['direction'] == 'long'
    assert op[0]['current_usd'] == 0.0
    assert op[0].get('flip_action') is None
    # The broker SPY equity short is orphaned by the EQUITY path (correct — no equity
    # SPY target), NOT swallowed by the option order.
    eq_spy = [o for o in orders if o.get('instrument_class') != 'option' and o['ticker'] == 'SPY']
    assert len(eq_spy) == 1
    assert eq_spy[0]['action'].startswith('close')


def test_no_equity_signals_fail_closed(monkeypatch):
    """No equity signals at all ⇒ no equity scale ⇒ emit ZERO option orders
    (fail-closed; documented). The option signal is the only signal."""
    sigs = [
        _sig('S_opt_spy', 'SPY', direction='BUY_VOL', sharpe=5.0, weight=1.0,
             option_spec=_STRADDLE_SPEC),
    ]
    orders = _run_sizer(monkeypatch, sigs)
    op = [o for o in orders if o.get('instrument_class') == 'option']
    assert op == [], f'option orders must be empty when no equity scale exists, got {orders}'


# ===========================================================================
# Test 3 — min_cum_sharpe gate applies to the option group
# ===========================================================================

def test_option_group_emitted_no_legacy_floor(monkeypatch):
    """The legacy min_cumulative_sharpe conviction gate on the option path was
    retired 2026-07-01 (options are inert; the floor was never option-appropriate).
    An option group now passes through regardless of a naive raw-Sharpe sum, and a
    benign equity signal establishes scale. TODO(option-gate): reinstate an
    option-appropriate conviction gate when the first option strategy promotes."""
    sigs = [
        _sig('S_eq_aapl', 'AAPL', direction=1, sharpe=5.0, weight=1.0),
        _sig('S_opt_spy', 'SPY', direction='BUY_VOL', sharpe=1.0, weight=1.0,
             option_spec=_STRADDLE_SPEC),
    ]
    orders = _run_sizer(monkeypatch, sigs)
    assert any(o.get('instrument_class') == 'option' for o in orders), \
        'option group must pass through now that the legacy floor is gone'
    assert any(o['ticker'] == 'AAPL' for o in orders)


# ===========================================================================
# Test 4 — headroom guard
# ===========================================================================

def test_headroom_scales_options_down(monkeypatch):
    """Option gross > headroom > 0 ⇒ option notionals scaled down proportionally;
    equity orders identical to the no-constraint case."""
    sigs = [
        _sig('S_eq_aapl', 'AAPL', direction=1, sharpe=5.0, weight=1.0),
        _sig('S_opt_spy', 'SPY', direction='BUY_VOL', sharpe=5.0, weight=1.0,
             option_spec=_STRADDLE_SPEC),
    ]
    # Equity gross = lam*nav = 2.0*100k = 200k. Set bp so headroom is small.
    # headroom = bp - equity_gross. Pick bp=210k → headroom=10k < option gross(=200k).
    constrained = _run_sizer(monkeypatch, sigs, account=_account(bp=210_000))
    op_c = [o for o in constrained if o.get('instrument_class') == 'option'][0]
    assert op_c['notional_usd'] == pytest.approx(10_000.0, rel=1e-6), \
        f'option scaled to headroom; got {op_c["notional_usd"]}'
    # Equity order identical to a high-bp (unconstrained) run.
    unconstrained = _run_sizer(monkeypatch, sigs, account=_account(bp=10_000_000))
    eq_c = [o for o in constrained if o.get('instrument_class') != 'option' and o['ticker'] == 'AAPL'][0]
    eq_u = [o for o in unconstrained if o.get('instrument_class') != 'option' and o['ticker'] == 'AAPL'][0]
    assert eq_c['notional_usd'] == pytest.approx(eq_u['notional_usd'], rel=1e-9)


def test_headroom_zero_emits_no_options(monkeypatch):
    """headroom <= 0 ⇒ no option orders; equity orders unchanged."""
    sigs = [
        _sig('S_eq_aapl', 'AAPL', direction=1, sharpe=5.0, weight=1.0),
        _sig('S_opt_spy', 'SPY', direction='BUY_VOL', sharpe=5.0, weight=1.0,
             option_spec=_STRADDLE_SPEC),
    ]
    # Equity gross = 200k; bp=150k → headroom = -50k <= 0.
    orders = _run_sizer(monkeypatch, sigs, account=_account(bp=150_000))
    op = [o for o in orders if o.get('instrument_class') == 'option']
    assert op == [], f'no option orders when headroom<=0, got {orders}'
    assert any(o['ticker'] == 'AAPL' for o in orders)


# ===========================================================================
# Test 5 — conflicting specs: first wins + warning
# ===========================================================================

def test_conflicting_specs_first_wins_and_warns(monkeypatch, caplog):
    """Two option strategies on the same underlying with DIFFERENT specs ⇒ first
    contributor's spec wins; a loud warning is logged."""
    spec_a = dict(_STRADDLE_SPEC)
    spec_b = {'underlying': 'SPY', 'structure': 'strangle', 'hedge': 'delta',
              'strike_rule': 'delta_30', 'right': 'both'}
    sigs = [
        _sig('S_eq_aapl', 'AAPL', direction=1, sharpe=5.0, weight=1.0),
        _sig('S_opt_a', 'SPY', direction='BUY_VOL', sharpe=3.0, weight=1.0, option_spec=spec_a),
        _sig('S_opt_b', 'SPY', direction='BUY_VOL', sharpe=3.0, weight=1.0, option_spec=spec_b),
    ]
    with caplog.at_level(logging.WARNING):
        orders = _run_sizer(monkeypatch, sigs)
    op = [o for o in orders if o.get('instrument_class') == 'option']
    assert len(op) == 1
    assert op[0]['option_spec'] == spec_a, 'first contributor spec must win'
    # Composite option-only sid (sorted).
    assert op[0]['strategy_id'] == 'S_opt_a|S_opt_b'
    assert any('option_spec' in r.message.lower() or 'conflict' in r.message.lower()
               for r in caplog.records if r.levelno >= logging.WARNING), \
        'a conflicting-spec warning must be logged'


# ===========================================================================
# Prong 2 — OCC filter in _load_broker_positions_usd
# ===========================================================================

_FAKE_POSITIONS = [
    {'symbol': 'AAPL', 'qty': '100', 'market_value': '15000'},
    {'symbol': 'SPY', 'qty': '-50', 'market_value': '-25000'},
    {'symbol': 'BTC/USD', 'qty': '0.5', 'market_value': '30000'},
    {'symbol': 'SPY260626C00755000', 'qty': '2', 'market_value': '1400'},   # OCC call
    {'symbol': 'QQQ260618P00400000', 'qty': '-1', 'market_value': '-800'},  # OCC put
]


def _patch_alpaca(monkeypatch, positions):
    import json as _json

    class _Proc:
        returncode = 0
        stdout = _json.dumps(positions).encode()
        stderr = b''

    # subprocess is imported INSIDE _load_broker_positions_usd; patch the module object.
    import subprocess
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: _Proc())


def test_occ_filter_excludes_option_legs(monkeypatch, caplog):
    """_load_broker_positions_usd keeps equities + crypto, excludes OCC option legs."""
    _patch_alpaca(monkeypatch, _FAKE_POSITIONS)
    with caplog.at_level(logging.INFO):
        out = _sizer._load_broker_positions_usd()
    assert set(out.keys()) == {'AAPL', 'SPY', 'BTC/USD'}, \
        f'OCC legs must be excluded; got {out.keys()}'
    assert 'SPY260626C00755000' not in out
    assert 'QQQ260618P00400000' not in out
    assert any('option leg' in r.message.lower() or 'occ' in r.message.lower()
               for r in caplog.records), 'an excluded-legs info log is expected'


def test_is_occ_symbol():
    assert _sizer._is_occ_symbol('SPY260626C00755000')
    assert _sizer._is_occ_symbol('QQQ260618P00400000')
    assert _sizer._is_occ_symbol('A260618C00050000')      # 1-char root
    assert not _sizer._is_occ_symbol('AAPL')
    assert not _sizer._is_occ_symbol('BTC/USD')
    assert not _sizer._is_occ_symbol('SPY')
    assert not _sizer._is_occ_symbol('BRK.B')


# ===========================================================================
# Test 7 — _classify_position_deltas never orphan-closes an OCC leg
# ===========================================================================

def test_classify_never_orphans_occ_leg(monkeypatch):
    """A broker map built via the real (filtered) loader contains NO OCC keys, so
    _classify_position_deltas can never emit an orphan_close for one."""
    _patch_alpaca(monkeypatch, _FAKE_POSITIONS)
    broker = _sizer._load_broker_positions_usd()
    ticker_meta = {}
    # No targets at all → every held position would orphan-close; assert none are OCC.
    emissions = _sizer._classify_position_deltas({}, broker, ticker_meta)
    orphaned = [tkr for (tkr, _usd, kind) in emissions if kind == 'orphan_close']
    assert 'SPY260626C00755000' not in orphaned
    assert 'QQQ260618P00400000' not in orphaned
    assert set(orphaned) == {'AAPL', 'SPY', 'BTC/USD'}


# ===========================================================================
# Test 9 — BYTE-IDENTICAL REGRESSION: equity-only run is unperturbed
# ===========================================================================

def test_equity_only_byte_identical(monkeypatch):
    """With ZERO option signals, _sharpe_cadence_path output carries NO option keys
    and exact, partition-agnostic notionals. The partition is a provable no-op:
    opt_active is empty, the helper returns [], and the equity loop is unchanged.

    Representative equity-only set: two strategies agree LONG on AAPL and one is
    LONG MSFT (each daily_weight 1.0). Broker flat. No min-notional floor. Under
    the corr-adjusted gate the two correlated AAPL LONGs deflate below the naive
    sum of 2.0, while single-strategy MSFT stays at 1.0."""
    sigs = [
        _sig('S_eq_a', 'AAPL', direction=1, sharpe=4.0, weight=1.0),
        _sig('S_eq_b', 'AAPL', direction=1, sharpe=4.0, weight=1.0),
        _sig('S_eq_c', 'MSFT', direction=1, sharpe=4.0, weight=1.0),
    ]
    orders = _run_sizer(monkeypatch, sigs)
    # No option keys anywhere.
    for o in orders:
        assert 'option_spec' not in o, f'equity-only run leaked option_spec: {o}'
        assert 'instrument_class' not in o, f'equity-only run leaked instrument_class: {o}'
    by_tkr = {o['ticker']: o for o in orders}
    assert set(by_tkr) == {'AAPL', 'MSFT'}
    # Derive expected corr weights from the SAME function the sizer uses (empty sim
    # == the sparse-default these test-only strategies resolve to), so the test
    # tracks the formula rather than a magic number.
    meta = {'AAPL': {'strategies': ['S_eq_a', 'S_eq_b'], 'directions': [1, 1], 'brackets': []},
            'MSFT': {'strategies': ['S_eq_c'], 'directions': [1], 'brackets': []}}
    wbs = {'S_eq_a': 1.0, 'S_eq_b': 1.0, 'S_eq_c': 1.0}
    _, exp_size, _, _ = _sizer._corr_adjusted_maps(meta, wbs, wbs, {})
    assert exp_size['AAPL'] < 2.0                      # corr-deflated below the naive sum
    assert exp_size['MSFT'] == pytest.approx(1.0)      # single strategy: S_adj == weight
    gross = abs(exp_size['AAPL']) + abs(exp_size['MSFT'])
    lam = _sizer._load_lambda() * 1.0
    scale = lam * 100_000 / gross
    assert by_tkr['AAPL']['notional_usd'] == pytest.approx(exp_size['AAPL'] * scale, rel=1e-9)
    assert by_tkr['MSFT']['notional_usd'] == pytest.approx(exp_size['MSFT'] * scale, rel=1e-9)
    # Σ notionals == λ×NAV (the normalization invariant).
    assert sum(o['notional_usd'] for o in orders) == pytest.approx(lam * 100_000, rel=1e-9)
    assert all(o['source_mode'] == 'sharpe_cadence' for o in orders)


# ===========================================================================
# Prong 3 — open_reconcile._load_approved_set skips option rows (unit)
# ===========================================================================

import os as _os  # noqa: E402
import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from execution import open_reconcile as _recon  # noqa: E402


class _MockCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return list(self._rows)


def _arow(ticker, direction, signal_params=None):
    return {'ticker': ticker, 'direction': direction, 'signal_params': signal_params}


def test_load_approved_set_skips_option_rows_gate_off(monkeypatch):
    """Gate OFF: an APPROVED option row (signal_params carries option_spec) is excluded
    from the equity target map; equity rows remain."""
    monkeypatch.delenv('OPENCLAW_OPTION_DELTA_HEDGE', raising=False)
    rows = [
        _arow('AAPL', 'LONG'),
        _arow('SPY', 'LONG'),  # equity SPY
        _arow('SPY', 'LONG', signal_params={'option_spec': _STRADDLE_SPEC}),  # option SPY → skip
    ]
    out = _recon._load_approved_set(_MockCursor(rows), date(2026, 6, 3))
    # SPY is present from the EQUITY row, AAPL present. The option row added nothing.
    assert out == {'AAPL': 1.0, 'SPY': 1.0}


def test_load_approved_set_option_only_ticker_not_shielded_gate_off(monkeypatch):
    """An underlying present ONLY via an APPROVED option row is NOT in the equity
    target map ⇒ a dropped equity position of that underlying is NOT shielded."""
    monkeypatch.delenv('OPENCLAW_OPTION_DELTA_HEDGE', raising=False)
    rows = [
        _arow('AAPL', 'LONG'),
        _arow('SPY', 'LONG', signal_params={'option_spec': _STRADDLE_SPEC}),
    ]
    out = _recon._load_approved_set(_MockCursor(rows), date(2026, 6, 3))
    assert 'SPY' not in out, 'option-only underlying must not appear in equity target map'
    assert out == {'AAPL': 1.0}


def test_load_approved_set_keeps_hedge_rows(monkeypatch):
    """Hedge rows carry is_hedge but NO option_spec ⇒ they MUST stay in the target
    map (they are the equity offset leg)."""
    monkeypatch.setenv('OPENCLAW_OPTION_DELTA_HEDGE', '1')
    rows = [
        _arow('SPY', 'SHORT', signal_params={'is_hedge': True, 'hedge_shares': 10}),
    ]
    out = _recon._load_approved_set(_MockCursor(rows), date(2026, 6, 3))
    assert out == {'SPY': -1.0}, 'hedge row (no option_spec) must remain in target map'


# ===========================================================================
# Prong 3 — DB-backed validation of the real SELECT (the mock ignores SQL)
# ===========================================================================

_DSN = _os.environ.get('POSTGRES_URI')


@pytest.mark.integration
@pytest.mark.skipif(not _DSN, reason='POSTGRES_URI not set in env')
def test_load_approved_set_skips_option_rows_db(monkeypatch):
    """DB-backed: validates the real `SELECT ... signal_params` change. INSERTs an
    APPROVED option row + an APPROVED equity row on the SAME underlying for a unique
    test target_date, asserts the option row is excluded (equity row remains).
    Auto-rollback — nothing persists."""
    monkeypatch.delenv('OPENCLAW_OPTION_DELTA_HEDGE', raising=False)
    conn = psycopg2.connect(_DSN, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM workspaces WHERE name='default' LIMIT 1")
        ws = cur.fetchone()['id']
        cur.execute("SELECT id FROM strategy_registry WHERE status='approved' LIMIT 1")
        sid = cur.fetchone()['id']
        tdate = date(2021, 3, 18)  # unique test day, never a real production target_date
        import json as _json

        def _ins(ticker, direction, sigdate, sp):
            cur.execute(
                """INSERT INTO execution_signals
                     (strategy_id, workspace_id, signal_date, ticker, direction,
                      entry_price, target_date, lifecycle_state, position_size_pct,
                      regime_state, signal_params, status, computed_at)
                   VALUES (%s,%s,%s,%s,%s, %s,%s,'APPROVED',%s, %s,%s::jsonb,'open',%s)""",
                (sid, ws, sigdate, ticker, direction, 100.0, tdate, 0.05, 'NORMAL',
                 _json.dumps(sp), datetime.now(timezone.utc)))

        # Equity SPY (signal_date earlier) + option SPY (later). Distinct signal_dates
        # avoid the UNIQUE(strategy_id, signal_date, ticker, direction) collision.
        # Plus an OPTION-ONLY ticker (QQQ) that must NOT appear in the equity map.
        _ins('SPY', 'LONG', date(2021, 3, 16), {})
        _ins('SPY', 'LONG', date(2021, 3, 17), {'option_spec': _STRADDLE_SPEC})
        _ins('QQQ', 'LONG', date(2021, 3, 16), {'option_spec': _SINGLE_SPEC})

        out = _recon._load_approved_set(cur, tdate)
        # SPY present from the equity row; the option rows added nothing (skipped).
        assert out.get('SPY') == 1.0
        assert 'QQQ' not in out, 'option-only underlying must be excluded by the real SELECT'
        cur.close()
    finally:
        conn.rollback()
        conn.close()
