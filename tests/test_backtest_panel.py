import sys, math
from pathlib import Path
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
