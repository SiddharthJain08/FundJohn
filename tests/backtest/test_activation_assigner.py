"""tests/test_activation_assigner.py — Strategy Activation Slider backend
(src/backtest/activation_assigner.py). DB layer fully mocked (FakeConn/
FakeCursor below) — no live DB access, no network. Mirrors the fixture
style of tests/test_eligibility_manager_db.py (upsert-pattern assertions)
and tests/test_bt_sharpe_clamp.py (pipeline_config fail-safe assertions)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import activation_assigner as aa  # noqa: E402


# ── Fakes ────────────────────────────────────────────────────────────────────
class FakeCursor:
    """Cursor stub. `responses` is an ordered list, one entry consumed per
    execute() call: a dict -> fetchone() source; a list -> fetchall()/
    iteration source; anything else (e.g. None) -> unused placeholder for
    write-only statements (INSERT) that are never fetched. Once exhausted,
    further execute() calls are no-ops (covers trailing INSERTs)."""
    def __init__(self, responses=()):
        self._responses = list(responses)
        self._current = None
        self.executed: list = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        self._current = self._responses.pop(0) if self._responses else None

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


def _regime_row(regime, sharpe, trade_count, max_dd_pct=10.0):
    return {'regime_state': regime, 'sharpe': sharpe, 'trade_count': trade_count,
            'max_dd_pct': max_dd_pct}


def _prior_row(regime, eligible, size_scalar=None, stop_pct=None, target_pct=None, max_hold_days=None):
    return (regime, eligible, size_scalar, stop_pct, target_pct, max_hold_days)


# ── Config accessor (mirrors TestGetCap in test_bt_sharpe_clamp.py) ─────────
class FakeCur0:
    """Minimal cursor stub for get_activation_threshold, decoupled from the
    heavier FakeCursor above (matches test_bt_sharpe_clamp.py's FakeCur)."""
    def __init__(self, row=None, raise_on_execute=False):
        self._row = row
        self._raise = raise_on_execute

    def execute(self, *a, **k):
        if self._raise:
            raise RuntimeError('boom')

    def fetchone(self):
        return self._row


class TestGetActivationThreshold(unittest.TestCase):
    def test_present_value_parsed(self):
        self.assertEqual(aa.get_activation_threshold(FakeCur0(row=('0.75',))), 0.75)

    def test_absent_key_defaults_half(self):
        self.assertEqual(aa.get_activation_threshold(FakeCur0(row=None)), 0.5)

    def test_malformed_value_defaults_half(self):
        self.assertEqual(aa.get_activation_threshold(FakeCur0(row=('abc',))), 0.5)

    def test_query_error_defaults_half(self):
        self.assertEqual(aa.get_activation_threshold(FakeCur0(raise_on_execute=True)), 0.5)


class TestGetActivationMinTrades(unittest.TestCase):
    """min_trades is a dashboard-adjustable activation parameter (2026-07-16).

    It was enforced at a fixed 100 from regime_qualification.class_thresholds with
    no config key, so the sample-size floor could not be tuned without a code
    change — unlike its sibling min-Sharpe slider. Same fail-safe contract:
    missing / malformed / query error → None, which makes compute_eligible fall
    back to the per-class gate rather than silently loosening the floor to 0.
    """
    def test_present_value_parsed(self):
        self.assertEqual(aa.get_activation_min_trades(FakeCur0(row=('250',))), 250)

    def test_zero_is_honoured_not_treated_as_absent(self):
        # 0 = "no sample-size floor" and is a legitimate operator choice; it must
        # not be confused with an unset key (which defers to the class gate).
        self.assertEqual(aa.get_activation_min_trades(FakeCur0(row=('0',))), 0)

    def test_absent_key_defers_to_class_gate(self):
        self.assertIsNone(aa.get_activation_min_trades(FakeCur0(row=None)))

    def test_malformed_value_defers_to_class_gate(self):
        self.assertIsNone(aa.get_activation_min_trades(FakeCur0(row=('abc',))))

    def test_negative_value_defers_to_class_gate(self):
        # A negative floor would activate everything — never honour it.
        self.assertIsNone(aa.get_activation_min_trades(FakeCur0(row=('-5',))))

    def test_query_error_defers_to_class_gate(self):
        self.assertIsNone(aa.get_activation_min_trades(FakeCur0(raise_on_execute=True)))


# ── compute_eligible ─────────────────────────────────────────────────────────
class TestComputeEligible(unittest.TestCase):
    def test_no_primary_window_run_returns_none(self):
        conn = FakeConn(responses=[None])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.5)
        self.assertIsNone(eligible)
        self.assertEqual(diag, {})
        # Only the run_id lookup should have executed -- caller must skip,
        # never touch strategy_regime_params for a strategy with no run.
        self.assertEqual(len(conn.executed), 1)

    def test_run_with_zero_regime_rows_returns_none(self):
        """A primary_window row can exist with no strategy_backtest_regimes
        children (malformed/partial write) -- treated the same as no run.
        (3 executes since W3: run_id, empty shrink probe, empty fallback.)"""
        conn = FakeConn(responses=[{'run_id': 'r1'}, [], []])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.5)
        self.assertIsNone(eligible)
        self.assertEqual(diag, {})
        self.assertEqual(len(conn.executed), 3)

    def test_chosen_shrink_sleeves_take_precedence(self):
        """W3 (universe ladder campaign): when universe_shrink_metrics has
        chosen rows for the primary run, they are the eligibility source and
        strategy_backtest_regimes is never queried."""
        shrink = [_regime_row('LOW_VOL', 2.0, 150)]
        poison = [_regime_row('LOW_VOL', -5.0, 1)]   # must NOT be consumed
        conn = FakeConn(responses=[{'run_id': 'r1'}, shrink, poison])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.5)
        self.assertTrue(eligible['LOW_VOL'])
        self.assertEqual(len(conn.executed), 2)      # no fallback query
        self.assertIn('universe_shrink_metrics', conn.executed[1][0])

    def test_sharpe_threshold_boundary(self):
        rows = [_regime_row('LOW_VOL', 0.5, 100), _regime_row('CRISIS', 0.4999, 100)]
        conn = FakeConn(responses=[{'run_id': 'r1'}, [],rows])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.5)
        self.assertTrue(eligible['LOW_VOL'])     # exactly at threshold -> eligible (>=)
        self.assertFalse(eligible['CRISIS'])     # just below -> not eligible

    def test_slider_zero_requires_strictly_positive_sharpe(self):
        # 2026-07-13 v2: slider at 0 activates exactly the QUALIFYING regimes
        # (sharpe strictly > 0) -- a 0.0 sleeve stays dormant.
        rows = [_regime_row('LOW_VOL', 0.0, 100), _regime_row('CRISIS', 0.01, 100)]
        conn = FakeConn(responses=[{'run_id': 'r1'}, [],rows])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.0)
        self.assertFalse(eligible['LOW_VOL'])
        self.assertTrue(eligible['CRISIS'])

    def test_trade_count_guard_boundary(self):
        rows = [_regime_row('LOW_VOL', 1.0, 20), _regime_row('HIGH_VOL', 1.0, 19)]
        conn = FakeConn(responses=[{'run_id': 'r1'}, [],rows])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.5, min_trades=20)
        self.assertTrue(eligible['LOW_VOL'])     # n==20 -> passes (>=, explicit override)
        self.assertFalse(eligible['HIGH_VOL'])   # n==19 -> fails

    def test_trade_count_gate_default_100(self):
        # Default trade floor comes from the shared per-regime gate (100).
        rows = [_regime_row('LOW_VOL', 1.0, 100), _regime_row('HIGH_VOL', 1.0, 99)]
        conn = FakeConn(responses=[{'run_id': 'r1'}, [],rows])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.5)
        self.assertTrue(eligible['LOW_VOL'])
        self.assertFalse(eligible['HIGH_VOL'])

    def test_sleeve_dd_ceiling_class_aware(self):
        # Per-regime sleeve DD gates: equity ceiling 20, option 30.
        rows = [_regime_row('LOW_VOL', 1.0, 100, max_dd_pct=25.0),
                _regime_row('CRISIS', 1.0, 100, max_dd_pct=15.0)]
        conn = FakeConn(responses=[{'run_id': 'r1'}, [],rows])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.5)
        self.assertFalse(eligible['LOW_VOL'])    # dd 25 > equity 20
        self.assertTrue(eligible['CRISIS'])
        conn = FakeConn(responses=[{'run_id': 'r1'}, [],[_regime_row('LOW_VOL', 1.0, 100, max_dd_pct=25.0)]])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.5,
                                             instrument_class='option')
        self.assertTrue(eligible['LOW_VOL'])     # dd 25 <= option 30

    def test_null_dd_fails_closed(self):
        rows = [_regime_row('LOW_VOL', 1.0, 100, max_dd_pct=None)]
        conn = FakeConn(responses=[{'run_id': 'r1'}, [],rows])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.5)
        self.assertFalse(eligible['LOW_VOL'])

    def test_null_sharpe_excludes(self):
        rows = [_regime_row('LOW_VOL', None, 200)]
        conn = FakeConn(responses=[{'run_id': 'r1'}, [],rows])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.5)
        self.assertFalse(eligible['LOW_VOL'])

    def test_regime_absent_from_backtest_defaults_false(self):
        """A regime never seen in strategy_backtest_regimes for this run
        (e.g. CRISIS never occurred in the OOS window) is still returned in
        the eligible dict, explicitly False -- all 4 regimes always
        determined."""
        rows = [_regime_row('LOW_VOL', 2.0, 100)]
        conn = FakeConn(responses=[{'run_id': 'r1'}, [],rows])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.5)
        self.assertEqual(set(eligible.keys()), set(aa.CANONICAL_REGIMES))
        self.assertTrue(eligible['LOW_VOL'])
        self.assertFalse(eligible['TRANSITIONING'])
        self.assertFalse(eligible['HIGH_VOL'])
        self.assertFalse(eligible['CRISIS'])
        self.assertNotIn('TRANSITIONING', diag)  # diag only for observed regimes

    def test_one_qualifying_regime_others_false(self):
        rows = [
            _regime_row('LOW_VOL', 1.2, 100),         # passes
            _regime_row('TRANSITIONING', 0.1, 100),   # fails sharpe
            _regime_row('HIGH_VOL', 2.0, 5),          # fails trades
            _regime_row('CRISIS', -0.5, 50),          # fails sharpe (negative)
        ]
        conn = FakeConn(responses=[{'run_id': 'r1'}, [],rows])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.5)
        self.assertEqual(eligible, {'LOW_VOL': True, 'TRANSITIONING': False,
                                    'HIGH_VOL': False, 'CRISIS': False})

    def test_zero_qualifying_regimes_all_false(self):
        rows = [
            _regime_row('LOW_VOL', 0.1, 100),
            _regime_row('TRANSITIONING', -1.0, 100),
            _regime_row('HIGH_VOL', 0.3, 5),
            _regime_row('CRISIS', None, 50),
        ]
        conn = FakeConn(responses=[{'run_id': 'r1'}, [],rows])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.5)
        self.assertEqual(eligible, {r: False for r in aa.CANONICAL_REGIMES})


