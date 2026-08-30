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


def _grid(**cols):
    """{regime: {H: v}} from per-regime lists over the grid (1,2,3,5,10,21)."""
    hs = (1, 2, 3, 5, 10, 21)
    return {r: dict(zip(hs, vals)) for r, vals in cols.items()}


def test_computes_persists_schema2_and_reuses_cache():
    calls = []
    def compute(start, end, benchmark='SPY', min_obs=40):
        calls.append((start, end, benchmark))
        return _grid(LOW_VOL=[0.80, 0.60, 0.51, 0.57, 0.34, 0.26],
                     TRANSITIONING=[0.41, 0.25, 0.29, 0.42, 0.52, 0.20],
                     HIGH_VOL=[0.49, 0.59, 0.87, 1.11, 0.63, 0.73],
                     CRISIS=[None] * 6)
    store = {}
    conn = _Conn(store)
    v = bz.regime_benchmark_sharpe_for_sizing('LOW_VOL', date(2026, 8, 31), conn=conn, compute=compute)
    assert v == 0.80 and calls == [('2016-04-11', '2026-08-31', 'SPY')]
    payload = json.loads(store[bz.CONFIG_KEY])
    assert payload['schema'] == bz.CACHE_SCHEMA == 2
    assert payload['horizons'] == [1, 2, 3, 5, 10, 21]
    assert payload['by_regime']['LOW_VOL']['5'] == 0.57 and payload['by_regime']['CRISIS']['1'] is None
    # same day: cache hit, no recompute; explicit horizon selects a column
    assert bz.regime_benchmark_sharpe_for_sizing('HIGH_VOL', date(2026, 8, 31), conn=conn,
                                                 compute=compute, horizon=5) == 1.11
    assert len(calls) == 1
    # a new day recomputes
    bz.regime_benchmark_sharpe_for_sizing('LOW_VOL', date(2026, 9, 1), conn=conn, compute=compute)
    assert len(calls) == 2


def test_schema1_cache_is_a_miss_and_is_rewritten():
    calls = []
    def compute(start, end, benchmark='SPY', min_obs=40):
        calls.append(1)
        return _grid(LOW_VOL=[0.80] * 6, TRANSITIONING=[0.41] * 6, HIGH_VOL=[0.49] * 6, CRISIS=[1.54] * 6)
    store = {bz.CONFIG_KEY: json.dumps({'as_of': '2026-08-31', 'benchmark': 'SPY', 'start': '2016-04-11',
                                        'by_regime': {'LOW_VOL': 2.01, 'TRANSITIONING': 0.6,
                                                      'HIGH_VOL': 0.6, 'CRISIS': 0.73}})}
    conn = _Conn(store)
    assert bz.regime_benchmark_sharpe_for_sizing('LOW_VOL', date(2026, 8, 31), conn=conn, compute=compute) == 0.80
    assert calls == [1]
    assert json.loads(store[bz.CONFIG_KEY])['schema'] == 2


def test_horizon_config_selects_column_and_falls_back_to_1():
    compute = lambda *a, **k: _grid(LOW_VOL=[0.80, 0.60, 0.51, 0.57, 0.34, 0.26], TRANSITIONING=[None] * 6,
                                    HIGH_VOL=[None] * 6, CRISIS=[None] * 6)
    for raw, expected in [('5', 0.57), ('21', 0.26), (None, 0.80), ('7', 0.80), ('abc', 0.80), ('1.0', 0.80)]:
        store = {}
        if raw is not None:
            store[bz.HORIZON_KEY] = raw
        conn = _Conn(store)
        assert bz.load_benchmark_horizon(conn=conn) == (int(float(raw)) if raw in ('5', '21', '1.0') else 1)
        assert bz.regime_benchmark_sharpe_for_sizing('LOW_VOL', date(2026, 8, 31), conn=conn, compute=compute) == expected


def test_thin_regime_and_failures_return_none():
    conn = _Conn({})
    assert bz.regime_benchmark_sharpe_for_sizing('CRISIS', date(2026, 8, 29), conn=conn,
                                                 compute=lambda *a, **k: _grid(CRISIS=[None] * 6)) is None
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


def test_shadow_line_carries_horizon():
    before = {'SPY': 2.0, 'ZZTA': 2.6, 'ZZTB': 1.5}
    after, dropped = bz.apply_benchmark_hurdle(before, 0.8, {'SPY'})
    line = bz.shadow_line('LOW_VOL', 0.8, before, after, dropped, {'SPY'}, lam_nav=100_000.0, h=1)
    assert line.startswith("bench_sizing.shadow[LOW_VOL]: S_m=0.80 h=1 bench=['SPY']")
    # h omitted -> byte-identical to the pre-amendment format
    assert bz.shadow_line('LOW_VOL', 0.8, before, after, dropped, {'SPY'}, 100_000.0).startswith(
        "bench_sizing.shadow[LOW_VOL]: S_m=0.80 bench=['SPY']")


