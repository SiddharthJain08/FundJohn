import json
import unittest
from unittest.mock import MagicMock, patch

from execution import alpaca_executor as ae


def _mock_proc(returncode=0, stdout='', stderr=''):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _mock_session(bid=40.0, ask=40.0):
    sess = MagicMock()
    sess._base = 'https://paper-api.alpaca.markets'
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {'latestTrade': {'p': 40.0},
                           'latestQuote': {'bp': bid, 'ap': ask}}
    sess.get.return_value = r
    return sess


def test_qty_str_formats_without_trailing_zeros():
    assert ae._qty_str(1.0) == '1' and ae._qty_str(0.5) == '0.5'
    assert ae._qty_str(0.1234567) == '0.123457' and ae._qty_str(12.000001) == '12.000001'
    assert ae._qty_str(0.00001) == '0.00001'


def test_size_qty_long_fractional_exact():
    assert ae._size_qty('buy', 6.0, 40.0, fractionable=True) == (0.15, None)


def test_size_qty_dust_below_1usd_skips():
    assert ae._size_qty('buy', 0.8, 40.0, fractionable=True) == (0.0, 'dust_below_1usd')
    assert ae._size_qty('buy', 5.0, 0.0, fractionable=True) == (0.0, 'dust_below_1usd')


def test_size_qty_short_floors_and_never_rounds_up():
    assert ae._size_qty('sell', 36.0, 40.0, fractionable=True) == (0.0, 'short_sub_share')
    assert ae._size_qty('sell', 104.0, 40.0, fractionable=True) == (2.0, None)


def test_size_qty_non_fractionable_long_floors():
    assert ae._size_qty('buy', 24.0, 40.0, fractionable=False) == (0.0, 'not_fractionable_sub_share')
    assert ae._size_qty('buy', 104.0, 40.0, fractionable=False) == (2.0, None)


def test_is_fractionable_fail_open_when_db_unreachable():
    ae._FRACTIONABLE_CACHE.clear()
    with patch.dict('os.environ', {'POSTGRES_URI': ''}):
        assert ae._is_fractionable('AAPL') is True
    ae._FRACTIONABLE_CACHE.clear()


class TestExecuteSingleFractional(unittest.TestCase):
    def setUp(self):
        ae._asset_cache.clear()
        ae._unsupported_assets.clear()
        ae._market_hours_cache.update({'is_open': None, 'cached_at': 0.0})
        ae._session_kind_cache.update({'session': None, 'cached_at': 0.0})

    def _order(self, direction, pct_nav):
        if direction == 'long':
            stop, t1 = 38.0, 44.0
        else:
            stop, t1 = 42.0, 36.0
        return {'ticker': 'ZZZQ', 'strategy_id': 'S_x', 'direction': direction,
                'pct_nav': pct_nav, 'entry': 40.0, 'stop': stop, 't1': t1}

    def test_long_submits_fractional_qty(self):
        out = json.dumps({'id': 'oid-1', 'status': 'accepted'})
        with patch.object(ae, '_is_fractionable', lambda t: True), \
             patch.object(ae, '_alpaca_session_kind', return_value='rth'), \
             patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(0, out, '')) as run:
            ae.execute_single(_mock_session(), equity=3000.0,
                              order=self._order('long', 0.002), run_date='2026-11-23')
        submits = [c[0][0] for c in run.call_args_list if 'submit' in c[0][0]]
        assert submits, run.call_args_list
        argv = submits[-1]
        assert argv[argv.index('--qty') + 1] == '0.15'

    def test_short_sub_share_skips_without_cli(self):
        with patch.object(ae, '_is_fractionable', lambda t: True), \
             patch.object(ae, '_alpaca_session_kind', return_value='rth'), \
             patch('execution.alpaca_executor.subprocess.run') as run:
            res = ae.execute_single(_mock_session(), equity=3000.0,
                                    order=self._order('short', 0.012), run_date='2026-11-23')
        assert res['status'] == 'SKIP' and res['reason'] == 'short_sub_share'
        assert not run.called


# ── Task 2: partial reduce by decimal --qty ──────────────────────────────────
def test_reduce_qty_long_is_exact_fraction():
    assert ae._reduce_qty(3.0, 40.0, 120.0, is_short=False) == 1.0
    assert ae._reduce_qty(0.5, 10.0, 25.0, is_short=False) == 0.2


def test_reduce_qty_short_floors():
    assert ae._reduce_qty(-1.0, 70.0, 100.0, is_short=True) == 0.0
    assert ae._reduce_qty(-5.0, 55.0, 100.0, is_short=True) == 2.0


def test_reduce_qty_capped_and_zero_current():
    assert ae._reduce_qty(2.0, 500.0, 100.0, is_short=False) == 2.0
    assert ae._reduce_qty(-3.0, 1.0, 0.0, is_short=True) == 3.0