# ── _classify_action ─────────────────────────────────────────────────────────
class TestClassifyAction(unittest.TestCase):
    def test_unchanged_only_when_prior_row_exists_and_matches(self):
        self.assertEqual(aa._classify_action(True, True), 'unchanged')
        self.assertEqual(aa._classify_action(False, False), 'unchanged')

    def test_true_to_false_is_deactivated(self):
        self.assertEqual(aa._classify_action(True, False), 'deactivated')

    def test_false_or_none_to_true_is_activated(self):
        self.assertEqual(aa._classify_action(False, True), 'activated')
        self.assertEqual(aa._classify_action(None, True), 'activated')

    def test_none_to_false_is_initialized_not_unchanged(self):
        """No prior row + computed False must still be an explicit write
        (fully-determined eligibility), never silently 'unchanged'."""
        self.assertEqual(aa._classify_action(None, False), 'initialized')


# ── apply_one: end-to-end (dry-run and real-write paths) ────────────────────
class TestApplyOneDryRun(unittest.TestCase):
    def test_dry_run_makes_no_writes(self):
        regime_rows = [_regime_row('LOW_VOL', 1.0, 100)]
        prior_rows = [_prior_row('LOW_VOL', True)]
        conn = FakeConn(responses=[{'run_id': 'r1'}, [],regime_rows, prior_rows])
        result = aa.apply_one(conn, 'S_test', threshold=0.5, dry_run=True)
        self.assertEqual(result['status'], 'ok')
        # Exactly 4 reads (run_id, shrink probe, regime rows, prior rows) --
        # no INSERT/UPDATE.
        self.assertEqual(len(conn.executed), 4)
        for sql, _ in conn.executed:
            self.assertNotIn('INSERT', sql.upper())
        self.assertFalse(conn.committed)

    def test_skipped_strategy_is_fully_untouched(self):
        conn = FakeConn(responses=[None])
        result = aa.apply_one(conn, 'S_test', threshold=0.5, dry_run=False)
        self.assertEqual(result['status'], 'skipped_no_run')
        self.assertEqual(result['prior'], {})
        self.assertEqual(result['new'], {})
        # Only the run_id lookup executed -- strategy_regime_params never
        # read or written for a strategy lacking a corrected backtest.
        self.assertEqual(len(conn.executed), 1)
        self.assertFalse(conn.committed)


