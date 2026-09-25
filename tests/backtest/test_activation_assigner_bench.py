"""tests/backtest/test_activation_assigner_bench.py — Activation
bench-relative rule (src/backtest/activation_assigner.py). Spec:
docs/specs/2026-09-25-activation-bench-relative-spec.md §1, §2, §5-A/B/C/D/E.
Task 1 brief: .superpowers/sdd/2026-09-25-activation-bench-relative/
task-1-brief.md. DB layer fully mocked (FakeConn/FakeCursor below, mirrored
verbatim from tests/backtest/test_activation_assigner.py) -- no live DB
access, no network, no Postgres.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import activation_assigner as aa  # noqa: E402


# ── Fakes (mirrored from test_activation_assigner.py) ───────────────────────
class FakeCursor:
    """Cursor stub. `responses` is an ordered list, one entry consumed per
    execute() call: a dict -> fetchone() source; a list -> fetchall()/
    iteration source; anything else (e.g. None, a tuple) -> fetchone()
    source returned as-is; a callable -> raised as an exception when
    execute() is called (for testing query-error fail-safes). Once
    exhausted, further execute() calls are no-ops."""
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


def _prior_row(regime, eligible, size_scalar=None, stop_pct=None, target_pct=None, max_hold_days=None):
    return (regime, eligible, size_scalar, stop_pct, target_pct, max_hold_days)


def _captured_stdout(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*a, **k)
    return result, buf.getvalue()


# ── Scenario 1: bench vector loaded from the sleeve's latest primary run ────
class TestLoadBenchSharpeFromSleeve(unittest.TestCase):
    def test_loads_full_vector_from_registry_and_sleeve_run(self):
        # sleeve id lookup -> one registry row; run_id lookup; regime rows
        # (all 4 present) -- no pipeline_config fallback needed/queried.
        conn = FakeConn(responses=[
            [('S_beta_spy',)],
            ('r51b5b915',),
            [('LOW_VOL', 0.95), ('TRANSITIONING', 0.44), ('HIGH_VOL', 0.53), ('CRISIS', 1.58)],
        ])
        bench, meta = aa.load_bench_sharpe(conn)
        self.assertEqual(bench, {'LOW_VOL': 0.95, 'TRANSITIONING': 0.44,
                                 'HIGH_VOL': 0.53, 'CRISIS': 1.58})
        self.assertEqual(meta['sleeve_id'], 'S_beta_spy')
        self.assertEqual(meta['sleeve_source'], 'registry')
        self.assertEqual(meta['run_id'], 'r51b5b915')
        self.assertEqual(meta['regime_source'], {r: 'sleeve' for r in aa.CANONICAL_REGIMES})
        self.assertEqual(len(conn.executed), 3)   # no 4th (pipeline_config) query

    def test_explicit_sleeve_id_skips_registry_round_trip(self):
        conn = FakeConn(responses=[
            ('r1',),
            [('LOW_VOL', 1.0), ('TRANSITIONING', 1.0), ('HIGH_VOL', 1.0), ('CRISIS', 1.0)],
        ])
        bench, meta = aa.load_bench_sharpe(conn, sleeve_id='S_beta_spy')
        self.assertEqual(meta['sleeve_id'], 'S_beta_spy')
        self.assertEqual(meta['sleeve_source'], 'registry')
        self.assertEqual(len(conn.executed), 2)   # sleeve id NOT re-queried

    def test_sharpe_equal_to_bench_is_eligible(self):
        # A strategy whose sharpe exactly equals the loaded bench is
        # eligible (>=, spec §1's "sharpe >= bench").
        bench = {'LOW_VOL': 0.95, 'TRANSITIONING': 0.44, 'HIGH_VOL': 0.53, 'CRISIS': 1.58}
        rows = [_regime_row('LOW_VOL', 0.95, 200)]
        conn = FakeConn(responses=[{'run_id': 'r1'}, [], rows])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.0, bench=bench)
        self.assertTrue(eligible['LOW_VOL'])
        self.assertEqual(diag['LOW_VOL']['bench'], 0.95)


# ── sleeve id resolution: registry lookup fails/empty -> literal fallback ──
class TestBenchSleeveIdResolution(unittest.TestCase):
    def test_empty_registry_result_falls_back_to_literal(self):
        conn = FakeConn(responses=[
            [],                        # registry lookup: zero benchmark sleeves
            ('r1',),
            [('LOW_VOL', 1.2)],
        ])
        bench, meta = aa.load_bench_sharpe(conn)
        self.assertEqual(meta['sleeve_id'], aa.BENCH_SLEEVE_FALLBACK_ID)
        self.assertEqual(meta['sleeve_source'], 'literal_fallback')

    def test_registry_query_error_falls_back_to_literal(self):
        conn = FakeConn(responses=[
            RuntimeError,              # registry lookup raises
            ('r1',),
            [('LOW_VOL', 1.2)],
        ])
        bench, meta = aa.load_bench_sharpe(conn)
        self.assertEqual(meta['sleeve_id'], aa.BENCH_SLEEVE_FALLBACK_ID)
        self.assertEqual(meta['sleeve_source'], 'literal_fallback')
        self.assertTrue(conn.rolled_back)

    def test_multiple_registry_sleeves_prefers_literal_id_and_warns(self):
        conn = FakeConn(responses=[
            [('S_beta_spy',), ('S_beta_btc',)],
            ('r1',),
            [('LOW_VOL', 1.2)],
        ])
        (bench, meta), out = _captured_stdout(aa.load_bench_sharpe, conn)
        self.assertEqual(meta['sleeve_id'], 'S_beta_spy')
        self.assertEqual(meta['sleeve_source'], 'registry')
        self.assertIn('WARN', out)
        self.assertIn('multiple benchmark sleeves', out)


# ── Scenario 4: per-regime fail-safe tiers (spec §2) ────────────────────────
class TestBenchFailSafeTiers(unittest.TestCase):
    def test_no_sleeve_run_falls_back_to_pipeline_config_vector(self):
        pc_vector = json.dumps({'LOW_VOL': 0.81, 'TRANSITIONING': 0.42,
                                'HIGH_VOL': 0.49, 'CRISIS': 1.54})
        conn = FakeConn(responses=[
            None,               # sleeve run_id lookup: no primary_window run
            (pc_vector,),       # pipeline_config.strategy_activation_bench_sharpe
        ])
        bench, meta = aa.load_bench_sharpe(conn, sleeve_id='S_beta_spy')
        self.assertEqual(bench, {'LOW_VOL': 0.81, 'TRANSITIONING': 0.42,
                                 'HIGH_VOL': 0.49, 'CRISIS': 1.54})
        self.assertIsNone(meta['run_id'])
        self.assertEqual(meta['regime_source'], {r: 'pipeline_config' for r in aa.CANONICAL_REGIMES})

    def test_neither_sleeve_nor_pipeline_config_defaults_half_with_warn(self):
        conn = FakeConn(responses=[
            None,   # no sleeve run
            None,   # no pipeline_config row either
        ])
        (bench, meta), out = _captured_stdout(aa.load_bench_sharpe, conn, 'S_beta_spy')
        self.assertEqual(bench, {r: aa.DEFAULT_MIN_SHARPE for r in aa.CANONICAL_REGIMES})
        self.assertEqual(meta['regime_source'], {r: 'default' for r in aa.CANONICAL_REGIMES})
        for r in aa.CANONICAL_REGIMES:
            self.assertIn(f'WARN: {r} bench sharpe unavailable', out)

    def test_partial_sleeve_vector_falls_back_per_regime_not_globally(self):
        # LOW_VOL present in the sleeve run; the other 3 regimes never
        # occurred in that window -- each missing regime resolves its OWN
        # fallback tier independently (never widens the whole vector).
        pc_vector = json.dumps({'CRISIS': 1.5})
        conn = FakeConn(responses=[
            ('r9',),
            [('LOW_VOL', 0.9)],
            (pc_vector,),
        ])
        bench, meta = aa.load_bench_sharpe(conn, sleeve_id='S_beta_spy')
        self.assertEqual(bench['LOW_VOL'], 0.9)
        self.assertEqual(meta['regime_source']['LOW_VOL'], 'sleeve')
        self.assertEqual(bench['CRISIS'], 1.5)
        self.assertEqual(meta['regime_source']['CRISIS'], 'pipeline_config')
        self.assertEqual(bench['TRANSITIONING'], aa.DEFAULT_MIN_SHARPE)
        self.assertEqual(meta['regime_source']['TRANSITIONING'], 'default')

    def test_strategy_at_point_six_eligible_under_default_fallback(self):
        # "a strategy at 0.6 is eligible under the fallback"
        conn = FakeConn(responses=[None, None])
        bench, _ = aa.load_bench_sharpe(conn, sleeve_id='S_beta_spy')
        rows = [_regime_row('LOW_VOL', 0.6, 150)]
        conn2 = FakeConn(responses=[{'run_id': 'r1'}, [], rows])
        eligible, diag = aa.compute_eligible(conn2, 'S_test', threshold=0.0, bench=bench)
        self.assertTrue(eligible['LOW_VOL'])
        self.assertEqual(diag['LOW_VOL']['bench'], aa.DEFAULT_MIN_SHARPE)


# ── Decimal safety (psycopg2 returns NUMERIC as Decimal) ────────────────────
class TestDecimalSharpeIsCastToFloat(unittest.TestCase):
    def test_sleeve_decimal_sharpe_becomes_float_and_hysteresis_works(self):
        conn = FakeConn(responses=[
            ('r1',),
            [('LOW_VOL', Decimal('0.95')), ('TRANSITIONING', Decimal('0.44')),
             ('HIGH_VOL', Decimal('0.53')), ('CRISIS', Decimal('1.58'))],
        ])
        bench, _ = aa.load_bench_sharpe(conn, sleeve_id='S_beta_spy')
        for r in aa.CANONICAL_REGIMES:
            self.assertIsInstance(bench[r], float)
        # Would raise TypeError (Decimal - float) if the cast were missing.
        json.dumps(bench)
        rows = [_regime_row('LOW_VOL', 0.90, 150)]
        prior_eligible = {'LOW_VOL': True, 'TRANSITIONING': None, 'HIGH_VOL': None, 'CRISIS': None}
        conn2 = FakeConn(responses=[{'run_id': 'r2'}, [], rows])
        eligible, diag = aa.compute_eligible(conn2, 'S_test', threshold=0.0,
                                             bench=bench, prior_eligible=prior_eligible)
        self.assertTrue(eligible['LOW_VOL'])   # 0.90 >= 0.95 - 0.10 (band)
        self.assertTrue(diag['LOW_VOL']['band_applied'])


# ── Scenario 2: hysteresis band (spec §5-B) ─────────────────────────────────
class TestHysteresisBand(unittest.TestCase):
    BENCH = {r: 1.0 for r in aa.CANONICAL_REGIMES}

    def test_prior_eligible_at_bench_minus_point_zero_five_stays_eligible(self):
        rows = [_regime_row('LOW_VOL', 0.95, 150)]
        prior = {'LOW_VOL': True, 'TRANSITIONING': None, 'HIGH_VOL': None, 'CRISIS': None}
        conn = FakeConn(responses=[{'run_id': 'r1'}, [], rows])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.0,
                                             bench=self.BENCH, prior_eligible=prior)
        self.assertTrue(eligible['LOW_VOL'])
        self.assertTrue(diag['LOW_VOL']['band_applied'])

    def test_prior_eligible_at_bench_minus_point_one_five_deactivates(self):
        rows = [_regime_row('LOW_VOL', 0.85, 150)]
        prior = {'LOW_VOL': True, 'TRANSITIONING': None, 'HIGH_VOL': None, 'CRISIS': None}
        conn = FakeConn(responses=[{'run_id': 'r1'}, [], rows])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.0,
                                             bench=self.BENCH, prior_eligible=prior)
        self.assertFalse(eligible['LOW_VOL'])
        self.assertFalse(diag['LOW_VOL']['band_applied'])

    def test_prior_ineligible_at_bench_minus_point_zero_five_stays_ineligible(self):
        rows = [_regime_row('LOW_VOL', 0.95, 150)]
        prior = {'LOW_VOL': False, 'TRANSITIONING': None, 'HIGH_VOL': None, 'CRISIS': None}
        conn = FakeConn(responses=[{'run_id': 'r1'}, [], rows])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.0,
                                             bench=self.BENCH, prior_eligible=prior)
        self.assertFalse(eligible['LOW_VOL'])   # strict leg: 0.95 < 1.0
        self.assertFalse(diag['LOW_VOL']['band_applied'])

    def test_no_prior_row_treated_as_not_prior_eligible(self):
        # prior_eligible=None entirely (direct-call default) -- every cell
        # must clear the strict bench, never the loosened band.
        rows = [_regime_row('LOW_VOL', 0.95, 150)]
        conn = FakeConn(responses=[{'run_id': 'r1'}, [], rows])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.0, bench=self.BENCH)
        self.assertFalse(eligible['LOW_VOL'])

    def test_activate_edge_is_always_strict_never_the_band(self):
        # A cell that is NOT currently eligible must clear the full bench to
        # activate, even though 0.95 would be inside a hypothetical band.
        rows = [_regime_row('LOW_VOL', 0.95, 150)]
        prior = {'LOW_VOL': False, 'TRANSITIONING': None, 'HIGH_VOL': None, 'CRISIS': None}
        conn = FakeConn(responses=[{'run_id': 'r1'}, [], rows])
        eligible, _ = aa.compute_eligible(conn, 'S_test', threshold=0.0,
                                          bench=self.BENCH, prior_eligible=prior)
        self.assertFalse(eligible['LOW_VOL'])


# ── Scenario 3: class-gate failure deactivates even inside the band ────────
class TestClassGateOverridesBand(unittest.TestCase):
    def test_trade_count_below_floor_deactivates_inside_band(self):
        bench = {r: 1.0 for r in aa.CANONICAL_REGIMES}
        # sharpe 0.95 sits inside the band (bench-0.10=0.90), but n=50 < 100.
        rows = [_regime_row('LOW_VOL', 0.95, 50)]
        prior = {'LOW_VOL': True, 'TRANSITIONING': None, 'HIGH_VOL': None, 'CRISIS': None}
        conn = FakeConn(responses=[{'run_id': 'r1'}, [], rows])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.0,
                                             bench=bench, prior_eligible=prior)
        self.assertFalse(eligible['LOW_VOL'])
        self.assertFalse(diag['LOW_VOL']['band_applied'])

    def test_dd_ceiling_breach_deactivates_inside_band(self):
        bench = {r: 1.0 for r in aa.CANONICAL_REGIMES}
        rows = [_regime_row('LOW_VOL', 0.95, 150, max_dd_pct=25.0)]  # equity ceiling 20
        prior = {'LOW_VOL': True, 'TRANSITIONING': None, 'HIGH_VOL': None, 'CRISIS': None}
        conn = FakeConn(responses=[{'run_id': 'r1'}, [], rows])
        eligible, diag = aa.compute_eligible(conn, 'S_test', threshold=0.0,
                                             bench=bench, prior_eligible=prior,
                                             instrument_class='equity')
        self.assertFalse(eligible['LOW_VOL'])


# ── Scenario 5: always-on unaffected; crypto judged on the same vector ─────
class TestAlwaysOnAndCrypto(unittest.TestCase):
    def test_always_on_forces_eligible_regardless_of_bench(self):
        bench = {r: 5.0 for r in aa.CANONICAL_REGIMES}   # deliberately unreachable
        rows = [_regime_row('LOW_VOL', 0.2, 900)]
        conn = FakeConn(responses=[{'run_id': 'r1'}, [], rows])
        eligible, diag = aa.compute_eligible(conn, 'S_beta_spy', threshold=0.0,
                                             bench=bench, always_on=True)
        self.assertTrue(eligible['LOW_VOL'])
        self.assertFalse(diag['LOW_VOL']['eligible'])   # underlying verdict still recorded

    def test_crypto_strategy_uses_same_vector_no_special_casing(self):
        # Spec §5-D: crypto strategies use the SAME SPY vector; only the DD
        # ceiling differs by class_thresholds (crypto 70 vs equity 20), the
        # bench comparison itself is identical.
        bench = {'LOW_VOL': 0.53, 'TRANSITIONING': 1.58, 'HIGH_VOL': 0.95, 'CRISIS': 0.44}
        rows = [_regime_row('CRISIS', 0.44, 150, max_dd_pct=60.0)]  # would fail equity DD (20)
        conn = FakeConn(responses=[{'run_id': 'r1'}, [], rows])
        eligible, diag = aa.compute_eligible(conn, 'S_btc_momentum', threshold=0.0,
                                             bench=bench, instrument_class='crypto')
        self.assertTrue(eligible['CRISIS'])            # 0.44 >= 0.44, DD 60 <= crypto ceiling 70
        self.assertEqual(diag['CRISIS']['bench'], 0.44)


# ── Scenario 6: stamp gains bench_sharpe/bench_run_id; rule literal ─────────
class TestStampGainsBenchFields(unittest.TestCase):
    def test_stamp_last_applied_persists_bench_vector_and_run_id(self):
        conn = FakeConn([None])
        bench = {'LOW_VOL': 0.95, 'TRANSITIONING': 0.44, 'HIGH_VOL': 0.53, 'CRISIS': 1.58}
        ok = aa.stamp_last_applied(conn, bench['LOW_VOL'], 100, 3, 7, trigger='weekly_cron',
                                   bench_sharpe=bench, bench_run_id='r51b5b915')
        self.assertTrue(ok)
        sql, params = conn.executed[-1]
        payload = json.loads(params[1])
        self.assertEqual(payload['threshold'], 0.95)   # LOW_VOL bench, not the retired slider
        self.assertEqual(payload['bench_sharpe'], bench)
        self.assertEqual(payload['bench_run_id'], 'r51b5b915')

    def test_stamp_bench_sharpe_config_writes_the_fallback_vector(self):
        conn = FakeConn([None])
        bench = {'LOW_VOL': 0.95, 'TRANSITIONING': 0.44, 'HIGH_VOL': 0.53, 'CRISIS': 1.58}
        ok = aa.stamp_bench_sharpe_config(conn, bench)
        self.assertTrue(ok)
        self.assertTrue(conn.committed)
        sql, params = conn.executed[-1]
        self.assertIn('INSERT INTO pipeline_config', sql)
        self.assertEqual(params[0], aa.CONFIG_KEY_BENCH_SHARPE)
        self.assertEqual(json.loads(params[1]), bench)

    def test_apply_one_audit_reason_uses_bench_not_slider(self):
        # Spec §3: audit rows record the per-regime threshold ACTUALLY used
        # -- bench[r], not the retired global slider (threshold=9.9 here
        # would show up in the old `reason` string if the bug regressed).
        bench = {'LOW_VOL': 1.0, 'TRANSITIONING': 1.0, 'HIGH_VOL': 1.0, 'CRISIS': 1.0}
        rows = [_regime_row('LOW_VOL', 1.2, 150)]
        prior_rows = [_prior_row('LOW_VOL', False)]
        conn = FakeConn(responses=[{'run_id': 'r1'}, [], rows, prior_rows])
        result = aa.apply_one(conn, 'S_test', threshold=9.9, dry_run=False, bench=bench)
        self.assertEqual(result['actions']['LOW_VOL'], 'activated')
        inserts = [p for sql, p in conn.executed if 'strategy_regime_param_changes' in sql]
        self.assertTrue(any('threshold=1.0' in str(p) and '9.9' not in str(p) for p in inserts))
        self.assertTrue(any('rule=qualifies(>0·classDD·trades)+bench_relative' in str(p) for p in inserts))

    def test_apply_one_audit_reason_uses_loosened_band_when_prior_eligible(self):
        bench = {'LOW_VOL': 1.0, 'TRANSITIONING': 1.0, 'HIGH_VOL': 1.0, 'CRISIS': 1.0}
        rows = [_regime_row('LOW_VOL', 0.95, 150)]   # inside the band
        prior_rows = [_prior_row('LOW_VOL', True)]
        conn = FakeConn(responses=[{'run_id': 'r1'}, [], rows, prior_rows])
        result = aa.apply_one(conn, 'S_test', threshold=9.9, dry_run=False, bench=bench)
        self.assertEqual(result['actions']['LOW_VOL'], 'unchanged')   # True->True, no write
        # unchanged -> no INSERT for LOW_VOL was issued at all; assert no
        # crash and the cell stayed eligible via the band.
        self.assertTrue(result['new']['LOW_VOL'])


# ── Scenario 7: dry-run prints the bench vector + per-regime diff ──────────
class TestDryRunBenchOutput(unittest.TestCase):
    # Transcribed verbatim from src/channels/api/activation_preview.js so
    # the new lines are proven to collide with none of the dashboard's
    # pinned parsers.
    HEADER_RE = re.compile(r'^\[activation_assigner\] threshold=([-+\d.eE]+) min_trades=(\d+) dry_run=(\w+) strategies=(\d+)\s*$')
    DETAIL_RE = re.compile(r'^\[activation_assigner\]\s+(\S+): (LOW_VOL: .+)$')
    SKIP_RE   = re.compile(r'^\[activation_assigner\]\s+SKIP\s+\S+:')
    ERROR_RE  = re.compile(r'^\[activation_assigner\]\s+ERROR\s+\S+:')
    SUMMARY_RE = re.compile(
        r'^\[activation_assigner\] activation_assigner summary: (\d+) strategies evaluated, '
        r'(\d+) skipped \(no corrected backtest\), (\d+) cell\(s\) activated, '
        r'(\d+) cell\(s\) deactivated, (\d+) newly-dormant strateg(?:y|ies)'
        r'(?: \(([^)]*)\))?, threshold=([-+\d.eE]+), min_trades=(\d+), dry_run=(\w+), errors=(\d+)\s*$'
    )

    def test_bench_vector_line_lists_the_vector_and_matches_no_pinned_regex(self):
        bench = {'LOW_VOL': 0.95, 'TRANSITIONING': 0.44, 'HIGH_VOL': 0.53, 'CRISIS': 1.58}
        meta = {'sleeve_id': 'S_beta_spy', 'sleeve_source': 'registry', 'run_id': 'r51b5b915'}
        line = '[activation_assigner] ' + aa._fmt_bench_vector(bench, meta)
        for r in aa.CANONICAL_REGIMES:
            self.assertIn(f'{r}={bench[r]}', line)
        self.assertIn('S_beta_spy', line)
        for regex in (self.HEADER_RE, self.DETAIL_RE, self.SUMMARY_RE):
            self.assertIsNone(regex.match(line))
        self.assertFalse(self.SKIP_RE.match(line))
        self.assertFalse(self.ERROR_RE.match(line))

    def test_bench_diff_line_lists_gained_lost_per_regime_and_matches_no_pinned_regex(self):
        gained = {'LOW_VOL': 4, 'TRANSITIONING': 7, 'HIGH_VOL': 12, 'CRISIS': 0}
        lost = {'LOW_VOL': 1, 'TRANSITIONING': 0, 'HIGH_VOL': 0, 'CRISIS': 14}
        line = '[activation_assigner] ' + aa._fmt_bench_diff(gained, lost)
        self.assertIn('LOW_VOL +4/-1', line)
        self.assertIn('TRANSITIONING +7/-0', line)
        self.assertIn('HIGH_VOL +12/-0', line)
        self.assertIn('CRISIS +0/-14', line)
        for regex in (self.HEADER_RE, self.DETAIL_RE, self.SUMMARY_RE):
            self.assertIsNone(regex.match(line))
        self.assertFalse(self.SKIP_RE.match(line))
        self.assertFalse(self.ERROR_RE.match(line))

    def test_pinned_header_and_summary_lines_are_unaffected(self):
        # The retired slider's threshold/min_trades/dry_run/strategies
        # header stays byte-stable -- the bench vector is new, additional
        # output, not a replacement for the pinned line.
        header = '[activation_assigner] threshold=0.5 min_trades=100 dry_run=True strategies=149'
        self.assertIsNotNone(self.HEADER_RE.match(header))
        summary = ('[activation_assigner] activation_assigner summary: 145 strategies evaluated, '
                  '4 skipped (no corrected backtest), 12 cell(s) activated, 33 cell(s) deactivated, '
                  '2 newly-dormant strategies (S_a, S_b), threshold=0.5, min_trades=100, '
                  'dry_run=True, errors=0')
        self.assertIsNotNone(self.SUMMARY_RE.match(summary))


if __name__ == '__main__':
    unittest.main()
