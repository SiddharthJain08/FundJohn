"""Crypto engine Phase 3 Task 2: benchmark-keyed S_m cache (schema 3), the
crypto hurdle's zero floor, per-class max NAV fraction, crypto sleeve weight
floored at zero. Fakes only — no DB."""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import benchmark_sizing as bz      # noqa: E402
from execution import strategy_weights as sw      # noqa: E402

# tests/execution/conftest.py stubs the provider to `lambda *a, **k: None` for
# every file but test_benchmark_sizing.py; capture the real one at import
# (collection precedes the autouse fixture) so these tests exercise its body.
_PROVIDER = bz.regime_benchmark_sharpe_for_sizing


class _Cur:
    def __init__(self, store): self.store = store
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        if sql.lstrip().upper().startswith('SELECT'):
            v = self.store.get(params[0])
            self._row = (v,) if v is not None else None
        else:
            self.store[params[0]] = params[1]
    def fetchone(self): return self._row


class _Conn:
    def __init__(self, store): self.store = store
    def cursor(self): return _Cur(self.store)
    def commit(self): pass
    def close(self): pass


HS = (1, 2, 3, 5, 10, 21)


def _grid(v):
    return {r: {h: v.get(r) for h in HS} for r in bz.CANONICAL_REGIMES}


@pytest.fixture(autouse=True)
def _const_rf(monkeypatch):
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'const')


def test_cache_schema3_roundtrip_and_schema2_upgrade():
    assert bz.CACHE_SCHEMA == 3
    # a live schema-2 SPY payload (pre-Phase-3 shape) is served as benchmarks.SPY
    v2 = {'schema': 2, 'as_of': '2026-11-10', 'benchmark': 'SPY', 'start': '2016-04-11',
          'horizons': list(HS), 'rf_source': 'const',
          'by_regime': {r: {str(h): 0.5 for h in HS} for r in bz.CANONICAL_REGIMES}}
    store = {bz.CONFIG_KEY: json.dumps(v2)}
    conn = _Conn(store)
    up = bz._read_cache(conn)
    assert up['schema'] == 3 and set(up['benchmarks']) == {'SPY'}
    assert up['benchmarks']['SPY']['by_regime']['LOW_VOL']['1'] == 0.5
    # cache hit straight off the upgraded payload: no compute
    def boom(*a, **k): raise AssertionError('must not recompute')
    assert _PROVIDER('LOW_VOL', date(2026, 11, 10), conn=conn,
                                                 compute=boom, horizon=1) == 0.5
    # the next write (a BTC refresh) rewrites the whole key as schema 3, SPY kept
    calls = []
    def compute(start, end, benchmark='SPY', min_obs=40, regime_source='equity'):
        calls.append((benchmark, regime_source))
        return _grid({'LOW_VOL': 1.2, 'CRISIS': -0.3})
    assert _PROVIDER('LOW_VOL', date(2026, 11, 10), benchmark='BTC-USD',
                                                 conn=conn, compute=compute, horizon=1) == 1.2
    assert calls == [('BTC-USD', 'crypto')]
    raw = json.loads(store[bz.CONFIG_KEY])
    assert raw['schema'] == 3 and raw['rf_source'] == 'const'
    assert set(raw['benchmarks']) == {'SPY', 'BTC-USD'}
    assert raw['benchmarks']['BTC-USD']['regime_source'] == 'crypto'
    assert raw['benchmarks']['BTC-USD']['trading_days'] == 365
    # round trip: both served from cache, no recompute
    assert _PROVIDER('CRISIS', date(2026, 11, 10), benchmark='BTC-USD',
                                                 conn=conn, compute=boom, horizon=1) == -0.3
    assert _PROVIDER('HIGH_VOL', date(2026, 11, 10), conn=conn,
                                                 compute=boom, horizon=5) == 0.5
    # a schema-1 payload (no schema key) stays a miss
    assert bz._read_cache(_Conn({bz.CONFIG_KEY: json.dumps({'as_of': '2026-11-10', 'benchmark': 'SPY',
                                                            'by_regime': {'LOW_VOL': 2.0}})})) \
        == {'schema': 3, 'rf_source': None, 'benchmarks': {}}