class TestApplyOneWrites(unittest.TestCase):
    def test_one_qualifying_regime_writes_true_others_false(self):
        regime_rows = [
            _regime_row('LOW_VOL', 1.2, 100),
            _regime_row('TRANSITIONING', 0.1, 100),
            _regime_row('HIGH_VOL', 2.0, 5),
            _regime_row('CRISIS', -0.5, 50),
        ]
        # Prior: everything currently True (simulates a strategy that was
        # fully active before this derive run).
        prior_rows = [_prior_row(r, True) for r in aa.CANONICAL_REGIMES]
        conn = FakeConn(responses=[{'run_id': 'r1'}, [],regime_rows, prior_rows])
        result = aa.apply_one(conn, 'S_test', threshold=0.5, dry_run=False)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['new'], {'LOW_VOL': True, 'TRANSITIONING': False,
                                         'HIGH_VOL': False, 'CRISIS': False})
        self.assertEqual(result['actions']['LOW_VOL'], 'unchanged')       # True->True
        self.assertEqual(result['actions']['TRANSITIONING'], 'deactivated')
        self.assertEqual(result['actions']['HIGH_VOL'], 'deactivated')
        self.assertEqual(result['actions']['CRISIS'], 'deactivated')
        self.assertTrue(conn.committed)
        # 4 reads (incl. shrink probe) + 2 INSERTs per changed regime
        # (3 changed) = 4 + 6 = 10.
        self.assertEqual(len(conn.executed), 10)
        inserts = [sql for sql, _ in conn.executed if sql.strip().upper().startswith('INSERT')]
        self.assertEqual(len(inserts), 6)
        changes_inserts = [s for s in inserts if 'strategy_regime_param_changes' in s]
        params_inserts = [s for s in inserts if 'strategy_regime_params' in s and 'changes' not in s]
        self.assertEqual(len(changes_inserts), 3)
        self.assertEqual(len(params_inserts), 3)
        for s in params_inserts:
            self.assertIn('ON CONFLICT (strategy_id, regime_state)', s)
            self.assertIn('eligible', s)

    def test_zero_qualifying_regimes_writes_all_false_dormant(self):
        regime_rows = [
            _regime_row('LOW_VOL', 0.1, 100),
            _regime_row('TRANSITIONING', -1.0, 100),
            _regime_row('HIGH_VOL', 0.3, 5),
            _regime_row('CRISIS', None, 50),
        ]
        prior_rows = [_prior_row('LOW_VOL', True), _prior_row('CRISIS', True)]  # partially active before
        conn = FakeConn(responses=[{'run_id': 'r1'}, [],regime_rows, prior_rows])
        result = aa.apply_one(conn, 'S_test', threshold=0.5, dry_run=False)
        self.assertEqual(result['new'], {r: False for r in aa.CANONICAL_REGIMES})
        # LOW_VOL/CRISIS True->False = deactivated; TRANSITIONING/HIGH_VOL
        # had no prior row and compute False = initialized (still written).
        self.assertEqual(result['actions']['LOW_VOL'], 'deactivated')
        self.assertEqual(result['actions']['CRISIS'], 'deactivated')
        self.assertEqual(result['actions']['TRANSITIONING'], 'initialized')
        self.assertEqual(result['actions']['HIGH_VOL'], 'initialized')
        self.assertTrue(conn.committed)

    def test_noop_write_skipped_when_prior_matches(self):
        """server.js's exact no-op guard: a row that ALREADY has the
        computed eligible value gets no INSERT at all (avoids audit spam
        on every weekly run when nothing changed)."""
        regime_rows = [_regime_row(r, 2.0, 100) for r in aa.CANONICAL_REGIMES]  # all pass
        prior_rows = [_prior_row(r, True) for r in aa.CANONICAL_REGIMES]        # already all True
        conn = FakeConn(responses=[{'run_id': 'r1'}, [],regime_rows, prior_rows])
        result = aa.apply_one(conn, 'S_test', threshold=0.5, dry_run=False)
        self.assertTrue(all(a == 'unchanged' for a in result['actions'].values()))
        # 4 reads only (incl. shrink probe), zero writes (commit() is still
        # issued -- harmless, just closes the read-only transaction).
        self.assertEqual(len(conn.executed), 4)
        self.assertTrue(conn.committed)

    def test_first_ever_row_always_written_even_if_false(self):
        """A regime with NO prior strategy_regime_params row must get an
        explicit write even when the computed value is False -- eligibility
        must be fully determined, never implicit-by-omission."""
        regime_rows = [_regime_row('LOW_VOL', -1.0, 100)]  # fails -> False
        prior_rows = []  # no prior rows at all for this strategy
        conn = FakeConn(responses=[{'run_id': 'r1'}, [],regime_rows, prior_rows])
        result = aa.apply_one(conn, 'S_test', threshold=0.5, dry_run=False)
        self.assertEqual(result['actions']['LOW_VOL'], 'initialized')
        inserts = [sql for sql, _ in conn.executed if sql.strip().upper().startswith('INSERT')]
        # LOW_VOL(2) + TRANSITIONING(2) + HIGH_VOL(2) + CRISIS(2) = 8 (all 4
        # regimes have no prior row -> all written, even the 3 False ones).
        self.assertEqual(len(inserts), 8)


if __name__ == '__main__':
    unittest.main()
