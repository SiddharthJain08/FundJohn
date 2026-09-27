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

import contextlib
import io
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
    directly (get_activation_min_trades, the sids SELECT) -- returns None
    for fetchone() (the config read falls back to its default) and one
    strategy id for fetchall()."""
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


class TestMainNotifyAndBenchRegimeSource(unittest.TestCase):
    """F1-b/F2 (fix round 1): main()'s notify call must (a) relabel the
    Discord-bound summary's `threshold=` token to `bench_low_vol=` WITHOUT
    touching the byte-pinned stdout summary/header lines
    (activation_preview.js HEADER_RE/SUMMARY_RE), and (b) pass the bench:
    line, the bench diff: line, and every WARN: line _log collected this
    run to _notify_botjohn_log. F1-c: main() must also thread
    bench_meta['regime_source'] through to stamp_last_applied as
    bench_regime_source."""

    def _run(self, argv, bench_meta=None, warn_before_bench=None):
        conn = _FakeConn()
        meta = bench_meta if bench_meta is not None else _BENCH_META

        def _load_bench_sharpe(*_a, **_kw):
            # Emulates a WARN _log'd from inside load_bench_sharpe (e.g. a
            # tier-2/tier-3 fallback) so the notify-wiring test below has a
            # real entry in aa._WARN_LINES to assert on, without touching
            # load_bench_sharpe's own internals (mocked out entirely here,
            # covered directly in test_activation_assigner_bench.py).
            if warn_before_bench:
                aa._log(warn_before_bench)
            return (_BENCH_VECTOR, meta)

        patches = [
            mock.patch.object(aa, 'psycopg2'),
            mock.patch('execution.benchmark_sleeve.load_benchmark_sleeve_ids', return_value=set()),
            mock.patch.object(aa, 'load_bench_sharpe', side_effect=_load_bench_sharpe),
            mock.patch.object(aa, 'apply_one', return_value=_OK_RESULT),
            mock.patch.object(aa, 'stamp_last_applied'),
            mock.patch.object(aa, 'stamp_bench_sharpe_config'),
            mock.patch.object(aa, '_notify_botjohn_log'),
            mock.patch.object(sys, 'argv', ['activation_assigner.py'] + argv),
            mock.patch.dict(os.environ, {'POSTGRES_URI': 'postgres://fake/fake'}),
        ]
        with patches[0] as mock_psycopg2, patches[1], patches[2], patches[3], \
             patches[4] as mock_stamp_last, patches[5], patches[6] as mock_notify, \
             patches[7], patches[8]:
            mock_psycopg2.connect.return_value = conn
            rc = aa.main()
        return rc, mock_stamp_last, mock_notify

    def test_notify_gets_relabeled_summary_bench_lines_and_warns(self):
        rc, _mock_stamp_last, mock_notify = self._run(
            ['--all', '--dry-run', '--notify'],
            warn_before_bench='WARN: multiple benchmark sleeves in the registry [...]')
        self.assertEqual(rc, 0)
        mock_notify.assert_called_once()
        args, kwargs = mock_notify.call_args
        notify_summary = args[0]
        self.assertIn('bench_low_vol=', notify_summary)
        self.assertNotIn('threshold=', notify_summary)
        self.assertIn('bench:', kwargs['bench_line'])
        self.assertIn('bench diff:', kwargs['bench_diff_line'])
        self.assertTrue(any('multiple benchmark sleeves' in w for w in kwargs['warn_lines']))

    def test_stdout_summary_stays_byte_pinned_with_threshold(self):
        # The stdout copy (SUMMARY_RE, dashboard-parsed) must be completely
        # unaffected by the F2 Discord-only relabel above.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc, _mock_stamp_last, _mock_notify = self._run(['--all', '--dry-run', '--notify'])
        self.assertEqual(rc, 0)
        summary_lines = [l for l in buf.getvalue().splitlines()
                         if 'activation_assigner summary:' in l]
        self.assertEqual(len(summary_lines), 1)
        self.assertRegex(summary_lines[0], r'threshold=[-+\d.eE]+, min_trades=\d+, dry_run=\w+, errors=\d+')
        self.assertNotIn('bench_low_vol=', summary_lines[0])

    def test_no_notify_flag_never_calls_notify(self):
        rc, _mock_stamp_last, mock_notify = self._run(['--all', '--dry-run'])
        self.assertEqual(rc, 0)
        mock_notify.assert_not_called()

    def test_stamp_last_applied_receives_bench_regime_source(self):
        rc, mock_stamp_last, _mock_notify = self._run(['--all'])
        self.assertEqual(rc, 0)
        mock_stamp_last.assert_called_once()
        _args, kwargs = mock_stamp_last.call_args
        self.assertEqual(kwargs['bench_regime_source'], _BENCH_META['regime_source'])


class TestMainExcessCliOverride(unittest.TestCase):
    """Amendment 1 §8 (spec docs/specs/2026-09-25-activation-bench-relative-
    spec.md, Task 4 brief): --excess is a DRY-RUN-ONLY preview override,
    refused on any non-dry-run invocation (rc=1, before any DB connect --
    the persisted pipeline_config row is the single source of truth for a
    live apply), rejected outright if non-finite, and threaded into every
    apply_one call on a dry-run. _FakeCursor.fetchone() always returns None,
    so an OMITTED --excess resolves via get_activation_excess's own
    missing-row fail-safe (0.0), not the CLI override -- covered here too
    since it's the same resolution site."""

    def _run(self, argv):
        conn = _FakeConn()
        patches = [
            mock.patch.object(aa, 'psycopg2'),
            mock.patch('execution.benchmark_sleeve.load_benchmark_sleeve_ids', return_value=set()),
            mock.patch.object(aa, 'load_bench_sharpe', return_value=(_BENCH_VECTOR, _BENCH_META)),
            mock.patch.object(aa, 'apply_one', return_value=_OK_RESULT),
            mock.patch.object(aa, 'stamp_last_applied'),
            mock.patch.object(aa, 'stamp_bench_sharpe_config'),
            mock.patch.object(sys, 'argv', ['activation_assigner.py'] + argv),
            mock.patch.dict(os.environ, {'POSTGRES_URI': 'postgres://fake/fake'}),
        ]
        with patches[0] as mock_psycopg2, patches[1], patches[2], \
             patches[3] as mock_apply_one, patches[4] as mock_stamp_last, \
             patches[5] as mock_stamp_bench, patches[6], patches[7]:
            mock_psycopg2.connect.return_value = conn
            rc = aa.main()
        return rc, mock_psycopg2, mock_apply_one, mock_stamp_last, mock_stamp_bench

    def test_excess_with_all_non_dry_run_is_refused_before_any_db_connect(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc, mock_psycopg2, mock_apply_one, _stamp_last, _stamp_bench = self._run(
                ['--all', '--excess', '0.3'])
        self.assertEqual(rc, 1)
        mock_psycopg2.connect.assert_not_called()
        mock_apply_one.assert_not_called()
        self.assertIn('--excess', buf.getvalue())

    def test_excess_with_strategy_id_non_dry_run_is_also_refused(self):
        # The brief's D-clause names only "--excess with --all non-dry-run
        # refused"; this locks in the broader (advisor-reviewed) reading --
        # "--excess is dry-run only" applies regardless of --all/
        # --strategy-id, so a --strategy-id live apply can't write
        # eligibility under an excess that was never persisted either.
        rc, mock_psycopg2, mock_apply_one, _stamp_last, _stamp_bench = self._run(
            ['--strategy-id', 'S_test', '--excess', '0.3'])
        self.assertEqual(rc, 1)
        mock_psycopg2.connect.assert_not_called()
        mock_apply_one.assert_not_called()

    def test_non_finite_excess_is_rejected_even_on_dry_run(self):
        rc, mock_psycopg2, mock_apply_one, _stamp_last, _stamp_bench = self._run(
            ['--all', '--dry-run', '--excess', 'nan'])
        self.assertEqual(rc, 1)
        mock_psycopg2.connect.assert_not_called()
        mock_apply_one.assert_not_called()

    def test_excess_override_is_allowed_and_threaded_on_all_dry_run(self):
        rc, _psycopg2, mock_apply_one, mock_stamp_last, mock_stamp_bench = self._run(
            ['--all', '--dry-run', '--excess', '0.3'])
        self.assertEqual(rc, 0)
        mock_apply_one.assert_called_once()
        self.assertEqual(mock_apply_one.call_args.kwargs['excess'], 0.3)
        mock_stamp_last.assert_not_called()   # dry-run never stamps
        mock_stamp_bench.assert_not_called()

    def test_excess_override_is_allowed_and_threaded_on_strategy_id_dry_run(self):
        rc, _psycopg2, mock_apply_one, _stamp_last, _stamp_bench = self._run(
            ['--strategy-id', 'S_test', '--dry-run', '--excess', '-0.2'])
        self.assertEqual(rc, 0)
        mock_apply_one.assert_called_once()
        self.assertEqual(mock_apply_one.call_args.kwargs['excess'], -0.2)

    def test_omitted_excess_resolves_from_pipeline_config_and_is_threaded_and_stamped(self):
        rc, _psycopg2, mock_apply_one, mock_stamp_last, _stamp_bench = self._run(['--all'])
        self.assertEqual(rc, 0)
        self.assertEqual(mock_apply_one.call_args.kwargs['excess'], 0.0)
        mock_stamp_last.assert_called_once()
        self.assertEqual(mock_stamp_last.call_args.kwargs['excess'], 0.0)


if __name__ == '__main__':
    unittest.main()
