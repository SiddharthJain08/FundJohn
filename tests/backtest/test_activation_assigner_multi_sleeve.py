"""Crypto engine Phase 3 Task 3: more than one benchmark sleeve in the
registry (S_beta_spy + S_beta_btc).

- always_on holds for EVERY benchmark-sleeve id (characterization: already
  true via `always_on=(sid in bench_ids)`; locked here).
- the EQUITY activation bench vector never comes from the crypto sleeve
  (ruling): both resolve_bench_sleeve_id and main()'s inline pick exclude
  crypto sleeves.
- Task 4 (2026-11-18 btc-sleeve-floor-and-class-bench): crypto-class
  strategies are judged against the CRYPTO sleeve's vector (S_beta_btc),
  with crypto-only fallbacks -- never the SPY vector or the equity tier-2 key.
DB fully mocked; no Postgres, no network.
"""
from __future__ import annotations

import io
import contextlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import activation_assigner as aa  # noqa: E402
from tests.backtest.test_activation_assigner_bench import FakeConn  # noqa: E402

_PARAMS = {
    'S_beta_spy': {'benchmark_sleeve': True, 'instrument_class': 'etp'},
    'S_beta_btc': {'benchmark_sleeve': True, 'instrument_class': 'crypto', 'benchmark_ticker': 'BTC-USD'},
}
_BENCH_VECTOR = {r: 1.0 for r in aa.CANONICAL_REGIMES}
_BENCH_META = {'sleeve_id': 'S_beta_spy', 'sleeve_source': 'registry', 'run_id': 'r1',
               'regime_source': {r: 'sleeve' for r in aa.CANONICAL_REGIMES}}


def _registry_ids(registry, sql, params):
    """What the class-filtered registry sleeve query returns (fix wave M4:
    main() resolves the crypto sleeve via resolve_bench_sleeve_id, so the
    stubs answer that SQL from the helper's bench_ids/params)."""
    if '<> %s' in sql:
        return sorted(s for s, c in registry.items() if c != 'crypto')
    return sorted(s for s, c in registry.items() if c == params[-1])


def _registry(bench_ids, params):
    return {s: ('crypto' if str((params.get(s) or {}).get('instrument_class', '')).lower() == 'crypto'
                else 'equity') for s in bench_ids}


class _Cur:
    def __init__(self, sids, registry=None):
        self._sids = sids
        self._registry = registry or {}
        self._all = None

    def execute(self, sql, params=()):
        self._all = None
        if 'strategy_registry' in sql:
            self._all = [(s,) for s in _registry_ids(self._registry, sql, params)]

    def fetchone(self):
        return None

    def fetchall(self):
        if self._all is not None:
            return list(self._all)
        return [(s,) for s in self._sids]

    def close(self):
        pass


class _Conn:
    def __init__(self, sids, registry=None):
        self.cur = _Cur(sids, registry)

    def cursor(self, cursor_factory=None):
        return self.cur

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _run_main(bench_ids, params, sids=('S_beta_btc', 'S_beta_spy', 'S_alpha')):
    seen = {}

    def _apply_one(conn, sid, **kw):
        seen[sid] = kw
        return {'status': 'ok', 'strategy_id': sid,
                'prior': {r: None for r in aa.CANONICAL_REGIMES},
                'new': {r: True for r in aa.CANONICAL_REGIMES},
                'diag': {}, 'actions': {r: 'initialized' for r in aa.CANONICAL_REGIMES}}

    buf = io.StringIO()
    with mock.patch.object(aa, 'psycopg2') as pg, \
         mock.patch('execution.benchmark_sleeve.load_benchmark_sleeve_ids', return_value=set(bench_ids)), \
         mock.patch('execution.benchmark_sleeve.load_benchmark_sleeve_params', return_value=dict(params)), \
         mock.patch.object(aa, 'load_bench_sharpe', return_value=(_BENCH_VECTOR, _BENCH_META)) as lbs, \
         mock.patch.object(aa, 'apply_one', side_effect=_apply_one), \
         mock.patch.object(aa, 'stamp_last_applied'), \
         mock.patch.object(aa, 'stamp_bench_sharpe_config'), \
         mock.patch.object(sys, 'argv', ['activation_assigner.py', '--all', '--dry-run']), \
         mock.patch.dict(os.environ, {'POSTGRES_URI': 'postgres://fake/fake'}), \
         contextlib.redirect_stdout(buf):
        pg.connect.return_value = _Conn(list(sids), _registry(bench_ids, params))
        rc = aa.main()
    return rc, seen, lbs, buf.getvalue()


