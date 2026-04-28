"""tests/test_alpaca_replace_stop.py

Unit tests for src/execution/alpaca_replace_stop.py (Phase 2.4 of
alpaca-cli integration). Verifies the dry-run gate is OFF by default
and only fires the live CLI when OPENCLAW_ALPACA_LIVE_REPLACE=1.

Run:
    pytest tests/test_alpaca_replace_stop.py -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import alpaca_replace_stop as ars  # noqa: E402


def _mock_proc(returncode=0, stdout='', stderr=''):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


class TestDryRunGate(unittest.TestCase):
    def test_replace_skipped_when_flag_unset(self):
        """Default env (no OPENCLAW_ALPACA_LIVE_REPLACE): subprocess never invoked."""
        with patch.dict('os.environ', {}, clear=False):
            # Make sure flag is NOT set
            import os
            os.environ.pop('OPENCLAW_ALPACA_LIVE_REPLACE', None)
            with patch('execution.alpaca_replace_stop.subprocess.run') as mock_run:
                result = ars.replace_stop_for_coid('AX_TEST_COID', 99.50)
        self.assertEqual(result['status'], 'skipped_dry_run')
        self.assertEqual(result['coid'], 'AX_TEST_COID')
        self.assertEqual(result['new_stop'], '99.50')
        mock_run.assert_not_called()

    def test_replace_invokes_cli_when_flag_set(self):
        """OPENCLAW_ALPACA_LIVE_REPLACE=1 → subprocess called with correct args."""
        parent_order = json.dumps({
            'id':              'parent-uuid',
            'client_order_id': 'AX_TEST_COID',
            'status':          'filled',
            'legs': [
                {'id': 'tp-leg-uuid', 'type': 'limit', 'limit_price': '110'},
                {'id': 'sl-leg-uuid', 'type': 'stop',  'stop_price':  '95'},
            ],
        })
        replace_resp = json.dumps({'id': 'sl-leg-uuid', 'stop_price': '99.50',
                                   'replaced_by': None, 'status': 'new'})
        with patch.dict('os.environ',
                        {'OPENCLAW_ALPACA_LIVE_REPLACE': '1'}, clear=False):
            with patch('execution.alpaca_replace_stop.subprocess.run',
                       side_effect=[
                           _mock_proc(0, parent_order, ''),  # get-by-client-id
                           _mock_proc(0, replace_resp,  ''),  # order replace
                       ]) as mock_run:
                result = ars.replace_stop_for_coid('AX_TEST_COID', 99.50)
        self.assertEqual(result['status'], 'replaced')
        self.assertEqual(result['leg_id'], 'sl-leg-uuid')
        self.assertEqual(result['new_stop'], '99.50')
        self.assertEqual(mock_run.call_count, 2)
        # Second call: order replace --order-id sl-leg-uuid --stop-price 99.50
        replace_argv = mock_run.call_args_list[1][0][0]
        self.assertEqual(replace_argv[1:3], ['order', 'replace'])
        self.assertEqual(replace_argv[replace_argv.index('--order-id')   + 1], 'sl-leg-uuid')
        self.assertEqual(replace_argv[replace_argv.index('--stop-price') + 1], '99.50')

    def test_lookup_failure_short_circuits(self):
        """If get-by-client-id fails, no replace attempt is made."""
        with patch.dict('os.environ',
                        {'OPENCLAW_ALPACA_LIVE_REPLACE': '1'}, clear=False):
            err = json.dumps({'status': 404, 'error': 'order not found'})
            with patch('execution.alpaca_replace_stop.subprocess.run',
                       return_value=_mock_proc(1, '', err)) as mock_run:
                result = ars.replace_stop_for_coid('AX_MISSING', 99.50)
        self.assertEqual(result['status'], 'lookup_failed')
        self.assertEqual(mock_run.call_count, 1)


class TestLegSelection(unittest.TestCase):
    def test_finds_stop_loss_leg_among_take_profit(self):
        order = {
            'id': 'parent', 'legs': [
                {'id': 'tp-leg', 'type': 'limit', 'limit_price': '110'},
                {'id': 'sl-leg', 'type': 'stop',  'stop_price':  '95'},
            ],
        }
        leg = ars.find_stop_loss_leg(order)
        self.assertEqual(leg['id'], 'sl-leg')

    def test_returns_none_when_no_stop_loss_leg(self):
        order = {'id': 'p', 'legs': [
            {'id': 'tp-leg', 'type': 'limit', 'limit_price': '110'},
        ]}
        self.assertIsNone(ars.find_stop_loss_leg(order))

    def test_returns_none_for_non_dict(self):
        self.assertIsNone(ars.find_stop_loss_leg(None))
        self.assertIsNone(ars.find_stop_loss_leg('not-a-dict'))


if __name__ == '__main__':
    unittest.main()