def test_is_fractional_qty():
    assert ae._is_fractional_qty(0.5) and ae._is_fractional_qty(2.000001)
    assert not ae._is_fractional_qty(3.0) and not ae._is_fractional_qty(3.0000000001)


def _rth_close(pos_qty, notional, current, monkeypatch):
    calls = []

    def fake_cli(args, timeout=None):
        calls.append(list(args))
        return True, {'id': 'oid-9', 'qty': '1', 'notional': '80'}, {}
    monkeypatch.setattr(ae, '_run_alpaca_cli', fake_cli)
    monkeypatch.setattr(ae, '_fetch_position_qty', lambda t: pos_qty)
    monkeypatch.setattr(ae, '_cancel_open_equity_orders', lambda t: None)
    res, oid = ae._rth_position_close('T', 'coid1', notional_usd=notional,
                                      current_usd=current, equity=3000.0, pct_nav=0.01)
    return res, oid, calls


def test_rth_short_reduce_rounding_to_zero_skips_without_cli(monkeypatch):
    res, oid, calls = _rth_close(-1.0, 70.0, 100.0, monkeypatch)
    assert res['status'] == 'SKIP' and res['reason'] == 'reduce_rounds_to_zero'
    assert res['tif'] == 'day' and res['order_class'] == 'simple' and res['client_order_id'] == 'coid1'
    assert oid is None
    assert not [c for c in calls if c[:2] == ['position', 'close']]


def test_rth_long_partial_reduce_passes_qty(monkeypatch):
    res, oid, calls = _rth_close(3.0, 80.0, 120.0, monkeypatch)
    close = [c for c in calls if c[:2] == ['position', 'close']]
    assert len(close) == 1
    argv = close[0]
    assert argv[:4] == ['position', 'close', '--symbol-or-asset-id', 'T']
    assert argv[argv.index('--qty') + 1] == '2'
    assert '--percentage' not in argv
    assert res['status'] == 'submitted' and oid == 'oid-9'


def test_rth_long_one_third_reduce_qty(monkeypatch):
    _, _, calls = _rth_close(3.0, 40.0, 120.0, monkeypatch)
    argv = [c for c in calls if c[:2] == ['position', 'close']][0]
    assert argv[argv.index('--qty') + 1] == '1'


def test_rth_fractional_position_reduce_qty(monkeypatch):
    _, _, calls = _rth_close(0.5, 40.0, 100.0, monkeypatch)
    argv = [c for c in calls if c[:2] == ['position', 'close']][0]
    assert argv[argv.index('--qty') + 1] == '0.2'


def test_rth_reduce_lookup_failure_falls_back_to_percentage(monkeypatch):
    res, _, calls = _rth_close(0.0, 70.0, 100.0, monkeypatch)
    argv = [c for c in calls if c[:2] == ['position', 'close']][0]
    assert argv[argv.index('--percentage') + 1] == '70.0'
    assert '--qty' not in argv and res['status'] == 'submitted'


def test_rth_full_close_has_no_qty_or_percentage(monkeypatch):
    _, _, calls = _rth_close(3.0, 120.0, 120.0, monkeypatch)
    argv = [c for c in calls if c[:2] == ['position', 'close']][0]
    assert argv == ['position', 'close', '--symbol-or-asset-id', 'T']


# ── carry-over 1: _is_fractionable connects once per process, closes conn ────
def test_is_fractionable_single_attempt_on_failure(monkeypatch):
    monkeypatch.setattr(ae, '_FRACTIONABLE_CACHE', {})
    monkeypatch.setattr(ae, '_FRACTIONABLE_WARNED', [False])
    monkeypatch.setattr(ae, '_FRACTIONABLE_LOADED', [False])
    monkeypatch.setenv('POSTGRES_URI', 'postgres://x')
    n = []

    def boom(uri):
        n.append(1)
        raise RuntimeError('down')
    monkeypatch.setattr(ae.psycopg2, 'connect', boom)
    assert ae._is_fractionable('A') is True and ae._is_fractionable('B') is True
    assert len(n) == 1


def test_is_fractionable_empty_table_single_attempt_and_closes(monkeypatch):
    monkeypatch.setattr(ae, '_FRACTIONABLE_CACHE', {})
    monkeypatch.setattr(ae, '_FRACTIONABLE_WARNED', [False])
    monkeypatch.setattr(ae, '_FRACTIONABLE_LOADED', [False])
    monkeypatch.setenv('POSTGRES_URI', 'postgres://x')
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = []
    n = []
    monkeypatch.setattr(ae.psycopg2, 'connect', lambda uri: (n.append(1), conn)[1])
    assert ae._is_fractionable('A') is True and ae._is_fractionable('B') is True
    assert len(n) == 1
    conn.close.assert_called()


