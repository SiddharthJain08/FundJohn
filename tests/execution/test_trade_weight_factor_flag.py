"""D3 (2026-08-29 spec): the √(ln n / ln anchor) trade factor is OFF by default.
Two single-strategy tickers with equal Sharpe but very different bt_n must be
sized equally unless OPENCLAW_TRADE_WEIGHT_FACTOR=1."""
import sys
from datetime import date
from pathlib import Path
import unittest.mock as _mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import execution.regime_blended_sizer as _sizer  # noqa: E402

NAV, LAM = 100_000.0, 2.0


def _account():
    return {'equity': NAV, 'regt_buying_power': 2 * NAV, 'long_market_value': 0, 'cash': NAV}


def _params():
    return {'liquidity_param': 1.0, 'min_signal_notional_usd': 1,
            'min_signal_notional_pct': 0.00001, 'position_circuit_breaker_pct': 0.02,
            'min_cumulative_sharpe': 3.0, 'min_acting_strategies': 1}


def _carried(sid, ticker):
    return {'strategy_id': sid, 'ticker': ticker, 'direction': 'LONG',
            'signal_date': date(2026, 8, 28), 'entry_price': 100.0,
            'stop_loss': 95.0, 'target_1': 110.0, 'target_2': 120.0, 'signal_params': {}}


def _row(sid, eff, bt_n):
    return {'strategy_id': sid, 'daily_weight': eff, 'effective_sharpe': eff,
            'cadence_days': 5.0, 'bt_n': bt_n}


def _run(monkeypatch, rows, carried):
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
    for g in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_CORR_WEIGHT', 'OPENCLAW_STRATEGY_ORTHO_SHADOW',
              'OPENCLAW_STRATEGY_BRACKET_STACK', 'OPENCLAW_OPTION_DELTA_HEDGE', 'OPENCLAW_BENCH_RELATIVE_SIZING'):
        monkeypatch.delenv(g, raising=False)
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals',
                        lambda weight_by_strat, cadence_by_strat=None, **_kw: list(carried))
    monkeypatch.setattr(_sizer, '_load_lambda', lambda default=2.0, *, intraday=False: LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})
    monkeypatch.setattr(_sizer, '_apply_asset_corr_cap', lambda t, *a, **k: t)
    monkeypatch.setattr(_sizer, '_post_corr_cumsharpe_log', lambda line: None)
    with _mock.patch('execution.strategy_weights.load_current', return_value=list(rows)), \
         _mock.patch('execution.benchmark_sizing.regime_benchmark_sharpe_for_sizing', return_value=None):
        return _sizer.size_positions(signals=[], account_state=_account(), regime={'state': 'LOW_VOL'},
                                     run_date=date(2026, 8, 29), strategy_state={},
                                     regime_params=_params(), confirmer=lambda p: {})


def _targets(orders):
    return {o['ticker']: o['target_usd'] for o in orders
            if o['action'] not in ('close_long', 'close_short')}


def test_factor_off_by_default_equal_targets(monkeypatch):
    monkeypatch.delenv(_sizer.TRADE_WEIGHT_FACTOR_ENV, raising=False)
    t = _targets(_run(monkeypatch, [_row('S_a', 2.0, 50), _row('S_b', 2.0, 5000)],
                      [_carried('S_a', 'ZZTA'), _carried('S_b', 'ZZTB')]))
    assert abs(t['ZZTA'] - t['ZZTB']) < 1e-6


def test_factor_on_downweights_thin_sleeve(monkeypatch):
    monkeypatch.setenv(_sizer.TRADE_WEIGHT_FACTOR_ENV, '1')
    t = _targets(_run(monkeypatch, [_row('S_a', 2.0, 50), _row('S_b', 2.0, 5000)],
                      [_carried('S_a', 'ZZTA'), _carried('S_b', 'ZZTB')]))
    assert t['ZZTA'] < t['ZZTB']
