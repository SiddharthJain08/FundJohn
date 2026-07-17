"""tests/test_regime_liquidator.py

Unit tests for src/execution/regime_liquidator.py.

Verifies:
  - dry-run gate is OFF by default (no subprocess)
  - state-unchanged → noop, no CLI calls
  - corrupt regime file → error, no CLI calls
  - RTH-only guard refuses pre/post-market (Phase 4 2026-05-19)
  - cancel-before-close ordering when live
  - RTH path uses `position close --symbol-or-asset-id`
  - Post-submit poll-to-terminal classifies real outcome
    (filled / partial / pending / rejected / submit_error)
  - May-9 sentinel guard: zero-fill runs do not seal sentinel/cooldown
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

ROOT = Path(__file__).resolve().parents[2]
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
        position close, no order submit. Phase 4 guard requires RTH so
        we mock CLOCK_OPEN."""
        os.environ['POSTGRES_URI'] = 'postgres://stub/stub'

        # Stub psycopg2.connect → returns conn whose cursor returns the
        # OpenClaw submission row.
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [SUBMISSION_ROW]
        mock_conn.cursor.return_value = mock_cur

        proc_seq = [
            _mock_proc(0, json.dumps(CLOCK_OPEN)),         # clock (RTH guard)
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
    """When live, all cancellations must precede the first close call.
    The OPG (pre-market) close path was removed 2026-05-19 — only the
    RTH `position close` path remains."""
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

    def test_rth_uses_position_close(self):
        """RTH path: clock(open) → order list → 3 cancels → position list
        → position close → order get (poll terminal)."""
        filled_order = {'id': 'close-order-id', 'status': 'filled',
                        'filled_qty': '50'}
        proc_seq = [
            _mock_proc(0, json.dumps(CLOCK_OPEN)),         # _market_is_open
            _mock_proc(0, json.dumps([OPEN_OC_PARENT])),  # order list
            _mock_proc(0, json.dumps({'id': 'parent-uuid', 'status': 'cancelled'})),
            _mock_proc(0, json.dumps({'id': 'tp-leg',     'status': 'cancelled'})),
            _mock_proc(0, json.dumps({'id': 'sl-leg',     'status': 'cancelled'})),
            _mock_proc(0, json.dumps(POSITIONS_AAPL_LONG)),  # position list
            _mock_proc(0, json.dumps({'id': 'close-order-id',
                                       'symbol': 'AAPL', 'qty': '50',
                                       'status': 'pending_new'})),  # position close
            _mock_proc(0, json.dumps(filled_order)),  # order get (poll)
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
            if (first_close_idx is None
                    and argv[1:3] == ['position', 'close']):
                first_close_idx = i
        self.assertIsNotNone(first_close_idx, 'no close call issued')
        self.assertTrue(cancel_indices, 'no cancel calls issued')
        self.assertTrue(max(cancel_indices) < first_close_idx,
                        'cancel calls must precede the first close call')

        # `position close --symbol-or-asset-id AAPL`.
        close_argv = mock_run.call_args_list[first_close_idx][0][0]
        self.assertEqual(close_argv[1:3], ['position', 'close'])
        self.assertIn('--symbol-or-asset-id', close_argv)
        idx = close_argv.index('--symbol-or-asset-id')
        self.assertEqual(close_argv[idx + 1], 'AAPL')

        # NO `order submit` (OPG path is gone).
        for call in mock_run.call_args_list:
            argv = call[0][0]
            self.assertNotEqual(argv[1:3], ['order', 'submit'],
                                f'OPG path should never fire: {argv}')
            self.assertNotIn('opg', argv)


class TestForceOverride(unittest.TestCase):
    """`--force` / force_override=True bypasses the same-state veto.
    The audit/sentinel transition becomes MANUAL_FORCE->{state}, distinct
    from natural regime-change firings on the same date so neither
    masks the other's idempotency check."""

    def setUp(self):
        os.environ.pop('OPENCLAW_ALPACA_LIVE_LIQUIDATE', None)

    def test_force_bypasses_same_state_noop(self):
        """Without force, REGIME_SAME → noop. With force=True, the pipeline
        proceeds (dry-run path here, since live gate is off). RTH required
        post-Phase-4."""
        os.environ['POSTGRES_URI'] = 'postgres://stub/stub'

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [SUBMISSION_ROW]
        mock_conn.cursor.return_value = mock_cur

        proc_seq = [
            _mock_proc(0, json.dumps(CLOCK_OPEN)),
            _mock_proc(0, json.dumps(POSITIONS_AAPL_LONG)),
            _mock_proc(0, json.dumps([OPEN_OC_PARENT])),
        ]
        with patch.object(rl, '_load_regime', return_value=REGIME_SAME), \
             patch.object(rl, '_redis', return_value=None), \
             patch.object(rl, '_post_to_discord', return_value=True), \
             patch('psycopg2.connect', return_value=mock_conn), \
             patch('execution.regime_liquidator.subprocess.run',
                   side_effect=proc_seq):
            result = rl.liquidate_on_regime_change(force_override=True)
        self.assertEqual(result['action'], 'dry_run',
                         f'force should bypass same-state noop, got {result}')
        self.assertIn('AAPL', result['plan']['oc_symbols'])
        # The synthetic transition tag must be MANUAL_FORCE-prefixed so
        # the audit/sentinel doesn't collide with natural regime changes.
        self.assertTrue(result['plan']['transition'].startswith('MANUAL_FORCE->'),
                        f'expected MANUAL_FORCE-prefixed transition, got {result["plan"]["transition"]}')

    def test_force_uses_unique_sentinel_key(self):
        """A force-fire must NOT be blocked by a sentinel set for a
        natural same-day regime-change run, and vice versa."""
        stub = _StubRedis()
        # Natural change already fired today and set its sentinel.
        stub.store['liquidate:fired:2026-05-07:TRANSITIONING->TRANSITIONING'] = '1'

        os.environ['POSTGRES_URI'] = 'postgres://stub/stub'
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [SUBMISSION_ROW]
        mock_conn.cursor.return_value = mock_cur
        proc_seq = [
            _mock_proc(0, json.dumps(CLOCK_OPEN)),
            _mock_proc(0, json.dumps(POSITIONS_AAPL_LONG)),
            _mock_proc(0, json.dumps([OPEN_OC_PARENT])),
        ]
        with patch.object(rl, '_load_regime', return_value=REGIME_SAME), \
             patch.object(rl, '_redis', return_value=stub), \
             patch.object(rl, '_post_to_discord', return_value=True), \
             patch('psycopg2.connect', return_value=mock_conn), \
             patch('execution.regime_liquidator.subprocess.run',
                   side_effect=proc_seq):
            result = rl.liquidate_on_regime_change(force_override=True)
        # Force runs use a different sentinel and so are NOT blocked.
        self.assertEqual(result['action'], 'dry_run')

    def test_force_handles_missing_prior_state(self):
        """force_override=True must work even if regime_latest.json has
        no prior_state (otherwise an operator can't recover from a
        corrupt regime file via the force path)."""
        os.environ['POSTGRES_URI'] = 'postgres://stub/stub'
        regime_no_prior = {'date': '2026-05-09', 'state': 'TRANSITIONING'}
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [SUBMISSION_ROW]
        mock_conn.cursor.return_value = mock_cur
        proc_seq = [
            _mock_proc(0, json.dumps(CLOCK_OPEN)),
            _mock_proc(0, json.dumps(POSITIONS_AAPL_LONG)),
            _mock_proc(0, json.dumps([OPEN_OC_PARENT])),
        ]
        with patch.object(rl, '_load_regime', return_value=regime_no_prior), \
             patch.object(rl, '_redis', return_value=None), \
             patch.object(rl, '_post_to_discord', return_value=True), \
             patch('psycopg2.connect', return_value=mock_conn), \
             patch('execution.regime_liquidator.subprocess.run',
                   side_effect=proc_seq):
            result = rl.liquidate_on_regime_change(force_override=True)
        self.assertEqual(result['action'], 'dry_run')
        self.assertEqual(result['plan']['transition'], 'MANUAL_FORCE->TRANSITIONING')

    def test_no_force_same_state_still_noops(self):
        """Regression: removing the force flag must NOT regress the
        natural same-state veto."""
        with patch.object(rl, '_load_regime', return_value=REGIME_SAME), \
             patch.object(rl, '_redis', return_value=None), \
             patch('execution.regime_liquidator.subprocess.run') as mock_run:
            result = rl.liquidate_on_regime_change()
        self.assertEqual(result['action'], 'noop')
        mock_run.assert_not_called()


class TestForceTransitionTag(unittest.TestCase):
    """`force_transition_tag` (introduced 2026-05-08 for intraday HMM)
    lets callers supply their own transition key — distinct from
    MANUAL_FORCE so daily/manual/intraday firings are all separable in
    audit + sentinels.

    Post Phase 1 (2026-05-19), no live caller passes this tag (the
    intraday HMM no longer fires the liquidator). The parameter is
    retained for backward compat + sentinel-key disambiguation."""

    def setUp(self):
        os.environ.pop('OPENCLAW_ALPACA_LIVE_LIQUIDATE', None)
        os.environ['POSTGRES_URI'] = 'postgres://stub/stub'

    def _run_dry(self, regime, **liquidate_kwargs):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [SUBMISSION_ROW]
        mock_conn.cursor.return_value = mock_cur
        proc_seq = [
            _mock_proc(0, json.dumps(CLOCK_OPEN)),
            _mock_proc(0, json.dumps(POSITIONS_AAPL_LONG)),
            _mock_proc(0, json.dumps([OPEN_OC_PARENT])),
        ]
        with patch.object(rl, '_load_regime', return_value=regime), \
             patch.object(rl, '_redis', return_value=None), \
             patch.object(rl, '_post_to_discord', return_value=True), \
             patch('psycopg2.connect', return_value=mock_conn), \
             patch('execution.regime_liquidator.subprocess.run',
                   side_effect=proc_seq):
            return rl.liquidate_on_regime_change(**liquidate_kwargs)

    def test_tag_implies_force_override(self):
        """Passing only force_transition_tag (without force_override=True)
        still bypasses the same-state veto — the tag implies override."""
        result = self._run_dry(
            REGIME_SAME,
            force_transition_tag='INTRADAY_HMM_TRANSITIONING_HIGH_VOL',
        )
        self.assertEqual(result['action'], 'dry_run')
        self.assertEqual(result['plan']['transition'],
                         'INTRADAY_HMM_TRANSITIONING_HIGH_VOL')

    def test_tag_overrides_manual_force_default(self):
        """When both force_override=True AND force_transition_tag are
        passed, the tag wins (not 'MANUAL_FORCE->...')."""
        result = self._run_dry(
            REGIME_SAME,
            force_override=True,
            force_transition_tag='INTRADAY_HMM_LOW_VOL_HIGH_VOL',
        )
        self.assertEqual(result['plan']['transition'],
                         'INTRADAY_HMM_LOW_VOL_HIGH_VOL')
        # Must NOT carry the MANUAL_FORCE prefix.
        self.assertNotIn('MANUAL_FORCE', result['plan']['transition'])

    def test_tag_does_not_collide_with_manual_force_sentinel(self):
        """A natural same-day MANUAL_FORCE sentinel must not block an
        intraday-tagged force fire (different sentinel keys)."""
        stub = _StubRedis()
        # Manual force already fired today.
        stub.store['liquidate:fired:2026-05-07:MANUAL_FORCE->TRANSITIONING'] = '1'

        os.environ['POSTGRES_URI'] = 'postgres://stub/stub'
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [SUBMISSION_ROW]
        mock_conn.cursor.return_value = mock_cur
        proc_seq = [
            _mock_proc(0, json.dumps(CLOCK_OPEN)),
            _mock_proc(0, json.dumps(POSITIONS_AAPL_LONG)),
            _mock_proc(0, json.dumps([OPEN_OC_PARENT])),
        ]
        with patch.object(rl, '_load_regime', return_value=REGIME_SAME), \
             patch.object(rl, '_redis', return_value=stub), \
             patch.object(rl, '_post_to_discord', return_value=True), \
             patch('psycopg2.connect', return_value=mock_conn), \
             patch('execution.regime_liquidator.subprocess.run',
                   side_effect=proc_seq):
            result = rl.liquidate_on_regime_change(
                force_transition_tag='INTRADAY_HMM_TRANSITIONING_HIGH_VOL',
            )
        self.assertEqual(result['action'], 'dry_run',
                         'intraday tag should not be blocked by MANUAL_FORCE sentinel')


class TestCooldownGate(unittest.TestCase):
    """Cooldown sentinel `liquidate:cooldown:{date}` blocks BOTH the
    daily 9 AM cron AND any intraday/manual force-fire — supersedes the
    transition-keyed sentinel. Set after every successful live fire."""

    def setUp(self):
        os.environ.pop('OPENCLAW_ALPACA_LIVE_LIQUIDATE', None)
        os.environ['POSTGRES_URI'] = 'postgres://stub/stub'

    def test_cooldown_blocks_natural_regime_change(self):
        """Pre-set cooldown → natural regime change returns noop."""
        stub = _StubRedis()
        stub.store['liquidate:cooldown:2026-05-07'] = '1'
        with patch.object(rl, '_load_regime', return_value=REGIME_CHANGED), \
             patch.object(rl, '_redis', return_value=stub), \
             patch('execution.regime_liquidator.subprocess.run') as mock_run:
            result = rl.liquidate_on_regime_change()
        self.assertEqual(result['action'], 'noop')
        self.assertEqual(result['reason'], 'cooldown_active')
        mock_run.assert_not_called()

    def test_cooldown_blocks_force_override(self):
        """Pre-set cooldown → force_override=True ALSO blocked. Cooldown
        supersedes force; the operator must explicitly clear cooldown
        to fire again within the window."""
        stub = _StubRedis()
        stub.store['liquidate:cooldown:2026-05-07'] = '1'
        with patch.object(rl, '_load_regime', return_value=REGIME_CHANGED), \
             patch.object(rl, '_redis', return_value=stub), \
             patch('execution.regime_liquidator.subprocess.run') as mock_run:
            result = rl.liquidate_on_regime_change(force_override=True)
        self.assertEqual(result['action'], 'noop')
        self.assertEqual(result['reason'], 'cooldown_active')
        mock_run.assert_not_called()

    def test_cooldown_blocks_intraday_tag(self):
        """Cooldown also supersedes force_transition_tag fires."""
        stub = _StubRedis()
        stub.store['liquidate:cooldown:2026-05-07'] = '1'
        with patch.object(rl, '_load_regime', return_value=REGIME_SAME), \
             patch.object(rl, '_redis', return_value=stub), \
             patch('execution.regime_liquidator.subprocess.run') as mock_run:
            result = rl.liquidate_on_regime_change(
                force_transition_tag='INTRADAY_HMM_LOW_VOL_HIGH_VOL',
            )
        self.assertEqual(result['action'], 'noop')
        self.assertEqual(result['reason'], 'cooldown_active')
        mock_run.assert_not_called()

    def test_live_fire_sets_cooldown_sentinel(self):
        """After a successful live fire, both `liquidate:fired:...`
        AND `liquidate:cooldown:{date}` must be set. Post-Phase-4 the
        close-attempt outcome must be 'filled' for n_close_ok to count."""
        os.environ['OPENCLAW_ALPACA_LIVE_LIQUIDATE'] = '1'
        try:
            stub = _StubRedis()
            mock_conn = MagicMock()
            mock_cur = MagicMock()
            mock_cur.fetchall.return_value = [SUBMISSION_ROW]
            mock_conn.cursor.return_value = mock_cur
            proc_seq = [
                _mock_proc(0, json.dumps(CLOCK_OPEN)),  # market open
                _mock_proc(0, json.dumps([OPEN_OC_PARENT])),  # order list
                _mock_proc(0, json.dumps({'id': 'p', 'status': 'cancelled'})),  # cancel parent
                _mock_proc(0, json.dumps({'id': 'tp', 'status': 'cancelled'})),
                _mock_proc(0, json.dumps({'id': 'sl', 'status': 'cancelled'})),
                _mock_proc(0, json.dumps(POSITIONS_AAPL_LONG)),  # position list
                _mock_proc(0, json.dumps({'id': 'close-id', 'symbol': 'AAPL',
                                          'status': 'pending_new'})),  # close
                _mock_proc(0, json.dumps({'id': 'close-id', 'status': 'filled',
                                          'filled_qty': '50'})),  # poll
            ]
            with patch.object(rl, '_load_regime', return_value=REGIME_CHANGED), \
                 patch.object(rl, '_redis', return_value=stub), \
                 patch.object(rl, '_post_to_discord', return_value=True), \
                 patch('execution.regime_liquidator.time.sleep'), \
                 patch('psycopg2.connect', return_value=mock_conn), \
                 patch('execution.regime_liquidator.subprocess.run',
                       side_effect=proc_seq):
                result = rl.liquidate_on_regime_change()
            self.assertEqual(result['action'], 'liquidated')
            self.assertIn('liquidate:fired:2026-05-07:TRANSITIONING->HIGH_VOL',
                          stub.store)
            self.assertIn('liquidate:cooldown:2026-05-07', stub.store)
        finally:
            os.environ.pop('OPENCLAW_ALPACA_LIVE_LIQUIDATE', None)


# ── Phase 4 new test classes (2026-05-19) ────────────────────────────────────


class TestRthOnlyGuard(unittest.TestCase):
    """Phase 4 (2026-05-19): the manual flatten path refuses to run
    outside RTH. Applies to both natural and forced paths. No OPG
    submission ever fires."""

    def setUp(self):
        # Force live so the guard is exercised on the live code path too;
        # but the guard should bite even in dry-run because it gates the
        # entire entry function. Tests assert no destructive CLI is run.
        os.environ['OPENCLAW_ALPACA_LIVE_LIQUIDATE'] = '1'
        os.environ['POSTGRES_URI'] = 'postgres://stub/stub'

    def tearDown(self):
        os.environ.pop('OPENCLAW_ALPACA_LIVE_LIQUIDATE', None)

    def _stub_conn(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [SUBMISSION_ROW]
        mock_conn.cursor.return_value = mock_cur
        return mock_conn

    def test_pre_market_clock_returns_error_no_submits(self):
        """clock(closed) → action=error, reason=not_rth; no destructive CLI."""
        proc_seq = [
            _mock_proc(0, json.dumps(CLOCK_CLOSED)),  # the only CLI call
        ]
        with patch.object(rl, '_load_regime', return_value=REGIME_CHANGED), \
             patch.object(rl, '_redis', return_value=None), \
             patch.object(rl, '_post_to_discord', return_value=True), \
             patch('psycopg2.connect', return_value=self._stub_conn()), \
             patch('execution.regime_liquidator.subprocess.run',
                   side_effect=proc_seq) as mock_run:
            result = rl.liquidate_on_regime_change()
        self.assertEqual(result['action'], 'error')
        self.assertEqual(result['reason'], 'not_rth')
        self.assertIn('AAPL', result['symbols_skipped'])
        # No `position close` or `order submit` ever issued.
        for call in mock_run.call_args_list:
            argv = call[0][0]
            self.assertNotEqual(argv[1:3], ['position', 'close'],
                                f'pre-market guard must not close: {argv}')
            self.assertNotEqual(argv[1:3], ['order', 'submit'],
                                f'pre-market guard must not submit: {argv}')
            self.assertNotIn('opg', argv)

    def test_after_market_clock_returns_error(self):
        """clock(closed, after-hours flavor) → same guard, also error."""
        # alpaca clock has the same is_open=False shape after hours.
        proc_seq = [
            _mock_proc(0, json.dumps({'is_open': False, 'next_open': 'tomorrow'})),
        ]
        with patch.object(rl, '_load_regime', return_value=REGIME_CHANGED), \
             patch.object(rl, '_redis', return_value=None), \
             patch.object(rl, '_post_to_discord', return_value=True), \
             patch('psycopg2.connect', return_value=self._stub_conn()), \
             patch('execution.regime_liquidator.subprocess.run',
                   side_effect=proc_seq) as mock_run:
            result = rl.liquidate_on_regime_change(force_override=True)
        self.assertEqual(result['action'], 'error')
        self.assertEqual(result['reason'], 'not_rth')
        for call in mock_run.call_args_list:
            argv = call[0][0]
            self.assertNotEqual(argv[1:3], ['position', 'close'])
            self.assertNotEqual(argv[1:3], ['order', 'submit'])


class TestPollAudit(unittest.TestCase):
    """Phase 4: after each successful close submission, poll the order
    to a terminal broker state and record the REAL outcome in the audit
    row (filled / partial / pending / rejected / submit_error)."""

    def setUp(self):
        os.environ['OPENCLAW_ALPACA_LIVE_LIQUIDATE'] = '1'
        os.environ['POSTGRES_URI'] = 'postgres://stub/stub'

    def tearDown(self):
        os.environ.pop('OPENCLAW_ALPACA_LIVE_LIQUIDATE', None)

    def _capture_audit_calls(self):
        """Build a mock conn + cursor that captures _record_audit's
        executemany/execute calls so the test can inspect the
        `result_status` arg."""
        mock_conn = MagicMock()
        # The submissions-query cursor returns SUBMISSION_ROW; subsequent
        # cursors are for the audit insert and we just need to capture
        # their .execute args.
        submissions_cur = MagicMock()
        submissions_cur.fetchall.return_value = [SUBMISSION_ROW]
        audit_cur = MagicMock()
        # cursor() returns submissions_cur first, then audit_cur on every
        # subsequent call.
        mock_conn.cursor.side_effect = [submissions_cur, audit_cur, audit_cur,
                                        audit_cur, audit_cur, audit_cur]
        return mock_conn, audit_cur

    def _run_close(self, close_payload, poll_payloads):
        """Run the live flatten with a single AAPL position, returning
        (result_dict, audit_cursor, mock_run)."""
        mock_conn, audit_cur = self._capture_audit_calls()
        proc_seq = [
            _mock_proc(0, json.dumps(CLOCK_OPEN)),
            _mock_proc(0, json.dumps([OPEN_OC_PARENT])),  # order list
            _mock_proc(0, json.dumps({'id': 'parent-uuid', 'status': 'cancelled'})),
            _mock_proc(0, json.dumps({'id': 'tp-leg', 'status': 'cancelled'})),
            _mock_proc(0, json.dumps({'id': 'sl-leg', 'status': 'cancelled'})),
            _mock_proc(0, json.dumps(POSITIONS_AAPL_LONG)),  # position list
            close_payload,  # position close
            *poll_payloads,  # order get polls
        ]
        with patch.object(rl, '_load_regime', return_value=REGIME_CHANGED), \
             patch.object(rl, '_redis', return_value=_StubRedis()), \
             patch.object(rl, '_post_to_discord', return_value=True), \
             patch('execution.regime_liquidator.time.sleep'), \
             patch('psycopg2.connect', return_value=mock_conn), \
             patch('execution.regime_liquidator.subprocess.run',
                   side_effect=proc_seq) as mock_run:
            result = rl.liquidate_on_regime_change()
        return result, audit_cur, mock_run

    def _audit_status(self, audit_cur):
        """Pull the result_status arg from the audit cursor's execute call."""
        # _record_audit issues a single INSERT per symbol; positional params
        # in the call are (sql, values_tuple). result_status is at index 9
        # in the VALUES tuple (matching the INSERT column order).
        self.assertTrue(audit_cur.execute.called,
                        'audit row was never inserted')
        values = audit_cur.execute.call_args[0][1]
        return values[9]

    def test_filled_outcome_recorded(self):
        """Submit succeeds + poll returns status=filled → 'filled'."""
        filled = _mock_proc(0, json.dumps({'id': 'cid', 'status': 'filled',
                                            'filled_qty': '50'}))
        close = _mock_proc(0, json.dumps({'id': 'cid', 'status': 'pending_new'}))
        result, audit_cur, _run = self._run_close(close, [filled])
        self.assertEqual(self._audit_status(audit_cur), 'filled')
        self.assertEqual(result['action'], 'liquidated')

    def test_partial_outcome_recorded(self):
        """Submit succeeds + poll returns terminal with partial fill → 'partial'."""
        # First poll: still working. Second: terminal expired with partial.
        polls = [
            _mock_proc(0, json.dumps({'id': 'cid', 'status': 'partially_filled',
                                       'filled_qty': '20'})),
            _mock_proc(0, json.dumps({'id': 'cid', 'status': 'expired',
                                       'filled_qty': '20'})),
        ]
        close = _mock_proc(0, json.dumps({'id': 'cid', 'status': 'pending_new'}))
        result, audit_cur, _run = self._run_close(close, polls)
        self.assertEqual(self._audit_status(audit_cur), 'partial')
        # Partial counts as NOT FILLED for sentinel purposes. With only
        # one position in fixtures, zero fills → fire_succeeded=False →
        # action='failed' (correct: May-9 guard keeps the sentinel clear
        # so the operator can retry without being silently masked).
        self.assertEqual(result['action'], 'failed')

    def test_expired_outcome_recorded(self):
        """Submit succeeds + order expires with zero fills → 'rejected'."""
        polls = [
            _mock_proc(0, json.dumps({'id': 'cid', 'status': 'expired',
                                       'filled_qty': '0'})),
        ]
        close = _mock_proc(0, json.dumps({'id': 'cid', 'status': 'pending_new'}))
        result, audit_cur, _run = self._run_close(close, polls)
        self.assertEqual(self._audit_status(audit_cur), 'rejected')

    def test_pending_outcome_recorded(self):
        """Submit succeeds + poll never returns terminal (timeout) → 'pending'.
        We simulate timeout by mocking _poll_to_terminal directly with a
        non-terminal final order."""
        # Use the direct helper path: patch _poll_to_terminal to return a
        # non-terminal order so we don't actually loop for 90s.
        mock_conn, audit_cur = self._capture_audit_calls()
        proc_seq = [
            _mock_proc(0, json.dumps(CLOCK_OPEN)),
            _mock_proc(0, json.dumps([OPEN_OC_PARENT])),
            _mock_proc(0, json.dumps({'id': 'parent-uuid', 'status': 'cancelled'})),
            _mock_proc(0, json.dumps({'id': 'tp-leg', 'status': 'cancelled'})),
            _mock_proc(0, json.dumps({'id': 'sl-leg', 'status': 'cancelled'})),
            _mock_proc(0, json.dumps(POSITIONS_AAPL_LONG)),
            _mock_proc(0, json.dumps({'id': 'cid', 'status': 'pending_new'})),
        ]
        # Poll returns the original pending_new (non-terminal, no fills).
        non_terminal = {'id': 'cid', 'status': 'accepted', 'filled_qty': '0'}
        with patch.object(rl, '_load_regime', return_value=REGIME_CHANGED), \
             patch.object(rl, '_redis', return_value=_StubRedis()), \
             patch.object(rl, '_post_to_discord', return_value=True), \
             patch.object(rl, '_poll_to_terminal', return_value=non_terminal), \
             patch('execution.regime_liquidator.time.sleep'), \
             patch('psycopg2.connect', return_value=mock_conn), \
             patch('execution.regime_liquidator.subprocess.run',
                   side_effect=proc_seq):
            result = rl.liquidate_on_regime_change()
        self.assertEqual(self._audit_status(audit_cur), 'pending')
        # Same as the partial case: one position, zero fills → 'failed'
        # (sentinel guard correctly leaves the run retry-eligible).
        self.assertEqual(result['action'], 'failed')

    def test_submit_error_recorded(self):
        """CLI returns non-zero on submit → 'submit_error', no poll."""
        close_err = _mock_proc(1, '', json.dumps({'error': 'insufficient buying power'}))
        # No poll payloads — _poll_to_terminal should not be called when
        # submit failed.
        result, audit_cur, mock_run = self._run_close(close_err, poll_payloads=[])
        self.assertEqual(self._audit_status(audit_cur), 'submit_error')
        # Make sure `order get` (the poll) was never issued.
        for call in mock_run.call_args_list:
            argv = call[0][0]
            self.assertNotEqual(argv[1:3], ['order', 'get'],
                                'no poll should run when submit failed')


class TestSentinelStillGuardedByMay9Logic(unittest.TestCase):
    """The May-9 sentinel guard (`fire_succeeded = n_close_ok > 0 or
    len(close_results) == 0`) keeps its current semantics but
    `n_close_ok` now means terminal-FILLS, not submission-acks.

    A run where every close errors out (or partials/expires) must NOT
    seal the sentinel + cooldown — otherwise the operator's retry is
    silently masked, which was the May-9 incident."""

    def setUp(self):
        os.environ['OPENCLAW_ALPACA_LIVE_LIQUIDATE'] = '1'
        os.environ['POSTGRES_URI'] = 'postgres://stub/stub'

    def tearDown(self):
        os.environ.pop('OPENCLAW_ALPACA_LIVE_LIQUIDATE', None)

    def test_zero_filled_does_not_set_sentinel(self):
        """Every close terminal-rejects → no sentinel/cooldown."""
        stub = _StubRedis()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [SUBMISSION_ROW]
        mock_conn.cursor.return_value = mock_cur
        proc_seq = [
            _mock_proc(0, json.dumps(CLOCK_OPEN)),
            _mock_proc(0, json.dumps([OPEN_OC_PARENT])),
            _mock_proc(0, json.dumps({'id': 'parent-uuid', 'status': 'cancelled'})),
            _mock_proc(0, json.dumps({'id': 'tp-leg', 'status': 'cancelled'})),
            _mock_proc(0, json.dumps({'id': 'sl-leg', 'status': 'cancelled'})),
            _mock_proc(0, json.dumps(POSITIONS_AAPL_LONG)),
            _mock_proc(0, json.dumps({'id': 'cid', 'status': 'pending_new'})),
            _mock_proc(0, json.dumps({'id': 'cid', 'status': 'rejected',
                                       'filled_qty': '0'})),  # poll = rejected
        ]
        with patch.object(rl, '_load_regime', return_value=REGIME_CHANGED), \
             patch.object(rl, '_redis', return_value=stub), \
             patch.object(rl, '_post_to_discord', return_value=True), \
             patch('execution.regime_liquidator.time.sleep'), \
             patch('psycopg2.connect', return_value=mock_conn), \
             patch('execution.regime_liquidator.subprocess.run',
                   side_effect=proc_seq):
            result = rl.liquidate_on_regime_change()
        # Action is 'failed' because n_close_ok=0 and len(close_results)>0.
        self.assertEqual(result['action'], 'failed')
        self.assertNotIn('liquidate:fired:2026-05-07:TRANSITIONING->HIGH_VOL',
                         stub.store,
                         'sentinel must NOT seal on zero-fill run (May-9 guard)')
        self.assertNotIn('liquidate:cooldown:2026-05-07', stub.store,
                         'cooldown must NOT seal on zero-fill run (May-9 guard)')

    def test_one_filled_sets_sentinel(self):
        """One filled close (rest could be anything) → sentinel IS set.
        Only one OpenClaw symbol in fixtures, so a single filled run
        exercises the n_close_ok=1 > 0 path."""
        stub = _StubRedis()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [SUBMISSION_ROW]
        mock_conn.cursor.return_value = mock_cur
        proc_seq = [
            _mock_proc(0, json.dumps(CLOCK_OPEN)),
            _mock_proc(0, json.dumps([OPEN_OC_PARENT])),
            _mock_proc(0, json.dumps({'id': 'parent-uuid', 'status': 'cancelled'})),
            _mock_proc(0, json.dumps({'id': 'tp-leg', 'status': 'cancelled'})),
            _mock_proc(0, json.dumps({'id': 'sl-leg', 'status': 'cancelled'})),
            _mock_proc(0, json.dumps(POSITIONS_AAPL_LONG)),
            _mock_proc(0, json.dumps({'id': 'cid', 'status': 'pending_new'})),
            _mock_proc(0, json.dumps({'id': 'cid', 'status': 'filled',
                                       'filled_qty': '50'})),
        ]
        with patch.object(rl, '_load_regime', return_value=REGIME_CHANGED), \
             patch.object(rl, '_redis', return_value=stub), \
             patch.object(rl, '_post_to_discord', return_value=True), \
             patch('execution.regime_liquidator.time.sleep'), \
             patch('psycopg2.connect', return_value=mock_conn), \
             patch('execution.regime_liquidator.subprocess.run',
                   side_effect=proc_seq):
            result = rl.liquidate_on_regime_change()
        self.assertEqual(result['action'], 'liquidated')
        self.assertIn('liquidate:fired:2026-05-07:TRANSITIONING->HIGH_VOL',
                      stub.store,
                      'sentinel MUST seal when at least one close filled')
        self.assertIn('liquidate:cooldown:2026-05-07', stub.store)


if __name__ == '__main__':
    unittest.main()
