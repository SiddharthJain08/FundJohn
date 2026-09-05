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


# ── Final fix wave 2026-09-05, F3: spec C.4 wants a shadow line from EVERY site ──
def _noisy_series(start, daily, n, first='2026-06-01'):
    """Alternating daily*1.5 / daily*0.5 so the trailing-20 std is non-zero —
    the same shape tests/execution/test_bench_realized.py drives compute() with."""
    import datetime as dt
    d0 = dt.date.fromisoformat(first); out = {}; v = start; k = 0
    for i in range(n):
        d = d0 + dt.timedelta(days=i)
        if d.weekday() < 5:
            out[d.isoformat()] = v
            v *= (1 + daily * (1.5 if k % 2 == 0 else 0.5)); k += 1
    return out


def test_bench_realized_emits_rf_shadow_per_sharpe(monkeypatch, caplog):
    import logging
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'const')
    from execution import bench_realized as br
    nav = _noisy_series(100_000.0, -0.002, 120)
    spy = _noisy_series(500.0, +0.001, 120)
    with caplog.at_level(logging.INFO, logger='execution.bench_realized'):
        st = br.compute(nav, spy, max(nav), anchor='2026-06-23')
    assert st['book_sharpe_20d'] is not None and st['spy_sharpe_20d'] is not None
    lines = [r.message for r in caplog.records if '[rf_shadow] site=bench_realized' in r.message]
    assert len(lines) == 2                       # one per Sharpe actually computed: book, SPY
    assert all('const=' in ln and 'macro=' in ln and 'n=20' in ln for ln in lines)


def test_bench_realized_emits_no_shadow_line_below_min_obs(monkeypatch, caplog):
    """No Sharpe, no line — the runbook counts lines, so a thin window must not
    inflate the count."""
    import logging
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'const')
    from execution import bench_realized as br
    with caplog.at_level(logging.INFO, logger='execution.bench_realized'):
        assert br._sharpe([0.001] * 5) is None
    assert not [r for r in caplog.records if '[rf_shadow]' in r.message]


def test_benchmark_baseline_emits_one_rf_shadow_line_per_regime(monkeypatch, caplog, tmp_path):
    """One line per regime, DEFAULT_HORIZON only — the column the sizer reads.
    Synthetic ZZT_SPY parquets under tmp_path (never data/master/*)."""
    import logging
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'const')
    from backtest import benchmark_baseline as bb
    dates = pd.bdate_range('2025-01-02', periods=252)
    # Six alternating 42-day LOW_VOL / HIGH_VOL blocks; no TRANSITIONING/CRISIS days.
    regimes = [('LOW_VOL' if (i // 42) % 2 == 0 else 'HIGH_VOL') for i in range(len(dates))]
    price, closes = 100.0, []
    for i in range(len(dates)):
        closes.append(price)
        price *= (1 + (0.01 if i % 2 else -0.008))
    reg_p, px_p = tmp_path / 'regimes.parquet', tmp_path / 'prices.parquet'
    pd.DataFrame({'date': [d.date() for d in dates], 'vix': 15.0, 'vix_smoothed': 15.0,
                  'regime': regimes}).to_parquet(reg_p)
    pd.DataFrame({'ticker': 'ZZT_SPY', 'date': [d.strftime('%Y-%m-%d') for d in dates],
                  'open': closes, 'high': closes, 'low': closes, 'close': closes,
                  'volume': 1000.0, 'vwap': closes, 'transactions': 10.0,
                  'source': 'synthetic'}).to_parquet(px_p)
    monkeypatch.setattr(bb, 'REGIMES_PARQUET', str(reg_p))
    monkeypatch.setattr(bb, 'PRICES_PARQUET', str(px_p))
    with caplog.at_level(logging.INFO, logger='backtest.benchmark_baseline'):
        grid = bb.regime_benchmark_sharpe_by_horizon(dates[0], dates[-1], benchmark='ZZT_SPY', min_obs=40)
    assert grid and grid['LOW_VOL'][bb.DEFAULT_HORIZON] is not None
    lines = [r.message for r in caplog.records if '[rf_shadow] site=benchmark_baseline' in r.message]
    assert len(lines) == len(bb.CANONICAL_REGIMES)
    assert all(f'h={bb.DEFAULT_HORIZON}' in ln for ln in lines)
    low = next(ln for ln in lines if 'regime=LOW_VOL' in ln)
    assert 'const=' in low and 'macro=' in low and 'n=' in low
    # An empty regime must print n=0 with n/a Sharpes, never crash on f'{None:.3f}'.
    crisis = next(ln for ln in lines if 'regime=CRISIS' in ln)
    assert 'const=n/a' in crisis and 'macro=n/a' in crisis and 'n=0' in crisis


def test_benchmark_baseline_no_shadow_line_without_default_horizon(monkeypatch, caplog, tmp_path):
    """A caller asking only for h=5 gets no line — the site emits the sizer's
    column or nothing."""
    import logging
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'const')
    from backtest import benchmark_baseline as bb
    monkeypatch.setattr(bb, 'load_regime_tags', lambda a, b: {'2025-01-02': 'LOW_VOL', '2025-01-03': 'LOW_VOL'})
    monkeypatch.setattr(bb, 'load_benchmark_closes', lambda a, b, c: {'2025-01-02': 100.0, '2025-01-03': 101.0})
    with caplog.at_level(logging.INFO, logger='backtest.benchmark_baseline'):
        bb.regime_benchmark_sharpe_by_horizon('2025-01-02', '2025-01-03', horizons=(5,))
    assert not [r for r in caplog.records if '[rf_shadow]' in r.message]
