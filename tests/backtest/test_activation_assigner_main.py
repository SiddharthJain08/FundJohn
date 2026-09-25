"""tests/backtest/test_activation_assigner_main.py — main() gating for
src/backtest/activation_assigner.py (Task 2 brief, carried Task 1 review
item: "main() gating — no stamp / no tier-2 write on --dry-run, on
--strategy-id, or when n_errors > 0").

Kept in its own file (separate from test_activation_assigner_bench.py) so
it has its own ≤2-run budget -- main()'s CLI path had zero direct test
coverage before this file (Task 1 report, "Concerns / owed follow-ups").

Everything DB/network-shaped is mocked: `psycopg2.connect`, the dynamic
`execution.benchmark_sleeve.load_benchmark_sleeve_ids` import inside
main(), `load_bench_sharpe`, and `apply_one` itself (this file tests the
GATING around those calls, not their own internals -- those are covered
directly in test_activation_assigner_bench.py / test_activation_assigner.py).
No live DB access, no network, no real assigner invocation."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import activation_assigner as aa  # noqa: E402


class _FakeCursor:
    """Minimal cursor stub for the plumbing queries main() still runs
    directly (get_activation_threshold, get_activation_min_trades, the
    sids SELECT) -- returns None for fetchone() (both config reads fall
    back to their defaults) and one strategy id for fetchall()."""
    def execute(self, sql, params=()):
        pass

    def fetchone(self):
        return None

    def fetchall(self):
        return [('S_test',)]

    def close(self):
        pass


class _FakeConn:
    def __init__(self):
        self.cur = _FakeCursor()
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self, cursor_factory=None):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


_BENCH_VECTOR = {r: 1.0 for r in aa.CANONICAL_REGIMES}
_BENCH_META = {'sleeve_id': 'S_beta_spy', 'sleeve_source': 'registry', 'run_id': 'r1',
              'regime_source': {r: 'sleeve' for r in aa.CANONICAL_REGIMES}}
_OK_RESULT = {'status': 'ok', 'strategy_id': 'S_test',
             'prior': {r: None for r in aa.CANONICAL_REGIMES},
             'new': {r: False for r in aa.CANONICAL_REGIMES},
             'diag': {}, 'actions': {r: 'initialized' for r in aa.CANONICAL_REGIMES}}


class TestMainStampGating(unittest.TestCase):
    """The last-applied + tier-2 fail-safe stamps are only ever written on
    a --all, non-dry-run, zero-error run (main()'s existing gate, spec §2
    "re-apply trigger" / Task 1 report). This class locks that gate down
    with a positive control (so the negative assertions below aren't
    vacuously true against a broken wiring) plus the three negative cases
    the brief calls out by name."""

    def _run(self, argv, apply_side_effect=None):
        conn = _FakeConn()
        patches = [
            mock.patch.object(aa, 'psycopg2'),
            mock.patch('execution.benchmark_sleeve.load_benchmark_sleeve_ids', return_value=set()),
            mock.patch.object(aa, 'load_bench_sharpe', return_value=(_BENCH_VECTOR, _BENCH_META)),
            mock.patch.object(aa, 'apply_one'),
            mock.patch.object(aa, 'stamp_last_applied'),
            mock.patch.object(aa, 'stamp_bench_sharpe_config'),
            mock.patch.object(sys, 'argv', ['activation_assigner.py'] + argv),
            mock.patch.dict(os.environ, {'POSTGRES_URI': 'postgres://fake/fake'}),
        ]
        with patches[0] as mock_psycopg2, patches[1], patches[2], \
             patches[3] as mock_apply_one, patches[4] as mock_stamp_last, \
             patches[5] as mock_stamp_bench, patches[6], patches[7]:
            mock_psycopg2.connect.return_value = conn
            if apply_side_effect is not None:
                mock_apply_one.side_effect = apply_side_effect
            else:
                mock_apply_one.return_value = _OK_RESULT
            rc = aa.main()
        return rc, mock_stamp_last, mock_stamp_bench

    def test_all_dry_run_and_no_errors_stamps_both(self):
        # Positive control: proves the mocks above actually reach the
        # stamp gate when every condition is satisfied, so the negative
        # tests below aren't vacuously passing against dead wiring.
        rc, mock_stamp_last, mock_stamp_bench = self._run(['--all'])
        self.assertEqual(rc, 0)
        mock_stamp_last.assert_called_once()
        mock_stamp_bench.assert_called_once()

    def test_dry_run_never_stamps(self):
        rc, mock_stamp_last, mock_stamp_bench = self._run(['--all', '--dry-run'])
        self.assertEqual(rc, 0)
        mock_stamp_last.assert_not_called()
        mock_stamp_bench.assert_not_called()

    def test_strategy_id_run_never_stamps(self):
        rc, mock_stamp_last, mock_stamp_bench = self._run(['--strategy-id', 'S_test'])
        self.assertEqual(rc, 0)
        mock_stamp_last.assert_not_called()
        mock_stamp_bench.assert_not_called()

    def test_a_per_strategy_error_never_stamps(self):
        rc, mock_stamp_last, mock_stamp_bench = self._run(
            ['--all'], apply_side_effect=RuntimeError('boom'))
        self.assertEqual(rc, 1)   # n_errors > 0 -> non-zero exit
        mock_stamp_last.assert_not_called()
        mock_stamp_bench.assert_not_called()


if __name__ == '__main__':
    unittest.main()