def test_btc_refresh_does_not_evict_spy():
    store = {}
    conn = _Conn(store)
    spy = lambda *a, **k: _grid({'LOW_VOL': 0.8})
    btc = lambda *a, **k: _grid({'LOW_VOL': 1.1})
    assert _PROVIDER('LOW_VOL', date(2026, 11, 9), conn=conn,
                                                 compute=spy, horizon=1) == 0.8
    # next day: BTC refreshes first; SPY's stale-dated entry must survive the write
    assert _PROVIDER('LOW_VOL', date(2026, 11, 10), benchmark='BTC-USD',
                                                 conn=conn, compute=btc, horizon=1) == 1.1
    raw = json.loads(store[bz.CONFIG_KEY])
    assert raw['benchmarks']['SPY']['as_of'] == '2026-11-09'
    assert raw['benchmarks']['BTC-USD']['as_of'] == '2026-11-10'
    # SPY's refresh in turn keeps BTC
    assert _PROVIDER('LOW_VOL', date(2026, 11, 10), conn=conn,
                                                 compute=spy, horizon=1) == 0.8
    raw = json.loads(store[bz.CONFIG_KEY])
    assert {b: e['as_of'] for b, e in raw['benchmarks'].items()} == {'SPY': '2026-11-10',
                                                                      'BTC-USD': '2026-11-10'}
    # an rf-source flip invalidates per entry, never by eviction of the other benchmark
    import os
    os.environ['OPENCLAW_RF_SOURCE'] = 'macro'
    try:
        calls = []
        _PROVIDER('LOW_VOL', date(2026, 11, 10), conn=conn,
                                              compute=lambda *a, **k: calls.append(1) or spy(), horizon=1)
        assert calls == [1]
        raw = json.loads(store[bz.CONFIG_KEY])
        assert raw['benchmarks']['BTC-USD']['rf_source'] == 'const'
        assert raw['benchmarks']['SPY']['rf_source'] == 'macro'
    finally:
        os.environ['OPENCLAW_RF_SOURCE'] = 'const'


def test_crypto_hurdle_floors_negative_s_m():
    w = {'BTC-USD': 0.9, 'ETH-USD': 0.4, 'SOL-USD': -0.2}
    out, dropped = bz.apply_benchmark_hurdle(w, -0.3, {'BTC-USD'}, floor_zero=True)
    assert out == w and dropped == []
    # without the floor (equity semantics) a negative S_m inflates alts
    out_eq, _ = bz.apply_benchmark_hurdle(w, -0.3, {'BTC-USD'})
    assert out_eq['ETH-USD'] == pytest.approx(0.7)
    # positive S_m behaves identically with or without the floor
    assert bz.apply_benchmark_hurdle(w, 0.3, {'BTC-USD'}, floor_zero=True) == \
        bz.apply_benchmark_hurdle(w, 0.3, {'BTC-USD'})


def test_benchmark_max_nav_frac_per_class_defaults():
    assert bz.benchmark_max_nav_frac(conn=_Conn({}), instrument_class='crypto') == 0.25
    assert bz.benchmark_max_nav_frac(conn=_Conn({}), instrument_class='equity') == 1.0
    assert bz.benchmark_max_nav_frac(conn=_Conn({})) == 1.0
    store = {bz.MAX_NAV_FRAC_KEY: '0.8'}
    assert bz.benchmark_max_nav_frac(conn=_Conn(store)) == 0.8
    # the equity key never leaks into the crypto class
    assert bz.benchmark_max_nav_frac(conn=_Conn(store), instrument_class='crypto') == 0.25
    assert bz.benchmark_max_nav_frac(conn=_Conn({bz.MAX_NAV_FRAC_KEY_CRYPTO: '0.1'}),
                                     instrument_class='crypto') == 0.1
    assert bz.benchmark_max_nav_frac(conn=_Conn({bz.MAX_NAV_FRAC_KEY_CRYPTO: 'junk'}),
                                     instrument_class='crypto') == 0.25
    assert bz.crypto_gross_max_nav_frac(conn=_Conn({})) == 0.50
    assert bz.crypto_gross_max_nav_frac(conn=_Conn({bz.CRYPTO_GROSS_MAX_NAV_FRAC_KEY: '0.4'})) == 0.4
    assert bz.crypto_gross_max_nav_frac(conn=_Conn({bz.CRYPTO_GROSS_MAX_NAV_FRAC_KEY: '-1'})) == 0.50


# --- strategy_weights: crypto sleeve floored at zero -------------------------

class _WCur:
    def __init__(self, sink): self.sink = sink
    def execute(self, sql, params=None):
        if 'INSERT INTO strategy_weights_by_regime' in sql:
            self.sink.append(params)
    def fetchone(self): return None
    def fetchall(self): return []
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _WConn:
    def __init__(self): self.inserts = []
    def cursor(self, *a, **k): return _WCur(self.inserts)
    def commit(self): pass
    def close(self): pass