# ── apply_beta_budget (spec 2026-08-30 §3.1) ────────────────────────────────
def test_beta_budget_conserves_conviction_and_redirects_to_benchmark():
    import pytest
    before = {'SPY': 2.0, 'ZZTA': 2.6, 'ZZTB': 1.5, 'ZZTC': -2.5, 'ZZTD': -1.0}
    hurdled, dropped = bz.apply_benchmark_hurdle(before, 2.0, {'SPY'})
    out, pool = bz.apply_beta_budget(before, hurdled, 2.0, {'SPY'})
    # survivors hand exactly S_m, dropped hand their whole |S|: 2.0 + 1.5 + 2.0 + 1.0
    assert pool == pytest.approx(6.5)
    assert out['SPY'] == pytest.approx(2.0 + 6.5)          # own raw weight + pool
    assert out['ZZTA'] == pytest.approx(0.6) and out['ZZTC'] == pytest.approx(-0.5)
    assert 'ZZTB' not in out and 'ZZTD' not in out
    assert sum(abs(v) for v in out.values()) == pytest.approx(sum(abs(v) for v in before.values()))


def test_beta_budget_none_s_m_or_no_bench_is_identity():
    before = {'SPY': 2.0, 'ZZTA': 2.6}
    hurdled, _ = bz.apply_benchmark_hurdle(before, None, {'SPY'})
    out, pool = bz.apply_beta_budget(before, hurdled, None, {'SPY'})
    assert out == hurdled and out is not hurdled and pool == 0.0
    hurdled2, _ = bz.apply_benchmark_hurdle(before, 2.0, set())
    out2, pool2 = bz.apply_beta_budget(before, hurdled2, 2.0, set())
    assert out2 == hurdled2 and pool2 == 0.0


def test_beta_budget_splits_pool_across_benchmark_tickers_and_keeps_inputs():
    import pytest
    before = {'SPY': 2.0, 'IVV': 1.0, 'ZZTA': 3.0}
    hurdled, _ = bz.apply_benchmark_hurdle(before, 1.0, {'SPY', 'IVV'})
    snap_b, snap_h = dict(before), dict(hurdled)
    out, pool = bz.apply_beta_budget(before, hurdled, 1.0, {'SPY', 'IVV'})
    assert pool == pytest.approx(1.0)
    assert out['SPY'] == pytest.approx(2.5) and out['IVV'] == pytest.approx(1.5)
    assert before == snap_b and hurdled == snap_h


def test_beta_budget_flag_and_nav_frac_reader(monkeypatch):
    monkeypatch.setenv(bz.BETA_BUDGET_ENV, '0'); assert bz.beta_budget_enabled() is False
    monkeypatch.setenv(bz.BETA_BUDGET_ENV, '1'); assert bz.beta_budget_enabled() is True
    store = {}
    assert bz.benchmark_max_nav_frac(conn=_Conn(store)) == 1.0          # unset -> default
    store[bz.MAX_NAV_FRAC_KEY] = '0.5'
    assert bz.benchmark_max_nav_frac(conn=_Conn(store)) == 0.5
    store[bz.MAX_NAV_FRAC_KEY] = 'garbage'
    assert bz.benchmark_max_nav_frac(conn=_Conn(store)) == 1.0          # garbage -> default
    store[bz.MAX_NAV_FRAC_KEY] = '-2'
    assert bz.benchmark_max_nav_frac(conn=_Conn(store)) == 1.0          # non-positive -> default


def test_shadow_line_reports_beta_budget_fields():
    before = {'SPY': 2.0, 'ZZTA': 2.6, 'ZZTB': 1.5}
    after, dropped = bz.apply_benchmark_hurdle(before, 2.0, {'SPY'})
    budgeted, pool = bz.apply_beta_budget(before, after, 2.0, {'SPY'})
    line = bz.shadow_line('LOW_VOL', 2.0, before, after, dropped, {'SPY'}, lam_nav=100_000.0, h=1,
                          budgeted=budgeted, beta_pool=pool, budget_mode='shadow')
    # pool = 2.0 (ZZTA) + 1.5 (ZZTB) = 3.5; budgeted SPY = 5.5 of Σ 6.1
    assert ' beta_budget=shadow pool=3.5 beta_share_budget=0.902 beta_usd_budget=90164 ' in line + ' '
    assert bz.shadow_line('LOW_VOL', 2.0, before, after, dropped, {'SPY'}, 100_000.0, h=1,
                          budgeted=budgeted, beta_pool=pool, budget_mode='apply').count('beta_budget=apply') == 1
    # budgeted omitted -> byte-identical to the pre-budget format
    assert 'beta_budget=' not in bz.shadow_line('LOW_VOL', 2.0, before, after, dropped, {'SPY'}, 100_000.0, h=1)
