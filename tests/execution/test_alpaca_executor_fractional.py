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
