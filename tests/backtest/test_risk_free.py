from __future__ import annotations
import math
import numpy as np
import pandas as pd
import pytest

from backtest import risk_free as rf


@pytest.fixture
def macro(tmp_path, monkeypatch):
    dates = pd.bdate_range('2024-01-01', '2024-12-31')
    rows = [{'date': d.date(), 'series': 'DGS3MO', 'value': 5.0 if d.month < 7 else 4.0, 'source': 'fred'} for d in dates]
    rows += [{'date': d.date(), 'series': 'DGS10', 'value': 4.2, 'source': 'fred'} for d in dates[:5]]
    p = tmp_path / 'macro.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv('OPENCLAW_MACRO_PARQUET', str(p))
    rf.clear_cache()
    yield
    rf.clear_cache()


def _old_sharpe(r):
    r = np.asarray(r, float)
    return float((r.mean() - 0.05 / 252) / r.std(ddof=1) * math.sqrt(252))


def test_const_source_reproduces_the_legacy_formula(monkeypatch):
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'const')
    rng = np.random.default_rng(0)
    r = rng.normal(0.0004, 0.01, 300)
    dates = pd.bdate_range('2024-01-02', periods=300)
    assert rf.excess_sharpe(r, dates) == pytest.approx(_old_sharpe(r), rel=1e-12)
    assert rf.excess_sharpe(r) == pytest.approx(_old_sharpe(r), rel=1e-12)


def test_macro_source_uses_dgs3mo_per_date(macro, monkeypatch):
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'macro')
    assert rf.rf_annual_asof('2024-03-15') == pytest.approx(0.05)
    assert rf.rf_annual_asof('2024-09-15') == pytest.approx(0.04)
    assert rf.rf_annual_asof('2024-07-06') == pytest.approx(0.04)      # Saturday → ffill from Friday 07-05
    daily = rf.rf_daily_for(pd.to_datetime(['2024-03-15', '2024-09-16']))
    assert daily == pytest.approx([0.05 / 252, 0.04 / 252])


def test_excess_sharpe_macro_subtracts_time_varying_rf(macro, monkeypatch):
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'macro')
    dates = pd.bdate_range('2024-06-24', periods=10)     # straddles the 5% → 4% step on 07-01
    r = np.full(10, 0.001)
    r[0] = 0.0011                                        # non-zero variance
    rfd = rf.rf_daily_for(dates)
    expect = float((r - rfd).mean() / r.std(ddof=1) * math.sqrt(252))
    assert rf.excess_sharpe(r, dates) == pytest.approx(expect)


def test_before_series_start_backfills_first_value_and_missing_file_warns(macro, monkeypatch, caplog):
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'macro')
    assert rf.rf_annual_asof('2020-01-01') == pytest.approx(0.05)
    monkeypatch.setenv('OPENCLAW_MACRO_PARQUET', '/nonexistent/macro.parquet')
    rf.clear_cache()
    import logging
    with caplog.at_level(logging.WARNING):
        assert rf.rf_annual_asof('2024-03-15') == pytest.approx(0.05)
    assert any('falling back to constant' in r.message for r in caplog.records)


def test_sharpe_pair_and_shadow_line(macro, monkeypatch):
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'const')
    dates = pd.bdate_range('2024-08-01', periods=40)
    r = np.linspace(-0.01, 0.012, 40)
    pair = rf.sharpe_pair(r, dates)
    assert set(pair) == {'const', 'macro', 'rf_mean_annual', 'n'}
    assert pair['n'] == 40 and pair['rf_mean_annual'] == pytest.approx(0.04)
    line = rf.shadow_line('unit_test', r, dates)
    assert line.startswith('[rf_shadow] site=unit_test const=') and ' macro=' in line and ' n=40 ' in line


def test_degenerate_inputs_return_none():
    assert rf.excess_sharpe([0.01]) is None
    assert rf.excess_sharpe([0.01, 0.01, 0.01]) is None
