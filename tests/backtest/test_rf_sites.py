from __future__ import annotations
import math
import numpy as np
import pandas as pd
import pytest


def test_benchmark_baseline_sharpe_accepts_dates_and_matches_const(monkeypatch):
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'const')
    from backtest import benchmark_baseline as bb
    r = [0.001, -0.002, 0.003, 0.0005, 0.002] * 10
    dates = [d.strftime('%Y-%m-%d') for d in pd.bdate_range('2025-01-02', periods=50)]
    legacy = (np.mean(r) - 0.05 / 252) / np.std(r, ddof=1) * math.sqrt(252)
    assert bb._excess_sharpe(r, 40, dates=dates) == pytest.approx(legacy, rel=1e-9)
    assert bb.RISK_FREE_ANNUAL == 0.05


def test_aggregate_metrics_emits_rf_shadow(monkeypatch):
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'const')
    from backtest.unified_backtest import aggregate_metrics
    trades = []
    for i in range(30):
        d = pd.Timestamp('2025-03-03') + pd.tseries.offsets.BDay(i)
        trades.append({'ticker': 'AAA', 'pnl_pct': 0.01 * (1 if i % 3 else -1), 'holding_days': 1,
                       'daily_marks': [(d.strftime('%Y-%m-%d'), 0.01 * (1 if i % 3 else -1))]})
    m = aggregate_metrics(trades)
    assert m['sharpe'] is not None
    assert set(m['rf_shadow']) == {'const', 'macro', 'rf_mean_annual', 'n'}
    assert m['rf_shadow']['const'] == pytest.approx(m['sharpe'])


def test_bench_realized_sharpe_signature(monkeypatch):
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'const')
    from execution import bench_realized as br
    r = [0.001, -0.001, 0.002, 0.0, 0.0015] * 5
    dates = [d.strftime('%Y-%m-%d') for d in pd.bdate_range('2026-08-01', periods=25)]
    legacy = (np.mean(r) - 0.05 / 252) / np.std(r, ddof=1) * math.sqrt(252)
    assert br._sharpe(r, dates) == pytest.approx(legacy, rel=1e-9)
    assert br._sharpe(r) == pytest.approx(legacy, rel=1e-9)


def test_options_pricing_rate_asof(monkeypatch, tmp_path):
    from backtest import options_pricing as op, risk_free as rf
    rows = [{'date': d.date(), 'series': 'DGS3MO', 'value': 3.0, 'source': 'fred'} for d in pd.bdate_range('2026-01-01', '2026-12-31')]
    p = tmp_path / 'macro.parquet'; pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv('OPENCLAW_MACRO_PARQUET', str(p)); monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'macro'); rf.clear_cache()
    assert op.bs_price('c', 100, 100, 0.5, 0.2) == pytest.approx(op.bs_price('c', 100, 100, 0.5, 0.2, r=0.04))
    assert op.bs_price('c', 100, 100, 0.5, 0.2, as_of='2026-06-01') == pytest.approx(op.bs_price('c', 100, 100, 0.5, 0.2, r=0.03))
    rf.clear_cache()
