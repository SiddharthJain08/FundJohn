"""2026-11-25: the crypto REDUCE / ADD paths must leave ONE protective stop
resting on the whole held position.

Incidents: 11-24 15:59 ET BTC-USD top-up refused 'potential wash trade
detected' (resting sell stop); 11-25 15:55 ET a $44 REDUCE cancelled the BTC
stop and never re-rested it (0.1476 BTC unprotected until the operator pass).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from execution import alpaca_executor as ax  # noqa: E402

RESTING = {'id': 'stop-old', 'symbol': 'BTC/USD', 'type': 'stop_limit',
           'stop_price': '35760', 'limit_price': '35581.2', 'qty': '0.148292'}


def _wire(monkeypatch, *, resting, position_qty, close_payload=None,
          entry_ok=True, poll_filled='0.000726631'):
    calls = []

    def fake_cli(args, timeout=30):
        calls.append(list(args))
        if args[:2] == ['order', 'list']:
            return True, list(resting), None
        if args[:2] == ['order', 'cancel']:
            return True, {}, None
        if args[:2] == ['position', 'list']:
            if position_qty is None:
                return False, None, {'error': 'boom'}
            return True, [{'symbol': 'BTCUSD', 'qty': str(position_qty), 'side': 'long'}], None
        if args[:2] == ['position', 'close']:
            return True, close_payload or {'id': 'reduce-1', 'qty': '0.000726631', 'notional': '43.68'}, None
        if args[:2] == ['order', 'get']:
            return True, {'status': 'filled', 'filled_qty': poll_filled}, None
        if 'latest-trades' in args:
            return True, {'trades': {'BTC/USD': {'p': 60000.0}}}, None
        if args[:2] == ['order', 'submit'] and 'stop_limit' in args:
            return True, {'id': f'stop-new-{len(calls)}', 'status': 'new'}, None
        if args[:2] == ['order', 'submit']:
            if entry_ok:
                return True, {'id': 'entry-1', 'status': 'pending_new', 'qty': '0.0005'}, None
            return False, None, {'exit_code': 1, 'status': 403,
                                 'error': 'potential wash trade detected. use complex orders'}
        return True, {}, None

    monkeypatch.setattr(ax, '_run_alpaca_cli', fake_cli)
    monkeypatch.setattr(ax, '_poll_to_terminal', lambda *a, **k: {'status': 'canceled'})
    monkeypatch.setattr(ax, '_crypto_nmbp', lambda: 1_000_000.0)
    monkeypatch.setenv('OPENCLAW_INSTRUMENT_CLASS_ROUTING', '1')
    return calls


def _stop_submits(calls):
    return [c for c in calls if c[:2] == ['order', 'submit'] and 'stop_limit' in c]


def _arg(c, flag):
    return c[c.index(flag) + 1]


def test_partial_reduce_rerests_stop_on_remaining_qty(monkeypatch):
    calls = _wire(monkeypatch, resting=[RESTING], position_qty='0.147565548')
    order = {'close_only': True, 'ticker': 'BTC-USD', 'notional_usd': 44.0, 'current_usd': 8879.0}
    res = ax._route_crypto_close(order, 'BTC/USD', 'BTC-USD', 'c-reduce')
    assert res['status'] == 'submitted'
    kinds = [c[:2] for c in calls]
    assert kinds.index(['order', 'cancel']) < kinds.index(['position', 'close'])
    stops = _stop_submits(calls)
    assert len(stops) == 1, f'expected one re-rested stop, got {len(stops)}'
    st = stops[0]
    assert _arg(st, '--qty') == '0.147565548'          # the WHOLE remaining position
    assert _arg(st, '--stop-price') == '35760.00'      # the cancelled stop's price
    assert _arg(st, '--side') == 'sell'
    assert _arg(st, '--client-order-id') == 'c-reduce-rerest-stop'
    # the re-rest happens AFTER the close was submitted
    assert calls.index(st) > kinds.index(['position', 'close'])


def test_partial_reduce_falls_back_to_prior_qty_minus_fill(monkeypatch):
    calls = _wire(monkeypatch, resting=[RESTING], position_qty=None)   # position list fails
    order = {'close_only': True, 'ticker': 'BTC-USD', 'notional_usd': 44.0, 'current_usd': 8879.0}
    ax._route_crypto_close(order, 'BTC/USD', 'BTC-USD', 'c2')
    stops = _stop_submits(calls)
    assert len(stops) == 1
    assert abs(float(_arg(stops[0], '--qty')) - (0.148292 - 0.000726631)) < 1e-9


def test_full_close_does_not_rerest(monkeypatch):
    calls = _wire(monkeypatch, resting=[RESTING], position_qty=None,
                  close_payload={'id': 'close-1', 'qty': '0.148292', 'notional': '8879'})
    order = {'close_only': True, 'ticker': 'BTC-USD', 'notional_usd': 8879.0, 'current_usd': 8879.0}
    res = ax._route_crypto_close(order, 'BTC/USD', 'BTC-USD', 'c3')
    assert res['status'] == 'submitted'
    assert _stop_submits(calls) == []


def test_reduce_without_resting_stop_uses_sized_stop(monkeypatch):
    """No stop was resting (e.g. after the 11-24 orphan sweep) but the sized
    order carries one → the REDUCE still leaves the position protected."""
    calls = _wire(monkeypatch, resting=[], position_qty='0.147565548')
    order = {'close_only': True, 'ticker': 'BTC-USD', 'notional_usd': 44.0,
             'current_usd': 8879.0, 'stop': 35760.0}
    ax._route_crypto_close(order, 'BTC/USD', 'BTC-USD', 'c4')
    stops = _stop_submits(calls)
    assert len(stops) == 1 and _arg(stops[0], '--qty') == '0.147565548'


def test_add_cancels_resting_stop_then_rerests_on_total_position(monkeypatch):
    calls = _wire(monkeypatch, resting=[RESTING], position_qty='0.148792', poll_filled='0.0005')
    order = {'ticker': 'BTC-USD', 'direction': 'long', 'pct_nav': 0.0003, 'stop': 35800.0}
    res = ax._route_crypto_order(order, equity=100_000.0, coid='c-add')
    assert res is not None and res['status'] == 'submitted'
    kinds = [c[:2] for c in calls]
    entry_idx = next(i for i, c in enumerate(calls)
                     if c[:2] == ['order', 'submit'] and 'stop_limit' not in c)
    assert ['order', 'cancel'] in kinds and kinds.index(['order', 'cancel']) < entry_idx
    stops = _stop_submits(calls)
    assert len(stops) == 1
    assert _arg(stops[0], '--qty') == '0.148792'        # held + fill, not just the fill
    assert _arg(stops[0], '--stop-price') == '35800.00'  # the freshly sized stop wins


def test_add_rejected_restores_prior_stop(monkeypatch):
    calls = _wire(monkeypatch, resting=[RESTING], position_qty='0.148292', entry_ok=False)
    order = {'ticker': 'BTC-USD', 'direction': 'long', 'pct_nav': 0.0003, 'stop': 35800.0}
    res = ax._route_crypto_order(order, equity=100_000.0, coid='c-rej')
    assert res is not None and res['status'] == 'rejected'
    stops = _stop_submits(calls)
    assert len(stops) == 1
    assert _arg(stops[0], '--qty') == '0.148292'
    assert _arg(stops[0], '--stop-price') == '35760.00'   # the ORIGINAL stop, restored
    assert _arg(stops[0], '--client-order-id') == 'c-rej-restore-stop'


def test_fresh_entry_without_position_sizes_stop_to_fill(monkeypatch):
    """Phase D behaviour preserved: no resting stop, no held position → the
    stop is sized to the polled filled qty."""
    calls = _wire(monkeypatch, resting=[], position_qty=None, poll_filled='0.000196')
    order = {'ticker': 'BTC-USD', 'direction': 'long', 'pct_nav': 0.01, 'stop': 50000.0}
    res = ax._route_crypto_order(order, equity=100_000.0, coid='c-new')
    assert res['status'] == 'submitted'
    stops = _stop_submits(calls)
    assert len(stops) == 1 and _arg(stops[0], '--qty') == '0.000196'


def test_crypto_qty_str_never_scientific():
    assert ax._crypto_qty_str(0.000196) == '0.000196'
    assert ax._crypto_qty_str(1e-05) == '0.00001'
    assert ax._crypto_qty_str(0.147565548) == '0.147565548'
    assert ax._crypto_qty_str(2.0) == '2'
