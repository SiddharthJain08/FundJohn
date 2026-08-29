"""Spec §2.5: per-ticker hurdle S_adj − S_m, benchmark ticker exempt, shorts
symmetric (D7), S_m=None passthrough; S_m provider cache; shadow line."""
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import benchmark_sizing as bz  # noqa: E402


# ── apply_benchmark_hurdle ───────────────────────────────────────────────────
def test_hurdle_subtracts_and_drops():
    import pytest
    w = {'SPY': 2.0, 'ZZTA': 2.6, 'ZZTB': 1.5, 'ZZTC': -2.5, 'ZZTD': -1.0}
    out, dropped = bz.apply_benchmark_hurdle(w, 2.0, {'SPY'})
    assert set(out) == {'SPY', 'ZZTA', 'ZZTC'}
    assert out['SPY'] == 2.0                       # benchmark ticker: no subtraction
    assert out['ZZTA'] == pytest.approx(0.6)       # 2.6 − 2.0
    assert out['ZZTC'] == pytest.approx(-0.5)      # short: |−2.5| − 2.0, sign kept (D7)
    assert sorted(dropped) == ['ZZTB', 'ZZTD']


def test_exact_tie_is_dropped():
    out, dropped = bz.apply_benchmark_hurdle({'ZZTA': 2.0}, 2.0, set())
    assert out == {} and dropped == ['ZZTA']


def test_none_s_m_is_passthrough():
    w = {'ZZTA': 0.3, 'SPY': 2.0}
    out, dropped = bz.apply_benchmark_hurdle(w, None, {'SPY'})
    assert out == w and dropped == [] and out is not w


def test_input_not_mutated():
    w = {'ZZTA': 2.6}
    bz.apply_benchmark_hurdle(w, 2.0, set())
    assert w == {'ZZTA': 2.6}


# ── regime_benchmark_sharpe_for_sizing ───────────────────────────────────────
class _Cur:
    def __init__(self, store): self.store = store; self.last = None
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        self.last = (sql, params)
        if sql.lstrip().upper().startswith('SELECT'):
            self._row = (self.store.get(params[0]),) if self.store.get(params[0]) is not None else None
        else:
            key, value = params[0], params[1]
            self.store[key] = value
    def fetchone(self): return self._row


class _Conn:
    def __init__(self, store): self.store = store; self.c = _Cur(store); self.commits = 0
    def cursor(self): return self.c
    def commit(self): self.commits += 1
    def close(self): pass


def test_computes_persists_and_reuses_cache():
    calls = []
    def compute(start, end, benchmark='SPY', min_obs=40):
        calls.append((start, end, benchmark))
        return {'LOW_VOL': 2.03, 'TRANSITIONING': 0.55, 'HIGH_VOL': 0.6, 'CRISIS': None}
    store = {}
    conn = _Conn(store)
    v = bz.regime_benchmark_sharpe_for_sizing('LOW_VOL', date(2026, 8, 29), conn=conn, compute=compute)
    assert v == 2.03 and calls == [('2016-04-11', '2026-08-29', 'SPY')]
    payload = json.loads(store[bz.CONFIG_KEY])
    assert payload['as_of'] == '2026-08-29' and payload['by_regime']['CRISIS'] is None
    # second call same day: cache hit, no recompute
    v2 = bz.regime_benchmark_sharpe_for_sizing('HIGH_VOL', date(2026, 8, 29), conn=conn, compute=compute)
    assert v2 == 0.6 and len(calls) == 1
    # a new day recomputes
    bz.regime_benchmark_sharpe_for_sizing('LOW_VOL', date(2026, 8, 30), conn=conn, compute=compute)
    assert len(calls) == 2


def test_thin_regime_and_failures_return_none():
    conn = _Conn({})
    assert bz.regime_benchmark_sharpe_for_sizing('CRISIS', date(2026, 8, 29), conn=conn,
                                                 compute=lambda *a, **k: {'CRISIS': None}) is None
    assert bz.regime_benchmark_sharpe_for_sizing('LOW_VOL', date(2026, 8, 29), conn=conn,
                                                 compute=lambda *a, **k: {}) is None
    def boom(*a, **k): raise RuntimeError('parquet gone')
    assert bz.regime_benchmark_sharpe_for_sizing('LOW_VOL', date(2026, 8, 29), conn=conn, compute=boom) is None


# ── shadow_line ──────────────────────────────────────────────────────────────
def test_shadow_line_reports_shares_and_moves():
    before = {'SPY': 2.0, 'ZZTA': 2.6, 'ZZTB': 1.5}
    after, dropped = bz.apply_benchmark_hurdle(before, 2.0, {'SPY'})
    line = bz.shadow_line('LOW_VOL', 2.0, before, after, dropped, {'SPY'}, lam_nav=100_000.0)
    assert line.startswith("bench_sizing.shadow[LOW_VOL]: S_m=2.00 bench=['SPY'] dropped=1/3")
    assert 'beta_share_before=0.328 beta_share_after=0.769' in line
    assert 'ZZTB' in line and 'top_moves=' in line
    assert bz.shadow_line('LOW_VOL', None, before, before, [], {'SPY'}, 100_000.0).startswith(
        'bench_sizing.shadow[LOW_VOL]: S_m=None')
    assert bz.shadow_line('LOW_VOL', 2.0, before, after, dropped, {'SPY'}, 100_000.0, mode='apply').startswith(
        'bench_sizing.apply[')