def test_is_fractionable_reads_flag_and_closes(monkeypatch):
    monkeypatch.setattr(ae, '_FRACTIONABLE_CACHE', {})
    monkeypatch.setattr(ae, '_FRACTIONABLE_WARNED', [False])
    monkeypatch.setattr(ae, '_FRACTIONABLE_LOADED', [False])
    monkeypatch.setenv('POSTGRES_URI', 'postgres://x')
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [('AAA', False), ('BBB', True)]
    monkeypatch.setattr(ae.psycopg2, 'connect', lambda uri: conn)
    assert ae._is_fractionable('AAA') is False and ae._is_fractionable('BBB') is True
    assert ae._is_fractionable('ZZZ') is True
    conn.close.assert_called()


# ── carry-over 2: close-only extended-hours path ─────────────────────────────
def _ext_close_order(notional_pct_nav):
    return {'ticker': 'ZZZQ', 'strategy_id': '__close_orphan__', 'direction': 'long',
            'close_only': True, 'pct_nav': notional_pct_nav,
            'notional_usd': 10.0, 'current_usd': 10.0}


def test_close_only_ext_hours_passes_fractional_qty(monkeypatch):
    calls = []

    def fake_submit(**kw):
        calls.append(kw)
        return True, {'id': 'oid-x'}, {}
    monkeypatch.setattr(ae, '_alpaca_session_kind', lambda: 'premarket')
    monkeypatch.setattr(ae, '_skip_extended_hours', lambda t, s: None)
    monkeypatch.setattr(ae, '_pick_limit_price', lambda t, s: 40.0)
    monkeypatch.setattr(ae, '_is_fractionable', lambda t: True)
    monkeypatch.setattr(ae, '_submit_order_via_cli', fake_submit)
    res = ae.execute_single(_mock_session(), equity=3000.0,
                            order=_ext_close_order(0.002), run_date='2026-11-23')
    assert res['status'] == 'submitted'
    assert calls and calls[0]['qty'] == 0.15 and calls[0]['extended_hours'] is True


def test_close_only_ext_hours_dust_skips(monkeypatch):
    called = []
    monkeypatch.setattr(ae, '_alpaca_session_kind', lambda: 'afterhours')
    monkeypatch.setattr(ae, '_skip_extended_hours', lambda t, s: None)
    monkeypatch.setattr(ae, '_pick_limit_price', lambda t, s: 40.0)
    monkeypatch.setattr(ae, '_is_fractionable', lambda t: True)
    monkeypatch.setattr(ae, '_submit_order_via_cli', lambda **kw: called.append(kw))
    res = ae.execute_single(_mock_session(), equity=1000.0,
                            order=_ext_close_order(0.0005), run_date='2026-11-23')
    assert res['status'] == 'SKIP' and res['reason'] == 'dust_below_1usd'
    assert not called


# ── carry-over 3: OPG never carries a fractional qty ─────────────────────────
def test_opg_fractional_position_falls_back_to_day_sweep(monkeypatch):
    calls = []

    def fake_cli(args, timeout=None):
        calls.append(list(args))
        return True, {'id': 'oid-s', 'qty': '0.5', 'notional': '20'}, {}
    monkeypatch.setenv('OPENCLAW_OPEN_CLOSE_MODE', 'opg_then_day')
    sessions = iter(['premarket', 'rth', 'rth', 'rth'])
    monkeypatch.setattr(ae, '_alpaca_session_kind', lambda: next(sessions, 'rth'))
    monkeypatch.setattr(ae, '_run_alpaca_cli', fake_cli)
    monkeypatch.setattr(ae, '_fetch_position_qty', lambda t: 0.5)
    monkeypatch.setattr(ae, '_cancel_open_equity_orders', lambda t: None)
    monkeypatch.setattr(ae, '_poll_to_terminal',
                        lambda oid, **kw: {'status': 'filled', 'filled_qty': '0.5',
                                           'filled_avg_price': '40'})
    submitted = []
    monkeypatch.setattr(ae, '_submit_order_via_cli',
                        lambda **kw: (submitted.append(kw), (True, {'id': 'x'}, {}))[1])
    order = {'ticker': 'ZZZQ', 'strategy_id': '__close_orphan__', 'direction': 'long',
             'close_only': True, 'is_dropped': True, 'pct_nav': 0.0,
             'notional_usd': 20.0, 'current_usd': 20.0}
    res = ae.execute_single(_mock_session(), equity=3000.0, order=order,
                            run_date='2026-11-23')
    assert not [s for s in submitted if s.get('tif') == 'opg']
    assert not any('opg' in c for c in calls)
    assert res['status'] == 'filled'
