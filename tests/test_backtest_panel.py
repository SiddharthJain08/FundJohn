import sys, math
from pathlib import Path
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from backtest.backtest_panel import classify_trades_oue


def test_oue_counts_invariant_and_classification():
    def hv(ticker, entry_date):
        return 0.20
    trades = [
        {'ticker':'AAA','entry_date':'2025-01-02','pnl_pct': 0.20,'holding_days':21,'entry_regime':'LOW_VOL'},
        {'ticker':'AAA','entry_date':'2025-01-02','pnl_pct':-0.20,'holding_days':21,'entry_regime':'LOW_VOL'},
        {'ticker':'AAA','entry_date':'2025-01-02','pnl_pct': 0.02,'holding_days':21,'entry_regime':'CRISIS'},
    ]
    overall, by_regime = classify_trades_oue(trades, hv, sigma_gate=2.0)
    assert overall == {'over':1,'under':1,'expected':1}
    assert sum(overall.values()) == len(trades)
    assert by_regime['LOW_VOL'] == {'over':1,'under':1,'expected':0}
    assert by_regime['CRISIS']  == {'over':0,'under':0,'expected':1}


def test_oue_missing_hv_falls_back_to_expected():
    def hv(ticker, entry_date):
        return None
    trades = [{'ticker':'X','entry_date':'2025-01-02','pnl_pct':0.5,'holding_days':10,'entry_regime':'HIGH_VOL'}]
    overall, by_regime = classify_trades_oue(trades, hv, sigma_gate=2.0)
    assert overall == {'over':0,'under':0,'expected':1}


from backtest.backtest_panel import effective_sharpe, build_equity_curve


def test_effective_sharpe():
    assert math.isclose(effective_sharpe(2.0, 4.0), 1.0)
    assert math.isclose(effective_sharpe(1.5, 0.0), 1.5)
    assert effective_sharpe(None, 4.0) is None


def test_build_equity_curve_normalizes_and_tags_regime():
    trades = [
        {'ticker':'AAA','entry_date':'2025-01-02','pnl_pct':0.10,'holding_days':5},
        {'ticker':'AAA','entry_date':'2025-02-02','pnl_pct':-0.05,'holding_days':5},
    ]
    bench = pd.Series([0.0]*400,
                      index=pd.date_range('2025-01-01', periods=400, freq='D'))
    def regime_for(dates):
        return pd.Series(['LOW_VOL']*len(list(dates)), index=list(dates))
    curve = build_equity_curve(trades, bench_daily_ret=bench,
                               regime_series_fn=regime_for, weekly=True)
    assert curve, "curve should be non-empty"
    assert curve[0]['strat_equity'] == 1.0
    assert all('date' in p and 'spx_equity' in p and 'regime' in p for p in curve)
    assert all(abs(p['spx_equity'] - 1.0) < 1e-9 for p in curve)
    assert len(curve) <= 60
