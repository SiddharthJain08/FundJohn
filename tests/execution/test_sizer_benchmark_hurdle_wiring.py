"""Spec §2.5 wiring: OFF -> byte-identical sizing + shadow line; ON -> hurdle
applied, benchmark ticker exempt and uncapped by the hurdle."""
import sys
from datetime import date
from pathlib import Path
import unittest.mock as _mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import execution.regime_blended_sizer as _sizer  # noqa: E402
from execution import benchmark_sizing as bz      # noqa: E402

NAV, LAM = 100_000.0, 2.0


def _account():
    return {'equity': NAV, 'regt_buying_power': 2 * NAV, 'long_market_value': 0, 'cash': NAV}


def _params():
    return {'liquidity_param': 1.0, 'min_signal_notional_usd': 1, 'min_signal_notional_pct': 0.00001,
            'position_circuit_breaker_pct': 0.02, 'min_cumulative_sharpe': 3.0, 'min_acting_strategies': 1}


def _carried(sid, ticker, direction='LONG'):
    return {'strategy_id': sid, 'ticker': ticker, 'direction': direction,
            'signal_date': date(2026, 8, 28), 'entry_price': 100.0, 'stop_loss': 95.0,
            'target_1': 110.0, 'target_2': 120.0, 'signal_params': {}}


def _row(sid, eff):
    return {'strategy_id': sid, 'daily_weight': eff, 'effective_sharpe': eff, 'cadence_days': 21.0, 'bt_n': 600}


ROWS = [_row('S_beta_spy', 2.0), _row('S_hi', 2.6), _row('S_lo', 1.5)]
CARRIED = [_carried('S_beta_spy', 'SPY'), _carried('S_hi', 'ZZTA'), _carried('S_lo', 'ZZTB')]


def run(monkeypatch, flag, s_m=2.0, lines=None):
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
    for g in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_CORR_WEIGHT', 'OPENCLAW_STRATEGY_ORTHO_SHADOW',
              'OPENCLAW_STRATEGY_BRACKET_STACK', 'OPENCLAW_OPTION_DELTA_HEDGE', 'OPENCLAW_TRADE_WEIGHT_FACTOR',
              'OPENCLAW_INTRADAY_REDEPLOY'):
        monkeypatch.delenv(g, raising=False)
    if flag: monkeypatch.setenv(bz.BENCH_SIZING_ENV, '1')
    else:    monkeypatch.delenv(bz.BENCH_SIZING_ENV, raising=False)
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals',
                        lambda weight_by_strat, cadence_by_strat=None, **_kw: list(CARRIED))
    monkeypatch.setattr(_sizer, '_load_lambda', lambda default=2.0, *, intraday=False: LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})
    monkeypatch.setattr(_sizer, '_apply_asset_corr_cap', lambda t, *a, **k: t)
    monkeypatch.setattr(_sizer, '_post_corr_cumsharpe_log', lambda line: (lines.append(line) if lines is not None else None))
    monkeypatch.setattr(_sizer, '_maybe_flatten_zero_conviction', lambda *a, **k: None)
    # The per-ticker conviction cap is armed under EOD_RECONCILE and would clamp
    # SPY/ZZTA here; it is not this task's subject (Task 11 exempts benchmark
    # tickers), so lift it out of the way.
    monkeypatch.setattr(_sizer, 'PER_TICKER_CAP_SHARPE_FRAC', 10.0)
    with _mock.patch('execution.strategy_weights.load_current', return_value=list(ROWS)), \
         _mock.patch('execution.benchmark_sleeve.load_benchmark_sleeve_ids', return_value={'S_beta_spy'}), \
         _mock.patch('execution.benchmark_sizing.regime_benchmark_sharpe_for_sizing', return_value=s_m):
        return _sizer.size_positions(signals=[], account_state=_account(), regime={'state': 'LOW_VOL'},
                                     run_date=date(2026, 8, 29), strategy_state={},
                                     regime_params=_params(), confirmer=lambda p: {})


def targets(orders):
    return {o['ticker']: o['target_usd'] for o in orders if o['action'] not in ('close_long', 'close_short')}


def test_off_is_byte_identical_and_logs_shadow(monkeypatch):
    lines = []
    t = targets(run(monkeypatch, flag=False, lines=lines))
    gross = LAM * NAV
    # raw S_adj shares: 2.0 : 2.6 : 1.5 (per-ticker cap lifted in run())
    assert abs(t['SPY'] - gross * 2.0 / 6.1) < 1e-6
    assert abs(t['ZZTA'] - gross * 2.6 / 6.1) < 1e-6
    assert abs(t['ZZTB'] - gross * 1.5 / 6.1) < 1e-6
    assert any(l.startswith('bench_sizing.shadow[LOW_VOL]: S_m=2.00') and 'dropped=1/3' in l for l in lines)


def test_on_applies_hurdle(monkeypatch):
    t = targets(run(monkeypatch, flag=True))
    # after hurdle: SPY 2.0 (exempt), ZZTA 0.6, ZZTB dropped -> gross 2.6
    gross = LAM * NAV
    assert 'ZZTB' not in t
    assert abs(t['ZZTA'] - gross * 0.6 / 2.6) < 1e-6
    assert abs(t['SPY'] - gross * 2.0 / 2.6) < 1e-6     # benchmark ticker keeps its full S_adj


def test_on_with_no_s_m_falls_back_to_raw(monkeypatch):
    t = targets(run(monkeypatch, flag=True, s_m=None))
    assert set(t) == {'SPY', 'ZZTA', 'ZZTB'}