class TestAlwaysOnEverySleeve(unittest.TestCase):
    def test_assigner_always_on_for_every_sleeve(self):
        rc, seen, _lbs, _out = _run_main({'S_beta_spy', 'S_beta_btc'}, _PARAMS)
        self.assertEqual(rc, 0)
        self.assertTrue(seen['S_beta_spy']['always_on'])
        self.assertTrue(seen['S_beta_btc']['always_on'])
        self.assertFalse(seen['S_alpha']['always_on'])

    def test_main_bench_vector_uses_equity_sleeve_without_multi_sleeve_warn(self):
        _rc, _seen, lbs, out = _run_main({'S_beta_spy', 'S_beta_btc'}, _PARAMS)
        # First call = the EQUITY vector (Task 4 adds a second, crypto call).
        self.assertEqual(lbs.call_args_list[0].kwargs['sleeve_id'], 'S_beta_spy')
        self.assertNotIn('multiple benchmark sleeves', out)

    def test_main_equity_vector_never_from_crypto_sleeve_when_equity_sleeve_absent(self):
        # S_beta_btc sorts before S_beta_spy; with SPY missing the old inline
        # pick would have chosen it. Now: no equity sleeve -> None -> the
        # resolver's own (equity-filtered) lookup / literal fallback. Task 4:
        # the crypto sleeve IS loaded -- but only as the crypto-class vector.
        _rc, _seen, lbs, _out = _run_main({'S_beta_btc'}, {'S_beta_btc': _PARAMS['S_beta_btc']})
        eq_call = lbs.call_args_list[0]
        self.assertIsNone(eq_call.kwargs['sleeve_id'])
        self.assertNotEqual(eq_call.kwargs.get('instrument_class', 'equity'), 'crypto')
        crypto_calls = [c for c in lbs.call_args_list
                        if c.kwargs.get('instrument_class') == 'crypto']
        self.assertEqual([c.kwargs['sleeve_id'] for c in crypto_calls], ['S_beta_btc'])

    def test_main_multiple_equity_sleeves_still_warns(self):
        params = dict(_PARAMS, S_alpha_spy={'benchmark_sleeve': True})
        _rc, _seen, lbs, out = _run_main({'S_beta_spy', 'S_beta_btc', 'S_alpha_spy'}, params)
        self.assertEqual(lbs.call_args_list[0].kwargs['sleeve_id'], 'S_beta_spy')
        self.assertIn('multiple benchmark sleeves', out)


class TestResolveBenchSleeveIdClass(unittest.TestCase):
    def test_resolve_bench_sleeve_id_prefers_equity_sleeve_for_bench_vector(self):
        # The registry query itself excludes crypto sleeves (FakeConn returns
        # what the filtered SQL would: only the equity row).
        conn = FakeConn(responses=[[('S_beta_spy',)]])
        sid, src = aa.resolve_bench_sleeve_id(conn)
        self.assertEqual((sid, src), ('S_beta_spy', 'registry'))
        sql, params = conn.executed[0]
        self.assertIn("COALESCE(LOWER(parameters ->> %s), %s)", sql)
        self.assertIn("instrument_class", params)
        self.assertIn("<> %s", sql)
        self.assertIn('crypto', params)
        self.assertIn('equity', params)

    def test_resolve_crypto_class_selects_crypto_rows(self):
        conn = FakeConn(responses=[[('S_beta_btc',)]])
        sid, src = aa.resolve_bench_sleeve_id(conn, instrument_class='crypto')
        self.assertEqual((sid, src), ('S_beta_btc', 'registry'))
        sql, params = conn.executed[0]
        self.assertIn("= %s", sql)
        self.assertNotIn("<> %s", sql)
        self.assertIn('crypto', params)

    def test_explicit_sleeve_id_still_short_circuits(self):
        conn = FakeConn(responses=[])
        self.assertEqual(aa.resolve_bench_sleeve_id(conn, 'S_beta_spy'), ('S_beta_spy', 'registry'))
        self.assertEqual(conn.executed, [])

    def test_load_bench_sharpe_resolves_equity_sleeve(self):
        conn = FakeConn(responses=[[('S_beta_spy',)], ('r1',), [('LOW_VOL', 1.2)]])
        _bench, meta = aa.load_bench_sharpe(conn)
        self.assertEqual(meta['sleeve_id'], 'S_beta_spy')
        self.assertIn('crypto', conn.executed[0][1])


# ── Task 4: per-class bench vector (real load_bench_sharpe over a SQL stub) ──
PARAMS = _PARAMS
CANONICAL_REGIMES = aa.CANONICAL_REGIMES
_RUNS = {
    'S_beta_btc': ('rb', {'LOW_VOL': 0.86, 'TRANSITIONING': 0.23,
                          'HIGH_VOL': -0.49, 'CRISIS': 0.60}),
    'S_beta_spy': ('rs', {'LOW_VOL': 0.9378, 'TRANSITIONING': 0.4299,
                          'HIGH_VOL': 0.5326, 'CRISIS': 1.575}),
}


