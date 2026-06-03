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


from backtest.backtest_panel import build_hv21_lookup


def test_build_hv21_lookup_returns_callable_with_annualized_vol():
    import numpy as np
    dates = pd.date_range('2025-01-01', periods=60, freq='B')
    px = pd.DataFrame({'ticker':'AAA','date':dates,
                       'close': 100*np.cumprod(1+np.linspace(0.001,0.002,60))})
    hv = build_hv21_lookup(px)
    v = hv('AAA', dates[40].strftime('%Y-%m-%d'))
    assert v is not None and v > 0
    assert hv('MISSING', dates[40].strftime('%Y-%m-%d')) is None


def test_equity_curve_nonflat_benchmark_consistent_anchor():
    trades = [
        {'ticker':'AAA','entry_date':'2025-01-02','pnl_pct':0.10,'holding_days':5},
        {'ticker':'AAA','entry_date':'2025-02-02','pnl_pct':-0.05,'holding_days':5},
    ]
    bench = pd.Series([0.001]*400, index=pd.date_range('2025-01-01', periods=400, freq='D'))
    def regime_for(dates):
        return pd.Series(['LOW_VOL']*len(list(dates)), index=list(dates))
    curve = build_equity_curve(trades, bench_daily_ret=bench, regime_series_fn=regime_for, weekly=True)
    assert curve[0]['strat_equity'] == 1.0
    assert curve[0]['spx_equity'] == 1.0          # both anchor to 1.0 at first displayed point
    assert curve[-1]['spx_equity'] != 1.0         # benchmark moves independently (drift)


def test_hv21_asof_no_lookahead():
    import numpy as np
    dates = pd.date_range('2025-01-01', periods=60, freq='B')
    rets = np.concatenate([np.full(44, 0.001), np.array([0.05, -0.05] * 8)[:15]])
    close = 100 * np.cumprod(np.concatenate([[1.0], 1 + rets]))[:60]
    px = pd.DataFrame({'ticker': 'AAA', 'date': dates, 'close': close})
    hv = build_hv21_lookup(px)
    calm = hv('AAA', dates[30].strftime('%Y-%m-%d'))   # inside the constant-return window
    assert calm is not None and calm < 1e-6            # ~0 vol => future volatility NOT leaked in


def test_oue_unknown_regime_bucket_and_invariant():
    def hv(ticker, entry_date):
        return None
    trades = [
        {'ticker':'X','entry_date':'2025-01-02','pnl_pct':0.01,'holding_days':5},          # no entry_regime
        {'ticker':'Y','entry_date':'2025-01-02','pnl_pct':0.01,'holding_days':5,'entry_regime':'LOW_VOL'},
    ]
    overall, by_regime = classify_trades_oue(trades, hv, sigma_gate=2.0)
    assert 'UNKNOWN' in by_regime
    assert by_regime['UNKNOWN'] == {'over':0,'under':0,'expected':1}
    assert sum(overall.values()) == len(trades)


import json as _json
import numpy as _np
import backtest.backtest_panel as _bp


def test_equity_curve_sanitizes_nonfinite_to_none_and_is_json_safe(monkeypatch):
    """Regression: high-frequency strategies can drive cumprod(1+daily_ret) to
    0 (a -100% day from many concurrent trades, more reachable under t+1 fills)
    or to inf (overflow over tens of thousands of steps). Normalizing by a zero
    first point yields NaN/inf, and a bare NaN/Infinity is INVALID JSON that
    aborts the whole panel INSERT. Each non-finite equity point must become
    None so the curve round-trips through json.dumps."""
    dates = pd.date_range('2025-01-06', periods=20, freq='B')  # starts on a Monday

    # daily_ret[0] = -1.0 zeroes the cumulative product at the FIRST sampled
    # point -> normalization divides by 0 -> every strat_equity is non-finite.
    dr = _np.zeros(len(dates)); dr[0] = -1.0
    monkeypatch.setattr(_bp, '_portfolio_daily_returns', lambda trades: (dr, list(dates)))
    bench = pd.Series([0.0] * 60, index=pd.date_range('2025-01-01', periods=60, freq='D'))
    regime_for = lambda d: pd.Series(['LOW_VOL'] * len(list(d)), index=list(d))

    curve = _bp.build_equity_curve([{'x': 1}], bench_daily_ret=bench,
                                   regime_series_fn=regime_for, weekly=True)
    assert curve, "curve should still be produced"
    assert all(p['strat_equity'] is None for p in curve), "non-finite -> None"
    # No NaN/Infinity slips into any float field.
    for p in curve:
        for k in ('strat_equity', 'spx_equity'):
            v = p[k]
            assert v is None or (isinstance(v, float) and _np.isfinite(v))
    # The real bug: this must not raise / must not emit a bare NaN token.
    dumped = _json.dumps(curve)
    assert 'NaN' not in dumped and 'Infinity' not in dumped
