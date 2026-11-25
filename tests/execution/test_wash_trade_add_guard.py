"""2026-11-25: an ADD to a held equity position cancels the symbol's resting
exit orders first (settle-polled) so the broker does not refuse it as a
'potential wash trade'; a refusal that still slips through is retried once
after a second sweep. Fresh opens are untouched."""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import alpaca_executor as ae  # noqa: E402


def _proc(stdout, rc=0, stderr=''):
    m = MagicMock(); m.returncode = rc; m.stdout = stdout; m.stderr = stderr; return m


def _sess():
    sess = MagicMock(); r = MagicMock(); r.status_code = 200
    r.json.return_value = {'latestTrade': {'p': 40.0}, 'latestQuote': {'bp': 39.98, 'ap': 40.02}}
    sess.get.return_value = r
    return sess


def _order(direction='long', pct_nav=0.0008):
    stop, t1 = (38.0, 44.0) if direction == 'long' else (42.0, 36.0)
    return {'ticker': 'T', 'strategy_id': 'S_x', 'direction': direction,
            'pct_nav': pct_nav, 'entry': 40.0, 'stop': stop, 't1': t1}


class _Broker:
    """argv-dispatching stand-in for the alpaca CLI."""
    def __init__(self, resting, *, wash_first=False):
        self.resting = list(resting); self.calls = []; self.wash_first = wash_first; self.submits = 0

    def __call__(self, cmd, **kw):
        argv = list(cmd); self.calls.append(argv[1:3])
        a = argv[1:3]
        if a == ['order', 'list']:
            return _proc(json.dumps(self.resting))
        if a == ['order', 'cancel']:
            self.resting = []; return _proc('{}')
        if a == ['order', 'get']:
            return _proc(json.dumps({'status': 'canceled', 'filled_qty': '0'}))
        if a == ['position', 'get']:
            return _proc(json.dumps({'symbol': 'T', 'qty': '10', 'qty_available': '10'}))
        if a == ['asset', 'get']:
            return _proc(json.dumps({'symbol': 'T', 'class': 'us_equity', 'tradable': True, 'fractionable': True}))
        if a == ['order', 'submit']:
            self.submits += 1
            if self.wash_first and self.submits == 1:
                return _proc('', rc=1, stderr=json.dumps(
                    {'status': 403, 'error': 'potential wash trade detected. use complex orders'}))
            return _proc(json.dumps({'id': f'add-{self.submits}', 'status': 'accepted'}))
        return _proc('{}')


@pytest.fixture(autouse=True)
def _clean():
    ae._asset_cache.clear(); ae._unsupported_assets.clear(); ae._broker_positions_cache.clear()
    ae._broker_positions_loaded = True
    ae._BATCH.update({'session': None, 'close_at': None})
    yield
    ae._broker_positions_cache.clear()


def _run(order, broker):
    with patch.object(ae, '_alpaca_session_kind', return_value='rth'), \
         patch.object(ae, '_is_fractionable', lambda t: True), \
         patch.object(ae, '_poll_to_terminal', lambda *a, **k: {'status': 'canceled'}), \
         patch('execution.alpaca_executor.subprocess.run', side_effect=broker):
        return ae.execute_single(_sess(), equity=100_000.0, order=order, run_date='2026-11-25')


def test_add_to_held_long_cancels_resting_stop_before_submit():
    ae._broker_positions_cache['T'] = 10.0
    broker = _Broker(resting=[{'id': 'stp-1', 'symbol': 'T', 'type': 'stop', 'side': 'sell'}])
    res = _run(_order('long'), broker)
    assert res['status'] == 'submitted' and res['order_id'] == 'add-1'
    kinds = broker.calls
    assert ['order', 'cancel'] in kinds
    assert kinds.index(['order', 'cancel']) < kinds.index(['order', 'submit'])
    assert broker.submits == 1


def test_add_to_held_short_cancels_resting_cover_before_submit():
    ae._broker_positions_cache['T'] = -10.0
    broker = _Broker(resting=[{'id': 'oco-1', 'symbol': 'T', 'type': 'limit', 'side': 'buy'}])
    res = _run(_order('short', pct_nav=0.004), broker)   # $400 / 40 = 10 sh short add
    assert res['status'] == 'submitted'
    assert ['order', 'cancel'] in broker.calls
    assert broker.calls.index(['order', 'cancel']) < broker.calls.index(['order', 'submit'])


def test_fresh_open_never_sweeps_orders():
    broker = _Broker(resting=[{'id': 'x', 'symbol': 'T', 'type': 'stop'}])   # would be cancelled if swept
    res = _run(_order('long'), broker)
    assert res['status'] == 'submitted'
    assert ['order', 'list'] not in broker.calls and ['order', 'cancel'] not in broker.calls


def test_wash_refusal_after_sweep_is_retried_once_after_second_sweep():
    ae._broker_positions_cache['T'] = 10.0
    broker = _Broker(resting=[], wash_first=True)          # nothing resting at first sweep
    broker.resting_after_first_submit = True
    # an exit order appears between sweep and submit → first submit is refused
    orig = broker.__call__
    def flaky(cmd, **kw):
        argv = list(cmd)
        if argv[1:3] == ['order', 'list'] and broker.submits == 1:
            return _proc(json.dumps([{'id': 'late-stop', 'symbol': 'T', 'type': 'stop'}]))
        return orig(cmd, **kw)
    with patch.object(ae, '_alpaca_session_kind', return_value='rth'), \
         patch.object(ae, '_is_fractionable', lambda t: True), \
         patch.object(ae, '_poll_to_terminal', lambda *a, **k: {'status': 'canceled'}), \
         patch('execution.alpaca_executor.subprocess.run', side_effect=flaky):
        res = ae.execute_single(_sess(), equity=100_000.0, order=_order('long'), run_date='2026-11-25')
    assert res['status'] == 'submitted' and res['order_id'] == 'add-2'
    assert broker.submits == 2
    assert broker.calls.count(['order', 'cancel']) == 1      # the late stop was cleared before the retry


def test_wash_refusal_twice_is_reported_as_error_not_looped():
    ae._broker_positions_cache['T'] = 10.0
    class Always(_Broker):
        def __call__(self, cmd, **kw):
            argv = list(cmd)
            if argv[1:3] == ['order', 'submit']:
                self.submits += 1; self.calls.append(argv[1:3])
                return _proc('', rc=1, stderr=json.dumps({'status': 403, 'error': 'potential wash trade detected. use complex orders'}))
            return super().__call__(cmd, **kw)
    broker = Always(resting=[])
    res = _run(_order('long'), broker)
    assert res['status'] == 'error' and 'wash trade' in res['body']
    assert broker.submits == 2
