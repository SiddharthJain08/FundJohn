"""tests/test_regime_liquidator.py

Unit tests for src/execution/regime_liquidator.py.

Verifies:
  - dry-run gate is OFF by default (no subprocess)
  - state-unchanged → noop, no CLI calls
  - corrupt regime file → error, no CLI calls
  - cancel-before-close ordering when live
  - pre-market path uses --time-in-force opg
  - RTH path uses `position close --symbol-or-asset-id`
  - Redis sentinel makes a same-day re-run a no-op

Run:
    pytest tests/test_regime_liquidator.py -v
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import regime_liquidator as rl  # noqa: E402


def _mock_proc(returncode=0, stdout='', stderr=''):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


REGIME_CHANGED = {
    'date': '2026-05-07',
    'state': 'HIGH_VOL',
    'prior_state': 'TRANSITIONING',
    'vix_level': 26.0,
}

REGIME_SAME = {
    'date': '2026-05-07',
    'state': 'TRANSITIONING',
    'prior_state': 'TRANSITIONING',
    'vix_level': 17.0,
}

OPEN_OC_PARENT = {
    'id': 'parent-uuid', 'client_order_id': 'AX20260506_AAPL_S1',
    'symbol': 'AAPL', 'status': 'filled',
    'legs': [
        {'id': 'tp-leg', 'type': 'limit', 'symbol': 'AAPL',
         'client_order_id': 'AX20260506_AAPL_S1.tp'},
        {'id': 'sl-leg', 'type': 'stop',  'symbol': 'AAPL',
         'client_order_id': 'AX20260506_AAPL_S1.sl'},
    ],
}

CLOCK_OPEN = {'is_open': True}
CLOCK_CLOSED = {'is_open': False}

POSITIONS_AAPL_LONG = [{'symbol': 'AAPL', 'qty': '50', 'side': 'long',
                        'market_value': '7500'}]
SUBMISSION_ROW = ('AX20260506_AAPL_S1', 'parent-uuid', 'AAPL', 'long', 50)


class _StubRedis:
    """In-memory shim with .get and .set(ex=...). Used to verify sentinel
    behaviour without requiring a real Redis."""
    def __init__(self):
        self.store = {}
    def get(self, k):
        return self.store.get(k)
    def set(self, k, v, ex=None):
        self.store[k] = v
    def ping(self):
        return True


class TestGuards(unittest.TestCase):
    def setUp(self):
        os.environ.pop('OPENCLAW_ALPACA_LIVE_LIQUIDATE', None)

    def test_noop_when_state_unchanged(self):
        with patch.object(rl, '_load_regime', return_value=REGIME_SAME), \
             patch.object(rl, '_redis', return_value=None), \
             patch('execution.regime_liquidator.subprocess.run') as mock_run:
            result = rl.liquidate_on_regime_change()
        self.assertEqual(result['action'], 'noop')
        mock_run.assert_not_called()

    def test_corrupt_regime_file_aborts_cleanly(self):
        with patch.object(rl, '_load_regime', return_value=None), \
             patch('execution.regime_liquidator.subprocess.run') as mock_run:
            result = rl.liquidate_on_regime_change()
        self.assertEqual(result['action'], 'error')
        self.assertEqual(result['reason'], 'regime_unreadable')
        mock_run.assert_not_called()

    def test_idempotency_sentinel(self):
        stub = _StubRedis()
        stub.store['liquidate:fired:2026-05-07:TRANSITIONING->HIGH_VOL'] = '1'
        with patch.object(rl, '_load_regime', return_value=REGIME_CHANGED), \
             patch.object(rl, '_redis', return_value=stub), \
             patch('execution.regime_liquidator.subprocess.run') as mock_run:
            result = rl.liquidate_on_regime_change()
        self.assertEqual(result['action'], 'already_fired')
        self.assertEqual(result['transition'], 'TRANSITIONING->HIGH_VOL')
        mock_run.assert_not_called()


class TestDryRunDefault(unittest.TestCase):
    def setUp(self):
        os.environ.pop('OPENCLAW_ALPACA_LIVE_LIQUIDATE', None)

    def test_dry_run_default_no_destructive_cli(self):
        """Env unset → returns dry_run plan; only read-only CLI calls
        (clock, position list, order list) ever fire — no cancel, no
        position close, no order submit."""
        os.environ['POSTGRES_URI'] = 'postgres://stub/stub'

        # Stub psycopg2.connect → returns conn whose cursor returns the
        # OpenClaw submission row.
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [SUBMISSION_ROW]
        mock_conn.cursor.return_value = mock_cur

        proc_seq = [
            _mock_proc(0, json.dumps(CLOCK_CLOSED)),       # clock
            _mock_proc(0, json.dumps(POSITIONS_AAPL_LONG)),  # position list
            _mock_proc(0, json.dumps([OPEN_OC_PARENT])),  # order list
        ]

        with patch.object(rl, '_load_regime', return_value=REGIME_CHANGED), \
             patch.object(rl, '_redis', return_value=None), \
             patch.object(rl, '_post_to_discord', return_value=True), \
             patch('psycopg2.connect',
                   return_value=mock_conn), \
             patch('execution.regime_liquidator.subprocess.run',
                   side_effect=proc_seq) as mock_run:
            result = rl.liquidate_on_regime_change()

        self.assertEqual(result['action'], 'dry_run')
        self.assertFalse(result['live'])
        self.assertIn('AAPL', result['plan']['oc_symbols'])
        # Inspect every subprocess call: none should be cancel/close/submit.
        for call in mock_run.call_args_list:
            argv = call[0][0]
            self.assertNotIn('cancel', argv)
            # `position close` or `order submit` are the destructive ones.
            if argv[1:3] == ['position', 'close']:
                self.fail(f'Dry-run issued destructive CLI: {argv}')
            if argv[1:3] == ['order', 'submit']:
                self.fail(f'Dry-run issued destructive CLI: {argv}')


class TestLiveOrdering(unittest.TestCase):
    """When live, all cancellations must precede the first close call."""
    def setUp(self):
        os.environ['OPENCLAW_ALPACA_LIVE_LIQUIDATE'] = '1'
        os.environ['POSTGRES_URI'] = 'postgres://stub/stub'

    def tearDown(self):
        os.environ.pop('OPENCLAW_ALPACA_LIVE_LIQUIDATE', None)

    def _run_with_proc_seq(self, proc_seq):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [SUBMISSION_ROW]
        mock_conn.cursor.return_value = mock_cur
        with patch.object(rl, '_load_regime', return_value=REGIME_CHANGED), \
             patch.object(rl, '_redis', return_value=_StubRedis()), \
             patch.object(rl, '_post_to_discord', return_value=True), \
             patch('execution.regime_liquidator.time.sleep'), \
             patch('psycopg2.connect',
                   return_value=mock_conn), \
             patch('execution.regime_liquidator.subprocess.run',
                   side_effect=proc_seq) as mock_run:
            result = rl.liquidate_on_regime_change()
        return result, mock_run

    def test_cancel_before_close_pre_market(self):
        """Pre-market path: clock(closed) → order list → 2 cancels → position
        list → order submit (OPG)."""
        proc_seq = [
            _mock_proc(0, json.dumps(CLOCK_CLOSED)),       # _market_is_open
            _mock_proc(0, json.dumps([OPEN_OC_PARENT])),  # order list
            _mock_proc(0, json.dumps({'id': 'parent-uuid', 'status': 'cancelled'})),  # cancel parent
            _mock_proc(0, json.dumps({'id': 'tp-leg',     'status': 'cancelled'})),  # cancel tp
            _mock_proc(0, json.dumps({'id': 'sl-leg',     'status': 'cancelled'})),  # cancel sl
            _mock_proc(0, json.dumps(POSITIONS_AAPL_LONG)),  # position list
            _mock_proc(0, json.dumps({'id': 'opg-order', 'status': 'accepted'})),  # order submit
        ]
        result, mock_run = self._run_with_proc_seq(proc_seq)
        self.assertEqual(result['action'], 'liquidated')

        # Find the index of the first destructive close call.
        first_close_idx = None
        cancel_indices = []
        for i, call in enumerate(mock_run.call_args_list):
            argv = call[0][0]
            if argv[1:3] == ['order', 'cancel']:
                cancel_indices.append(i)
            if argv[1:3] == ['order', 'submit'] or argv[1:3] == ['position', 'close']:
                first_close_idx = i
                break
        self.assertIsNotNone(first_close_idx, 'no close call issued')
        self.assertTrue(cancel_indices, 'no cancel calls issued')
        self.assertTrue(max(cancel_indices) < first_close_idx,
                        'cancel calls must precede the first close call')

        # Verify the OPG market order shape.
        submit_argv = mock_run.call_args_list[first_close_idx][0][0]
        self.assertEqual(submit_argv[1:3], ['order', 'submit'])
        self.assertIn('--time-in-force', submit_argv)
        tif_idx = submit_argv.index('--time-in-force')
        self.assertEqual(submit_argv[tif_idx + 1], 'opg')
        self.assertEqual(submit_argv[submit_argv.index('--side') + 1], 'sell')
        self.assertEqual(submit_argv[submit_argv.index('--type') + 1], 'market')

    def test_rth_uses_position_close(self):
        """RTH path: clock(open) → cancels → position list → position close."""
        proc_seq = [
            _mock_proc(0, json.dumps(CLOCK_OPEN)),         # _market_is_open
            _mock_proc(0, json.dumps([OPEN_OC_PARENT])),  # order list
            _mock_proc(0, json.dumps({'id': 'parent-uuid', 'status': 'cancelled'})),
            _mock_proc(0, json.dumps({'id': 'tp-leg',     'status': 'cancelled'})),
            _mock_proc(0, json.dumps({'id': 'sl-leg',     'status': 'cancelled'})),
            _mock_proc(0, json.dumps(POSITIONS_AAPL_LONG)),
            _mock_proc(0, json.dumps({'symbol': 'AAPL', 'qty': '50',
                                       'status': 'closed'})),
        ]
        result, mock_run = self._run_with_proc_seq(proc_seq)
        self.assertEqual(result['action'], 'liquidated')

        # Last call should be `position close --symbol-or-asset-id AAPL`.
        last_argv = mock_run.call_args_list[-1][0][0]
        self.assertEqual(last_argv[1:3], ['position', 'close'])
        self.assertIn('--symbol-or-asset-id', last_argv)
        idx = last_argv.index('--symbol-or-asset-id')
        self.assertEqual(last_argv[idx + 1], 'AAPL')


if __name__ == '__main__':
    unittest.main()