def test_sleeve_weight_floors_at_zero(monkeypatch):
    # pure: a crypto sleeve with S_m < 0 is weighted 0 (overridden, NOT the positive backtest value)
    eff, ov = sw._benchmark_sleeve_effective_sharpe('S_beta_btc', 'HIGH_VOL', 0.9, {'S_beta_btc'},
                                                    {'HIGH_VOL': -0.4}, floor_zero=True)
    assert (eff, ov) == (0.0, True)
    # equity semantics untouched: a negative S_m passes through
    assert sw._benchmark_sleeve_effective_sharpe('S_beta_spy', 'HIGH_VOL', 0.9, {'S_beta_spy'},
                                                 {'HIGH_VOL': -0.4}) == (-0.4, True)

    # rebuild(): each sleeve resolves its benchmark + class from registry parameters
    conn = _WConn()
    monkeypatch.delenv('OPENCLAW_AUTO_DEMOTE', raising=False)
    monkeypatch.delenv(sw.CADENCE_WEIGHT_NORM_ENV, raising=False)
    monkeypatch.setattr(sw, '_db', lambda: conn)
    monkeypatch.setattr(sw, '_load_active_strategies', lambda c: [
        {'strategy_id': 'S_beta_spy', 'eligible_regimes': ['LOW_VOL', 'HIGH_VOL'], 'cadence_days': 21.0},
        {'strategy_id': 'S_beta_btc', 'eligible_regimes': ['LOW_VOL', 'HIGH_VOL'], 'cadence_days': 21.0},
    ])
    monkeypatch.setattr(sw, '_load_backtest_sharpe', lambda c, sids: {
        (s, R): {'bt_sharpe': 0.9, 'bt_n': 300, 'avg_holding_days': 10.0}
        for s in ('S_beta_spy', 'S_beta_btc') for R in ('LOW_VOL', 'HIGH_VOL')})
    monkeypatch.setattr(sw, '_apply_regime_agnostic_override', lambda c, bt, active: None)
    monkeypatch.setattr(sw, '_load_regime_by_date', lambda: {})
    monkeypatch.setattr(sw, '_load_live_sharpe', lambda c, sids, rbd: {})
    monkeypatch.setattr(sw, '_load_oue_by_strategy_regime', lambda c, sids: {})
    monkeypatch.setattr(sw, 'load_benchmark_sleeve_ids', lambda conn=None: {'S_beta_spy', 'S_beta_btc'})
    monkeypatch.setattr(sw, 'load_benchmark_sleeve_params', lambda conn=None: {
        'S_beta_spy': {'benchmark_sleeve': True},
        'S_beta_btc': {'benchmark_sleeve': True, 'instrument_class': 'crypto',
                       'benchmark_ticker': 'BTC-USD'}})
    calls = []

    def fake_s_m(conn, regimes, *, benchmark='SPY'):
        calls.append((benchmark, tuple(regimes)))
        return {'SPY': {'LOW_VOL': 0.8, 'HIGH_VOL': -0.2},
                'BTC-USD': {'LOW_VOL': 1.3, 'HIGH_VOL': -0.4}}[benchmark]
    monkeypatch.setattr(sw, '_load_benchmark_regime_sharpe', fake_s_m)
    sw.rebuild(trigger='test')
    assert sorted(calls) == [('BTC-USD', ('HIGH_VOL', 'LOW_VOL')), ('SPY', ('HIGH_VOL', 'LOW_VOL'))]
    rows = {(p[0], p[1]): p[7] for p in conn.inserts}
    assert rows[('S_beta_btc', 'LOW_VOL')] == 1.3
    assert rows[('S_beta_spy', 'LOW_VOL')] == 0.8
    # floored to 0 → unsizeable → no row (never the positive backtest 0.9)
    assert ('S_beta_btc', 'HIGH_VOL') not in rows
    assert ('S_beta_spy', 'HIGH_VOL') not in rows   # SPY: -0.2 passes through, also unsizeable


def test_sleeve_params_loader_and_resolution():
    from execution import benchmark_sleeve as bs

    class _PCur(_WCur):
        def __init__(self, rows, fail): self.rows, self.fail = rows, fail
        def execute(self, sql, params=None):
            if self.fail:
                raise RuntimeError('db down')
        def fetchall(self): return self.rows

    class _PConn(_WConn):
        def __init__(self, rows, fail=False): self.rows, self.fail = rows, fail
        def cursor(self, *a, **k): return _PCur(self.rows, self.fail)

    rows = [('S_beta_spy', {'benchmark_sleeve': True}),
            ('S_beta_btc', json.dumps({'benchmark_sleeve': True, 'instrument_class': 'crypto',
                                       'benchmark_ticker': 'BTC-USD'}))]
    p = bs.load_benchmark_sleeve_params(conn=_PConn(rows))
    assert bs.sleeve_benchmark(p['S_beta_spy']) == ('SPY', 'equity')
    assert bs.sleeve_benchmark(p['S_beta_btc']) == ('BTC-USD', 'crypto')
    assert bs.sleeve_benchmark(None) == ('SPY', 'equity')
    assert bs.sleeve_benchmark({'instrument_class': 'crypto'}) == ('BTC-USD', 'crypto')
    assert bs.load_benchmark_sleeve_params(conn=_PConn(rows, fail=True)) == {}


def test_sleeve_benchmark_infers_class_from_ticker():
    from execution import benchmark_sleeve as bs
    assert bs.sleeve_benchmark({'benchmark_sleeve': True, 'benchmark_ticker': 'BTC-USD'}) == ('BTC-USD', 'crypto')
    assert bs.sleeve_benchmark({'benchmark_sleeve': True, 'benchmark_ticker': 'SPY'}) == ('SPY', 'equity')
    # an explicit class always wins
    assert bs.sleeve_benchmark({'benchmark_ticker': 'BTC-USD', 'instrument_class': 'equity'}) == ('BTC-USD', 'equity')
