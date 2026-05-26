"""tests/test_phase_d_crypto.py — SP-3.1 Phase D crypto reference strategy + constants."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))


def test_crypto_cost_bps_present():
    from backtest.unified_backtest import INSTRUMENT_COST_BPS, resolve_cost_model_bps
    assert INSTRUMENT_COST_BPS.get('crypto') == 25.0
    assert resolve_cost_model_bps('crypto') == 25.0


def test_crypto_promotion_threshold_present():
    from strategies.lifecycle import PROMOTION_THRESHOLDS, _promotion_threshold
    thr = PROMOTION_THRESHOLDS.get('crypto')
    assert thr is not None
    assert thr['min_sharpe'] == 0.5
    assert thr['max_drawdown'] == 0.70
    assert _promotion_threshold('crypto') == thr


def test_crypto_in_routed_classes():
    from strategies.lifecycle import ROUTED_INSTRUMENT_CLASSES
    assert 'crypto' in ROUTED_INSTRUMENT_CLASSES


def test_can_transition_uses_crypto_threshold():
    # A crypto candidate with 35% drawdown must be ALLOWED (crypto cap 70%),
    # whereas the equity cap (20%) would block it.
    from strategies.lifecycle import LifecycleStateMachine, StrategyState, StrategyRecord
    from datetime import datetime, timezone
    lsm = LifecycleStateMachine.__new__(LifecycleStateMachine)
    lsm._records = {}
    now = datetime.now(timezone.utc).isoformat()
    lsm._records['S_x'] = StrategyRecord(
        strategy_id='S_x', state=StrategyState.CANDIDATE, state_since=now,
        history=[], metadata={}, instrument_class='crypto')
    ok, msg = lsm.can_transition('S_x', StrategyState.LIVE,
                                 {'sharpe': 0.8, 'max_drawdown': 0.35})
    assert ok, msg


import os
import pytest


@pytest.mark.skipif(not os.environ.get('POSTGRES_URI'), reason='no POSTGRES_URI')
def test_fractional_qty_survives_round_trip():
    import psycopg2
    conn = psycopg2.connect(os.environ['POSTGRES_URI'])
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO alpaca_submissions
              (run_date, ticker, strategy_id, direction, qty,
               entry_price, stop_price, target_price, pct_nav, notional_usd,
               time_in_force, order_class, client_order_id, submitted_at)
            VALUES (DATE '2099-01-01', 'BTC-USD', '__pytest__', 'long', 0.00018,
                    76000, 70000, 80000, 0.01, 15.0, 'gtc', 'simple', '__pytest_coid__', NOW())
            RETURNING qty
        """)
        stored = float(cur.fetchone()[0])
        assert stored == pytest.approx(0.00018), f'qty truncated to {stored}'
    finally:
        conn.rollback()   # never commit — leaves the canonical table untouched
        conn.close()


import numpy as np
import pandas as pd


