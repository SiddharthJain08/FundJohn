"""Benchmark sleeves are weighted on S_m, not on their own backtest sleeve.

Operator ruling 2026-08-30: the beta sleeve must BE the buy-and-hold SPY
benchmark. Its backtest sleeves equal S_m exactly before cost (verified from
run 540e3def's trades) and sit 0.10–0.13 below it only because the engine
charges a spread on every one of the 2,604 lot round trips, which a genuine
buy-and-hold never pays. So strategy_weights persists effective_sharpe :=
S_m[regime] (the forward H=1 excess Sharpe the sizer's hurdle already uses)
for every registry benchmark sleeve; bt_sharpe keeps the backtest value so
the dashboard still shows both. Fail-open: no S_m for a regime -> the
backtest value stands (logged).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from execution import strategy_weights as sw


# --- pure override --------------------------------------------------------

def test_sleeve_effective_sharpe_is_benchmark_sharpe():
    eff, overridden = sw._benchmark_sleeve_effective_sharpe(
        'S_beta_spy', 'LOW_VOL', 0.674, {'S_beta_spy'}, {'LOW_VOL': 0.805})
    assert eff == 0.805
    assert overridden is True


def test_sleeve_keeps_backtest_sharpe_when_s_m_unavailable():
    eff, overridden = sw._benchmark_sleeve_effective_sharpe(
        'S_beta_spy', 'CRISIS', 1.488, {'S_beta_spy'}, {'LOW_VOL': 0.805, 'CRISIS': None})
    assert eff == 1.488
    assert overridden is False


def test_alpha_strategy_is_never_overridden():
    eff, overridden = sw._benchmark_sleeve_effective_sharpe(
        'S_alpha', 'LOW_VOL', 1.57, {'S_beta_spy'}, {'LOW_VOL': 0.805})
    assert eff == 1.57
    assert overridden is False


def test_no_sleeves_is_a_no_op():
    eff, overridden = sw._benchmark_sleeve_effective_sharpe(
        'S_beta_spy', 'LOW_VOL', 0.674, set(), {'LOW_VOL': 0.805})
    assert eff == 0.674
    assert overridden is False


# --- rebuild() persists the override ----------------------------------------

class _Cur:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=None):
        if 'INSERT INTO strategy_weights_by_regime' in sql:
            self.sink.append(params)

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self):
        self.inserts = []

    def cursor(self, *a, **k):
        return _Cur(self.inserts)

    def commit(self):
        pass

    def close(self):
        pass


def _wire(monkeypatch, conn, *, bench_ids, s_m):
    monkeypatch.delenv('OPENCLAW_AUTO_DEMOTE', raising=False)
    monkeypatch.delenv(sw.CADENCE_WEIGHT_NORM_ENV, raising=False)
    monkeypatch.setattr(sw, '_db', lambda: conn)
    monkeypatch.setattr(sw, '_load_active_strategies', lambda c: [
        {'strategy_id': 'S_beta_spy', 'eligible_regimes': ['LOW_VOL', 'CRISIS'], 'cadence_days': 21.0},
        {'strategy_id': 'S_alpha',    'eligible_regimes': ['LOW_VOL'],           'cadence_days': 5.0},
    ])
    monkeypatch.setattr(sw, '_load_backtest_sharpe', lambda c, sids: {
        ('S_beta_spy', 'LOW_VOL'): {'bt_sharpe': 0.674, 'bt_n': 920, 'avg_holding_days': 14.0},
        ('S_beta_spy', 'CRISIS'):  {'bt_sharpe': 1.488, 'bt_n': 150, 'avg_holding_days': 3.0},
        ('S_alpha',    'LOW_VOL'): {'bt_sharpe': 1.570, 'bt_n': 400, 'avg_holding_days': 5.0},
    })
    monkeypatch.setattr(sw, '_apply_regime_agnostic_override', lambda c, bt, active: None)
    monkeypatch.setattr(sw, '_load_regime_by_date', lambda: {})
    monkeypatch.setattr(sw, '_load_live_sharpe', lambda c, sids, rbd: {})
    monkeypatch.setattr(sw, '_load_oue_by_strategy_regime', lambda c, sids: {})
    monkeypatch.setattr(sw, 'load_benchmark_sleeve_ids', lambda conn=None: set(bench_ids))
    monkeypatch.setattr(sw, '_load_benchmark_regime_sharpe', lambda conn, regimes: dict(s_m))


def _rows_by_key(conn):
    # INSERT param order: (strategy_id, regime_state, cadence_days, bt_sharpe, bt_n,
    #                      live_sharpe, live_n, effective_sharpe, weight, daily_weight, trigger, ...)
    return {(p[0], p[1]): {'bt_sharpe': p[3], 'effective_sharpe': p[7], 'weight': p[8], 'daily_weight': p[9]}
            for p in conn.inserts}


def test_rebuild_persists_s_m_for_sleeve_and_backtest_for_alpha(monkeypatch):
    conn = _Conn()
    _wire(monkeypatch, conn, bench_ids={'S_beta_spy'}, s_m={'LOW_VOL': 0.805, 'CRISIS': 1.539})
    sw.rebuild(trigger='test')
    rows = _rows_by_key(conn)
    sleeve = rows[('S_beta_spy', 'LOW_VOL')]
    assert sleeve['effective_sharpe'] == 0.805
    assert sleeve['weight'] == 0.805 and sleeve['daily_weight'] == 0.805
    assert sleeve['bt_sharpe'] == 0.674          # backtest value still recorded
    assert rows[('S_beta_spy', 'CRISIS')]['effective_sharpe'] == 1.539
    alpha = rows[('S_alpha', 'LOW_VOL')]
    assert alpha['effective_sharpe'] == 1.570 and alpha['bt_sharpe'] == 1.570


def test_rebuild_falls_back_to_backtest_when_s_m_missing(monkeypatch):
    conn = _Conn()
    _wire(monkeypatch, conn, bench_ids={'S_beta_spy'}, s_m={'LOW_VOL': 0.805})   # no CRISIS
    sw.rebuild(trigger='test')
    rows = _rows_by_key(conn)
    assert rows[('S_beta_spy', 'CRISIS')]['effective_sharpe'] == 1.488


def test_rebuild_without_sleeves_never_calls_s_m_loader(monkeypatch):
    conn = _Conn()
    _wire(monkeypatch, conn, bench_ids=set(), s_m={})
    calls = []
    monkeypatch.setattr(sw, '_load_benchmark_regime_sharpe',
                        lambda conn, regimes: calls.append(regimes) or {})
    sw.rebuild(trigger='test')
    assert calls == []
    rows = _rows_by_key(conn)
    assert rows[('S_beta_spy', 'LOW_VOL')]['effective_sharpe'] == 0.674
