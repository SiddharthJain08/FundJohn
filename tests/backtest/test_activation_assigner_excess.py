"""tests/backtest/test_activation_assigner_excess.py — the EXCESS slider
(src/backtest/activation_assigner.py). Spec: docs/specs/2026-09-25-
activation-bench-relative-spec.md §8 (Amendment 1, operator-ruled
2026-09-27). Task 4 brief: .superpowers/sdd/2026-09-25-activation-bench-
relative/task-4-brief.md. Kept in its own file (own ≤2-run budget) per the
brief's advisor-reviewed guidance, mirroring test_activation_assigner_main.py's
own precedent of a dedicated file for a dedicated slice of this module.

DB layer fully mocked (FakeConn/FakeCursor below, mirrored verbatim from
test_activation_assigner_bench.py) -- no live DB access, no network, no
Postgres.
"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import activation_assigner as aa  # noqa: E402


# ── Fakes (mirrored from test_activation_assigner_bench.py) ─────────────────
class FakeCursor:
    def __init__(self, responses=()):
        self._responses = list(responses)
        self._current = None
        self.executed: list = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        nxt = self._responses.pop(0) if self._responses else None
        if callable(nxt):
            raise nxt()
        self._current = nxt

    def fetchone(self):
        if isinstance(self._current, list):
            return self._current[0] if self._current else None
        return self._current

    def fetchall(self):
        return list(self._current or [])

    def __iter__(self):
        return iter(self._current or [])

    def close(self):
        pass


class FakeConn:
    def __init__(self, responses=()):
        self.cur = FakeCursor(responses)
        self.committed = False
        self.rolled_back = False

    def cursor(self, cursor_factory=None):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass

    @property
    def executed(self):
        return self.cur.executed


def _regime_row(regime, sharpe, trade_count, max_dd_pct=10.0, calmar=None):
    return {'regime_state': regime, 'sharpe': sharpe, 'trade_count': trade_count,
            'max_dd_pct': max_dd_pct, 'calmar': calmar}


def _all_none_prior(override_regime=None, override_val=None):
    p = {r: None for r in aa.CANONICAL_REGIMES}
    if override_regime is not None:
        p[override_regime] = override_val
    return p


def _captured_stdout(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*a, **k)
    return result, buf.getvalue()


# ── get_activation_excess: missing/malformed/non-finite -> 0.0 + WARN ──────
class TestGetActivationExcess(unittest.TestCase):
    def test_missing_row_defaults_to_zero_with_warn(self):
        cur = FakeCursor([None])
        result, out = _captured_stdout(aa.get_activation_excess, cur)
        self.assertEqual(result, 0.0)
        self.assertIn('WARN:', out)
        self.assertIn(aa.EXCESS_KEY, out)

    def test_null_value_defaults_to_zero_with_warn(self):
        cur = FakeCursor([(None,)])
        result, out = _captured_stdout(aa.get_activation_excess, cur)
        self.assertEqual(result, 0.0)
        self.assertIn('WARN:', out)

    def test_malformed_value_defaults_to_zero_with_warn(self):
        cur = FakeCursor([('not-a-number',)])
        result, out = _captured_stdout(aa.get_activation_excess, cur)
        self.assertEqual(result, 0.0)
        self.assertIn('WARN:', out)

    def test_non_finite_value_defaults_to_zero_with_warn(self):
        cur = FakeCursor([('nan',)])
        result, out = _captured_stdout(aa.get_activation_excess, cur)
        self.assertEqual(result, 0.0)
        self.assertIn('WARN:', out)

    def test_infinite_value_defaults_to_zero_with_warn(self):
        cur = FakeCursor([('inf',)])
        result, out = _captured_stdout(aa.get_activation_excess, cur)
        self.assertEqual(result, 0.0)
        self.assertIn('WARN:', out)

    def test_query_failure_defaults_to_zero_with_warn(self):
        cur = FakeCursor([RuntimeError])
        result, out = _captured_stdout(aa.get_activation_excess, cur)
        self.assertEqual(result, 0.0)
        self.assertIn('WARN:', out)

    def test_valid_value_is_returned_without_warn(self):
        cur = FakeCursor([('0.30',)])
        result, out = _captured_stdout(aa.get_activation_excess, cur)
        self.assertEqual(result, 0.3)
        self.assertNotIn('WARN:', out)

    def test_negative_value_is_returned_without_warn(self):
        # Negative excess (looser than bench) is explicitly allowed by the
        # spec -- only the dashboard clamps to [-1.0, 2.0].
        cur = FakeCursor([('-0.5',)])
        result, out = _captured_stdout(aa.get_activation_excess, cur)
        self.assertEqual(result, -0.5)
        self.assertNotIn('WARN:', out)


# ── Pin: excess=0.0 reproduces the bench-only rule byte-for-byte ───────────
class TestExcessZeroIsByteIdenticalToBenchOnly(unittest.TestCase):
    """Golden values captured from the UNMODIFIED (pre-Task-4) _judge over a
    boundary grid (activate edge, band floor edge, prior True/False/None,
    Decimal sharpe, a class-gate failure inside the band) -- see
    task-4-report.md for the capture transcript. Every scenario below must
    reproduce the identical eligible/bench/band_floor/band_applied values
    whether `excess` is omitted entirely or passed explicitly as 0.0."""

    BENCH = {'LOW_VOL': 0.53, 'TRANSITIONING': 0.53, 'HIGH_VOL': 0.53, 'CRISIS': 0.53}

    def setUp(self):
        from backtest.regime_qualification import class_thresholds
        self.gate = class_thresholds('equity')

    def _check(self, rows, prior, expected):
        for kwargs in ({}, {'excess': 0.0}):
            elig, diag = aa._judge(rows, self.gate, 100, self.BENCH, prior, False, **kwargs)
            d = diag['LOW_VOL']
            self.assertEqual(elig['LOW_VOL'], expected['eligible'], kwargs)
            self.assertEqual(d['bench'], expected['bench'], kwargs)
            self.assertEqual(d['threshold'], expected['bench'], kwargs)   # thr == bench at excess=0.0
            self.assertEqual(d['excess'], 0.0, kwargs)
            self.assertEqual(d['band_floor'], expected['band_floor'], kwargs)
            self.assertEqual(d['band_applied'], expected['band_applied'], kwargs)
            self.assertEqual(d['rule'], 'qualifies(>0·classDD·trades)+bench_relative+excess', kwargs)

    def test_activate_edge_prior_none_at_bench_exactly(self):
        self._check([_regime_row('LOW_VOL', 0.53, 150)], _all_none_prior(),
                    {'eligible': True, 'bench': 0.53, 'band_floor': 0.43, 'band_applied': False})

    def test_activate_edge_prior_none_just_below_bench(self):
        self._check([_regime_row('LOW_VOL', 0.529999999999, 150)], _all_none_prior(),
                    {'eligible': False, 'bench': 0.53, 'band_floor': 0.43, 'band_applied': False})

    def test_band_floor_edge_prior_true_at_band_floor_exactly(self):
        self._check([_regime_row('LOW_VOL', 0.43, 150)], _all_none_prior('LOW_VOL', True),
                    {'eligible': True, 'bench': 0.53, 'band_floor': 0.43, 'band_applied': True})

    def test_band_floor_edge_prior_true_just_below_band_floor(self):
        self._check([_regime_row('LOW_VOL', 0.429999999999, 150)], _all_none_prior('LOW_VOL', True),
                    {'eligible': False, 'bench': 0.53, 'band_floor': 0.43, 'band_applied': False})

    def test_prior_false_explicit_at_bench_exactly_uses_strict_edge(self):
        self._check([_regime_row('LOW_VOL', 0.53, 150)], _all_none_prior('LOW_VOL', False),
                    {'eligible': True, 'bench': 0.53, 'band_floor': 0.43, 'band_applied': False})

    def test_decimal_sharpe_exactly_at_bench(self):
        from decimal import Decimal
        self._check([_regime_row('LOW_VOL', Decimal('0.53'), 150)], _all_none_prior(),
                    {'eligible': True, 'bench': 0.53, 'band_floor': 0.43, 'band_applied': False})

    def test_class_gate_failure_inside_band_overrides_prior_true(self):
        self._check([_regime_row('LOW_VOL', 0.50, 50)], _all_none_prior('LOW_VOL', True),
                    {'eligible': False, 'bench': 0.53, 'band_floor': 0.43, 'band_applied': False})

    def test_compute_eligible_omitted_and_explicit_zero_agree(self):
        # End-to-end through compute_eligible (not just _judge directly),
        # confirming the same invariant at the public API surface Task 4's
        # brief names explicitly ("excess = 0.0 must reproduce the current
        # decisions byte-for-byte").
        bench = self.BENCH
        rows = [_regime_row('LOW_VOL', 0.53, 150)]
        conn1 = FakeConn(responses=[{'run_id': 'r1'}, [], rows])
        elig1, diag1 = aa.compute_eligible(conn1, 'S_test', bench=bench)
        conn2 = FakeConn(responses=[{'run_id': 'r1'}, [], rows])
        elig2, diag2 = aa.compute_eligible(conn2, 'S_test', bench=bench, excess=0.0)
        self.assertEqual(elig1, elig2)
        self.assertEqual(diag1['LOW_VOL']['threshold'], diag2['LOW_VOL']['threshold'])
        self.assertEqual(diag1, diag2)


# ── excess shifts the activate/deactivate edges ─────────────────────────────
class TestExcessShiftsTheThreshold(unittest.TestCase):
    def setUp(self):
        from backtest.regime_qualification import class_thresholds
        self.gate = class_thresholds('equity')
        self.bench = {'LOW_VOL': 1.0, 'TRANSITIONING': 1.0, 'HIGH_VOL': 1.0, 'CRISIS': 1.0}

    def test_excess_refuses_activation_below_bench_plus_excess(self):
        # sharpe (1.2) is >= the OLD bench-only threshold (1.0) but BELOW
        # bench + excess (1.3) -- must NOT activate under excess=0.3.
        rows = [_regime_row('LOW_VOL', 1.2, 150)]
        elig, diag = aa._judge(rows, self.gate, 100, self.bench, _all_none_prior(), False, excess=0.3)
        self.assertFalse(elig['LOW_VOL'])
        self.assertEqual(diag['LOW_VOL']['threshold'], 1.3)
        self.assertEqual(diag['LOW_VOL']['excess'], 0.3)

    def test_excess_activates_at_bench_plus_excess_exactly(self):
        rows = [_regime_row('LOW_VOL', 1.3, 150)]
        elig, diag = aa._judge(rows, self.gate, 100, self.bench, _all_none_prior(), False, excess=0.3)
        self.assertTrue(elig['LOW_VOL'])
        self.assertEqual(diag['LOW_VOL']['threshold'], 1.3)

    def test_excess_deactivates_a_prior_true_cell_only_below_threshold_minus_hysteresis(self):
        # threshold = bench + excess = 1.3; band floor = 1.3 - 0.10 = 1.2.
        prior = _all_none_prior('LOW_VOL', True)
        # Inside the loosened band (1.2 <= s < 1.3): stays eligible even
        # though s is well below the OLD bench-only band floor (0.90).
        rows_inside = [_regime_row('LOW_VOL', 1.25, 150)]
        elig1, diag1 = aa._judge(rows_inside, self.gate, 100, self.bench, prior, False, excess=0.3)
        self.assertTrue(elig1['LOW_VOL'])
        self.assertTrue(diag1['LOW_VOL']['band_applied'])
        self.assertEqual(diag1['LOW_VOL']['band_floor'], 1.2)
        # Below the loosened band floor (1.2): deactivates, even though it
        # is still >= the OLD bench-only threshold (1.0).
        rows_below = [_regime_row('LOW_VOL', 1.15, 150)]
        elig2, diag2 = aa._judge(rows_below, self.gate, 100, self.bench, prior, False, excess=0.3)
        self.assertFalse(elig2['LOW_VOL'])

    def test_negative_excess_loosens_the_threshold(self):
        # excess=-0.2 -> threshold = 0.8, below the raw bench (1.0):
        # sharpe=0.9 would have failed the bench-only rule but now clears
        # the (looser) excess-adjusted threshold.
        rows = [_regime_row('LOW_VOL', 0.9, 150)]
        elig, diag = aa._judge(rows, self.gate, 100, self.bench, _all_none_prior(), False, excess=-0.2)
        self.assertTrue(elig['LOW_VOL'])
        self.assertEqual(diag['LOW_VOL']['threshold'], 0.8)

    def test_apply_one_end_to_end_excess_shift(self):
        # Same shift, through the real write path (apply_one), asserting
        # the audit reason records bench/excess/threshold separately.
        rows = [_regime_row('LOW_VOL', 1.2, 150)]
        prior_rows = [('LOW_VOL', False, None, None, None, None)]
        conn = FakeConn(responses=[{'run_id': 'r1'}, [], rows, prior_rows])
        result = aa.apply_one(conn, 'S_test', dry_run=False, bench=self.bench, excess=0.3)
        self.assertEqual(result['actions']['LOW_VOL'], 'unchanged')   # False->False, no write
        self.assertFalse(result['new']['LOW_VOL'])

        rows2 = [_regime_row('LOW_VOL', 1.3, 150)]
        conn2 = FakeConn(responses=[{'run_id': 'r1'}, [], rows2, prior_rows])
        result2 = aa.apply_one(conn2, 'S_test', dry_run=False, bench=self.bench, excess=0.3)
        self.assertEqual(result2['actions']['LOW_VOL'], 'activated')
        inserts = [p for sql, p in conn2.executed if 'strategy_regime_param_changes' in sql]
        self.assertTrue(any('bench=1.0' in str(p) and 'excess=0.3' in str(p) and 'threshold=1.3' in str(p)
                            for p in inserts))
        self.assertTrue(any('rule=qualifies(>0·classDD·trades)+bench_relative+excess' in str(p)
                            for p in inserts))


# ── stamp_last_applied gains excess ─────────────────────────────────────────
class TestStampGainsExcess(unittest.TestCase):
    def test_stamp_persists_excess(self):
        import json
        conn = FakeConn([None])
        ok = aa.stamp_last_applied(conn, 100, 3, 7, trigger='daily_cycle', excess=0.3)
        self.assertTrue(ok)
        sql, params = conn.executed[-1]
        payload = json.loads(params[1])
        self.assertEqual(payload['excess'], 0.3)

    def test_stamp_excess_defaults_to_none_when_omitted(self):
        # Back-compat: an older direct caller that doesn't pass excess still
        # stamps cleanly, with an explicit null (not an omitted key) -- see
        # docstring for why this mirrors bench_regime_source's convention.
        import json
        conn = FakeConn([None])
        ok = aa.stamp_last_applied(conn, 100, 1, 1, trigger='manual')
        self.assertTrue(ok)
        sql, params = conn.executed[-1]
        payload = json.loads(params[1])
        self.assertIn('excess', payload)
        self.assertIsNone(payload['excess'])


if __name__ == '__main__':
    unittest.main()
