"""Crypto engine Phase 3 Task 3: more than one benchmark sleeve in the
registry (S_beta_spy + S_beta_btc).

- always_on holds for EVERY benchmark-sleeve id (characterization: already
  true via `always_on=(sid in bench_ids)`; locked here).
- the activation bench VECTOR stays the EQUITY sleeve's (ruling): both
  resolve_bench_sleeve_id and main()'s inline pick exclude crypto sleeves.
DB fully mocked; no Postgres, no network.
"""
from __future__ import annotations

import io
import contextlib
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


class _Cur:
    def __init__(self, sids):
        self._sids = sids

    def execute(self, sql, params=()):
        pass

    def fetchone(self):
        return None

    def fetchall(self):
        return [(s,) for s in self._sids]

    def close(self):
        pass


class _Conn:
    def __init__(self, sids):
        self.cur = _Cur(sids)

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
        pg.connect.return_value = _Conn(list(sids))
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
        self.assertEqual(lbs.call_args.kwargs['sleeve_id'], 'S_beta_spy')
        self.assertNotIn('multiple benchmark sleeves', out)

    def test_main_never_picks_crypto_sleeve_when_equity_sleeve_absent(self):
        # S_beta_btc sorts before S_beta_spy; with SPY missing the old inline
        # pick would have chosen it. Now: no equity sleeve -> None -> the
        # resolver's own (equity-filtered) lookup / literal fallback.
        _rc, _seen, lbs, _out = _run_main({'S_beta_btc'}, {'S_beta_btc': _PARAMS['S_beta_btc']})
        self.assertIsNone(lbs.call_args.kwargs['sleeve_id'])

    def test_main_multiple_equity_sleeves_still_warns(self):
        params = dict(_PARAMS, S_alpha_spy={'benchmark_sleeve': True})
        _rc, _seen, lbs, out = _run_main({'S_beta_spy', 'S_beta_btc', 'S_alpha_spy'}, params)
        self.assertEqual(lbs.call_args.kwargs['sleeve_id'], 'S_beta_spy')
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


if __name__ == '__main__':
    unittest.main()