def _load_btc_strategy():
    import importlib.util
    path = ROOT / 'src' / 'strategies' / 'implementations' / 'S_btc_momentum.py'
    spec = importlib.util.spec_from_file_location('S_btc_momentum', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.BtcMomentum


def _btc_panel(values):
    idx = pd.date_range('2025-01-01', periods=len(values), freq='D')
    return pd.DataFrame({'BTC-USD': values}, index=idx)


def test_btc_momentum_emits_long_on_uptrend():
    Strat = _load_btc_strategy()
    s = Strat()
    prices = _btc_panel(list(np.linspace(30000, 60000, 120)))  # steady uptrend
    sigs = s.generate_signals(prices, {'state': 'LOW_VOL'}, ['BTC-USD'])
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.ticker == 'BTC-USD' and sig.direction == 'LONG'
    assert sig.position_size_pct > 0
    assert sig.stop_loss < sig.entry_price < sig.target_1


def test_btc_momentum_goes_cash_on_downtrend():
    Strat = _load_btc_strategy()
    s = Strat()
    prices = _btc_panel(list(np.linspace(60000, 30000, 120)))  # steady downtrend
    sigs = s.generate_signals(prices, {'state': 'LOW_VOL'}, ['BTC-USD'])
    assert sigs == []   # absolute-momentum gate → cash


def test_btc_momentum_empty_on_short_history():
    Strat = _load_btc_strategy()
    s = Strat()
    prices = _btc_panel(list(np.linspace(30000, 40000, 30)))  # < 90 lookback
    assert s.generate_signals(prices, {'state': 'LOW_VOL'}, ['BTC-USD']) == []


def test_btc_momentum_instrument_class_field():
    Strat = _load_btc_strategy()
    assert getattr(Strat, 'instrument_class', None) == 'crypto'
    assert Strat.id == 'S_btc_momentum'


def test_submit_crypto_stop_constructs_args(monkeypatch):
    sys.path.insert(0, str(ROOT / 'src'))
    from execution import alpaca_executor as ax
    captured = {}
    def fake_cli(args, timeout=30):
        captured['args'] = args
        return True, {'id': 'stop-123', 'status': 'new'}, None
    monkeypatch.setattr(ax, '_run_alpaca_cli', fake_cli)
    res = ax._submit_crypto_stop('BTC/USD', stop_price=70000.0, qty=0.0002, coid='c1')
    assert res is not None and res.get('order_id') == 'stop-123'
    a = captured['args']
    assert a[:2] == ['order', 'submit']
    assert '--symbol' in a and 'BTC/USD' in a
    assert '--side' in a and 'sell' in a
    assert '--type' in a and 'stop_limit' in a
    assert '--stop-price' in a
    assert '--limit-price' in a
    assert '--time-in-force' in a and 'gtc' in a
    # limit must be BELOW stop for a protective sell stop
    li = a.index('--limit-price'); si = a.index('--stop-price')
    assert float(a[li+1]) < float(a[si+1])


def test_submit_crypto_stop_failsafe(monkeypatch):
    sys.path.insert(0, str(ROOT / 'src'))
    from execution import alpaca_executor as ax
    monkeypatch.setattr(ax, '_run_alpaca_cli',
                        lambda args, timeout=30: (False, None, {'status': 422, 'error': 'stop rejected'}))
    assert ax._submit_crypto_stop('BTC/USD', 70000.0, 0.0002, 'c1') is None


def test_poll_crypto_fill_returns_filled_qty(monkeypatch):
    sys.path.insert(0, str(ROOT / 'src'))
    from execution import alpaca_executor as ax
    seq = [
        (True, {'status': 'pending_new', 'filled_qty': '0'}, None),
        (True, {'status': 'filled', 'filled_qty': '0.000199499'}, None),
    ]
    calls = {'i': 0}
    def fake_cli(args, timeout=30):
        r = seq[min(calls['i'], len(seq)-1)]; calls['i'] += 1
        return r
    monkeypatch.setattr(ax, '_run_alpaca_cli', fake_cli)
    fq = ax._poll_crypto_fill('entry-1', timeout=2.0, poll_interval=0.01)
    assert abs(fq - 0.000199499) < 1e-9


def test_poll_crypto_fill_zero_on_reject(monkeypatch):
    sys.path.insert(0, str(ROOT / 'src'))
    from execution import alpaca_executor as ax
    monkeypatch.setattr(ax, '_run_alpaca_cli',
                        lambda args, timeout=30: (True, {'status': 'rejected', 'filled_qty': '0'}, None))
    assert ax._poll_crypto_fill('x', timeout=2.0, poll_interval=0.01) == 0.0


def test_crypto_close_cancels_open_stop_first(monkeypatch):
    sys.path.insert(0, str(ROOT / 'src'))
    from execution import alpaca_executor as ax
    events = []
    def fake_cli(args, timeout=30):
        events.append(list(args))
        if args[:2] == ['order', 'list']:
            return True, [{'id': 'stop-9', 'symbol': 'BTC/USD', 'type': 'stop_limit'}], None
        if args[:2] == ['order', 'cancel']:
            return True, {}, None
        if args[:2] == ['position', 'close']:
            return True, {'id': 'close-1', 'qty': '0.0002', 'notional': '15.0'}, None
        return True, {}, None
    monkeypatch.setattr(ax, '_run_alpaca_cli', fake_cli)
    order = {'close_only': True, 'ticker': 'BTC-USD', 'notional_usd': 15.0, 'current_usd': 15.0}
    res = ax._route_crypto_close(order, 'BTC/USD', 'BTC-USD', 'c1')
    assert res['status'] == 'submitted'
    # a cancel happened BEFORE the close
    kinds = [e[:2] for e in events]
    assert ['order', 'cancel'] in kinds, 'no cancel issued'
    assert kinds.index(['order', 'cancel']) < kinds.index(['position', 'close']), 'cancel must precede close'


def test_crypto_entry_emits_paired_stop(monkeypatch):
    sys.path.insert(0, str(ROOT / 'src'))
    from execution import alpaca_executor as ax
    calls = []
    def fake_cli(args, timeout=30):
        calls.append(list(args))
        if args[:2] == ['data', 'crypto'] or 'latest-trades' in args:
            return True, {'trades': {'BTC/USD': {'p': 76000.0}}}, None
        if args[:2] == ['order', 'get']:
            # poll returns a filled entry (slightly short of requested — fees)
            return True, {'status': 'filled', 'filled_qty': '0.000196'}, None
        if args[:2] == ['order', 'submit'] and '--type' in args and 'stop_limit' in args:
            return True, {'id': 'stop-1', 'status': 'new'}, None
        if args[:2] == ['order', 'submit']:
            return True, {'id': 'entry-1', 'status': 'pending_new', 'qty': '0.0002'}, None
        return True, {}, None
    monkeypatch.setattr(ax, '_run_alpaca_cli', fake_cli)
    monkeypatch.setenv('OPENCLAW_INSTRUMENT_CLASS_ROUTING', '1')
    order = {'ticker': 'BTC-USD', 'direction': 'long', 'pct_nav': 0.01, 'stop': 70000.0}
    res = ax._route_crypto_order(order, equity=100_000.0, coid='c1')
    assert res is not None and res['status'] == 'submitted'
    assert res['order_id'] == 'entry-1'
    # a stop_limit order was also submitted, sized to the polled filled qty:
    stop_submits = [c for c in calls if c[:2] == ['order', 'submit'] and 'stop_limit' in c]
    assert len(stop_submits) == 1, f'expected 1 paired stop, got {len(stop_submits)}'
    a = stop_submits[0]
    assert a[a.index('--qty') + 1] == '0.000196'   # sized to FILLED qty, not requested 0.0002