class _SqlCur:
    """Dispatches on the SQL text: pipeline_config reads/writes, sleeve
    primary runs + regime rows, the class-filtered registry sleeve lookup
    (answered from the helper's bench_ids/params), and the --all sid list."""
    def __init__(self, conn):
        self.c = conn
        self._one = None
        self._all = []

    def execute(self, sql, params=()):
        self._one, self._all = None, []
        if 'INSERT INTO pipeline_config' in sql:
            self.c.stamps[params[0]] = params[1]
            self.c.stamp_desc[params[0]] = params[2]
        elif 'FROM pipeline_config' in sql:
            v = self.c.config.get(params[0])
            self._one = (v,) if v is not None else None
        elif 'FROM strategy_backtest_runs' in sql and 'strategy_id = %s' in sql:
            run = self.c.runs.get(params[0])
            self._one = (run[0],) if run else None
        elif 'FROM strategy_backtest_regimes' in sql:
            for _sid, (rid, vec) in self.c.runs.items():
                if rid == params[0]:
                    self._all = list(vec.items())
        elif 'SELECT DISTINCT strategy_id' in sql:
            self._all = [(s,) for s in self.c.sids]
        elif 'strategy_registry' in sql:
            self.c.registry_queries.append(params)
            self._all = [(s,) for s in _registry_ids(self.c.registry, sql, params)]

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._all)

    def close(self):
        pass


class _SqlConn:
    def __init__(self, sids, config, runs, registry=None):
        self.sids, self.config, self.runs = list(sids), dict(config), dict(runs)
        self.registry = dict(registry or {})
        self.stamps: dict = {}
        self.stamp_desc: dict = {}
        self.registry_queries: list = []

    def cursor(self, cursor_factory=None):
        return _SqlCur(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _run_main_capture(bench_ids, params, classes, sids, config=None, runs=None,
                      all_apply=False):
    """main() with the real load_bench_sharpe / stamps over _SqlConn.
    Returns (apply_one kwargs per sid, stdout[, stamped pipeline_config])."""
    seen = {}

    def _apply_one(conn, sid, **kw):
        seen[sid] = kw
        return {'status': 'ok', 'strategy_id': sid,
                'prior': {r: None for r in aa.CANONICAL_REGIMES},
                'new': {r: True for r in aa.CANONICAL_REGIMES},
                'diag': {}, 'actions': {r: 'initialized' for r in aa.CANONICAL_REGIMES}}

    conn = _SqlConn(sids, config or {}, _RUNS if runs is None else runs,
                    registry=_registry(bench_ids, params))
    argv = ['activation_assigner.py', '--all'] + ([] if all_apply else ['--dry-run'])
    buf = io.StringIO()
    with mock.patch.object(aa, 'psycopg2') as pg, \
         mock.patch('execution.benchmark_sleeve.load_benchmark_sleeve_ids', return_value=set(bench_ids)), \
         mock.patch('execution.benchmark_sleeve.load_benchmark_sleeve_params', return_value=dict(params)), \
         mock.patch.object(aa, '_load_instrument_classes', return_value=dict(classes)), \
         mock.patch.object(aa, 'apply_one', side_effect=_apply_one), \
         mock.patch.object(sys, 'argv', argv), \
         mock.patch.dict(os.environ, {'POSTGRES_URI': 'postgres://fake/fake'}), \
         contextlib.redirect_stdout(buf):
        pg.connect.return_value = conn
        rc = aa.main()
    assert rc == 0, buf.getvalue()
    if all_apply:
        return seen, buf.getvalue(), conn.stamps
    return seen, buf.getvalue()


class TestPerClassBenchVector(unittest.TestCase):
    def test_crypto_strategy_judged_against_crypto_sleeve_vector(self):
        calls, _ = _run_main_capture(bench_ids={'S_beta_spy', 'S_beta_btc'}, params=PARAMS,
                                     classes={'S_alpha': 'equity', 'S_btc_x': 'crypto'},
                                     sids=('S_alpha', 'S_btc_x'))
        self.assertEqual(calls['S_btc_x']['bench']['TRANSITIONING'], 0.23)
        self.assertEqual(calls['S_alpha']['bench']['TRANSITIONING'], 0.4299)

    def test_crypto_vector_without_crypto_sleeve_never_uses_equity(self):
        calls, out = _run_main_capture(bench_ids={'S_beta_spy'}, params=PARAMS,
                                       classes={'S_btc_x': 'crypto'}, sids=('S_btc_x',), config={})
        self.assertEqual(calls['S_btc_x']['bench'], {r: 0.5 for r in CANONICAL_REGIMES})
        self.assertIn('WARN: no crypto benchmark sleeve', out)
        self.assertIn('[activation_assigner] bench[crypto]: sleeve=None source=literal_fallback run=None', out)

    def test_crypto_tier2_reads_crypto_key_only(self):
        calls, _ = _run_main_capture(bench_ids={'S_beta_spy'}, params=PARAMS, classes={'S_btc_x': 'crypto'},
                                     sids=('S_btc_x',),
                                     config={'strategy_activation_bench_sharpe': '{"LOW_VOL": 9.0}',
                                             'strategy_activation_bench_sharpe_crypto': '{"LOW_VOL": 0.7}'})
        self.assertEqual(calls['S_btc_x']['bench']['LOW_VOL'], 0.7)
        self.assertEqual(calls['S_btc_x']['bench']['CRISIS'], 0.5)

    def test_log_lines_and_stamps(self):
        _, out, stamps = _run_main_capture(bench_ids={'S_beta_spy', 'S_beta_btc'}, params=PARAMS,
                                           classes={'S_btc_x': 'crypto'}, sids=('S_btc_x',), all_apply=True)
        self.assertIn('[activation_assigner] bench[crypto]: sleeve=S_beta_btc source=registry run=rb LOW_VOL=0.86', out)
        self.assertIn('[activation_assigner] thresholds[crypto]: LOW_VOL=0.86 ', out)
        self.assertEqual(out.count('[activation_assigner] bench: sleeve='), 1)     # equity line still once
        self.assertEqual(out.count('[activation_assigner] thresholds: '), 1)
        la = json.loads(stamps['strategy_activation_last_applied'])
        self.assertEqual(la['bench_by_class']['crypto']['bench_run_id'], 'rb')
        self.assertEqual(la['bench_by_class']['crypto']['sleeve_id'], 'S_beta_btc')
        self.assertEqual(la['bench_sharpe']['LOW_VOL'], 0.9378)                    # equity keys untouched
        self.assertEqual(la['bench_run_id'], 'rs')
        self.assertEqual(json.loads(stamps['strategy_activation_bench_sharpe_crypto'])['CRISIS'], 0.6)
        self.assertEqual(json.loads(stamps['strategy_activation_bench_sharpe'])['CRISIS'], 1.575)

    def test_crypto_sleeve_run_missing_one_regime_takes_crypto_key(self):
        # I1: the crypto sleeve run has three regimes only; both tier-2 keys
        # are set to different values -> the missing regime (CRISIS) takes the
        # CRYPTO key's value, never the equity key's (nor the SPY sleeve's).
        runs = dict(_RUNS, S_beta_btc=('rb', {'LOW_VOL': 0.86, 'TRANSITIONING': 0.23,
                                              'HIGH_VOL': -0.49}))
        calls, _ = _run_main_capture(bench_ids={'S_beta_spy', 'S_beta_btc'}, params=PARAMS,
                                     classes={'S_alpha': 'equity', 'S_btc_x': 'crypto'},
                                     sids=('S_alpha', 'S_btc_x'), runs=runs,
                                     config={'strategy_activation_bench_sharpe': '{"CRISIS": 9.0}',
                                             'strategy_activation_bench_sharpe_crypto': '{"CRISIS": 0.7}'})
        cb = calls['S_btc_x']['bench']
        self.assertEqual(cb['CRISIS'], 0.7)
        self.assertEqual((cb['LOW_VOL'], cb['TRANSITIONING'], cb['HIGH_VOL']), (0.86, 0.23, -0.49))
        self.assertEqual(calls['S_alpha']['bench']['CRISIS'], 1.575)       # equity: its own sleeve run

    def test_equity_only_fleet_has_no_crypto_lines_or_stamps(self):
        _, out, stamps = _run_main_capture(bench_ids={'S_beta_spy'}, params=PARAMS,
                                           classes={'S_alpha': 'equity'}, sids=('S_alpha',), all_apply=True)
        self.assertNotIn('[crypto]', out)
        self.assertNotIn('bench_by_class', json.loads(stamps['strategy_activation_last_applied']))
        self.assertNotIn('strategy_activation_bench_sharpe_crypto', stamps)

    def test_crypto_lines_match_no_dashboard_regex(self):
        line_b = '[activation_assigner] ' + aa._fmt_bench_vector(
            {r: 0.5 for r in CANONICAL_REGIMES},
            {'sleeve_id': 'S_beta_btc', 'sleeve_source': 'registry', 'run_id': 'rb',
             'instrument_class': 'crypto'})
        line_t = '[activation_assigner] ' + aa._fmt_thresholds(
            {r: 0.5 for r in CANONICAL_REGIMES}, 0.0, instrument_class='crypto')
        for line in (line_b, line_t):
            self.assertNotIn('] bench: ', line)
            self.assertNotIn('] thresholds: ', line)
            self.assertNotIn(': LOW_VOL: ', line)
            self.assertNotIn('threshold=', line)


if __name__ == '__main__':
    unittest.main()
