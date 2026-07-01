"""Wiring: the breadth weight factor routes per-strategy N -> corr weights ->
sizing, flag-gated (OPENCLAW_STRATEGY_BREADTH_WEIGHT). Two single-strategy
tickers with equal base weight; only differential universe size N moves them."""
import sys
from datetime import date
from pathlib import Path
import unittest.mock as _mock

import pytest  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import execution.regime_blended_sizer as _sizer  # noqa: E402

NAV = 100_000.0
LAM = 2.0


def _account(equity=NAV):
    return {'equity': equity, 'regt_buying_power': 2 * equity,
            'long_market_value': 0, 'cash': equity}


def _carried(sid, ticker):
    return {'strategy_id': sid, 'ticker': ticker, 'direction': 'LONG',
            'signal_date': date(2026, 6, 3), 'entry_price': 100.0, 'stop_loss': 95.0,
            'target_1': 110.0, 'target_2': 120.0, 'signal_params': {}}


def _wrow(sid, daily_weight):
    return {'strategy_id': sid, 'daily_weight': daily_weight,
            'effective_sharpe': daily_weight, 'cadence_days': 1.0}


def _params():
    return {'liquidity_param': 1.0, 'position_circuit_breaker_pct': 0.02,
            'min_cumulative_sharpe': 3.0, 'min_corr_cum_sharpe': 0.0}


def _opens(orders):
    return {o['ticker']: o for o in orders
            if o['action'] not in ('close_long', 'close_short')
            and o['strategy_id'] not in ('__close_orphan__', '__flip_close__')}


def _run(monkeypatch, breadth_on, sizes):
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    for g in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_CORR_WEIGHT',
              'OPENCLAW_STRATEGY_ORTHO_SHADOW', 'OPENCLAW_STRATEGY_BRACKET_STACK',
              'OPENCLAW_STRATEGY_SIZE_SCALAR', 'OPENCLAW_OPTION_DELTA_HEDGE'):
        monkeypatch.delenv(g, raising=False)
    if breadth_on:
        monkeypatch.setenv('OPENCLAW_STRATEGY_BREADTH_WEIGHT', '1')
    else:
        monkeypatch.delenv('OPENCLAW_STRATEGY_BREADTH_WEIGHT', raising=False)
    import execution.strategy_similarity as _ss
    monkeypatch.setattr(_ss, 'load_groups',
                        lambda regime: {'block_map': {}, 'fold_map': {}, 'rep_map': {}, 'matrix': {}})
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals',
                        lambda w: [_carried('S1', 'AAA'), _carried('S2', 'BBB')])
    monkeypatch.setattr(_sizer, '_load_lambda', lambda default=2.0, *, intraday=False: LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})
    import execution.strategy_weights as _sw
    monkeypatch.setattr(_sw, 'load_universe_sizes', lambda: dict(sizes))
    monkeypatch.setattr(_sizer, '_post_corr_cumsharpe_log', lambda line: None)
    with _mock.patch('execution.strategy_weights.load_current',
                     return_value=[_wrow('S1', 3.0), _wrow('S2', 3.0)]):
        return _sizer.size_positions(
            signals=[], account_state=_account(), regime={'state': 'LOW_VOL'},
            run_date=date(2026, 6, 4), strategy_state={},
            regime_params=_params(), confirmer=lambda p: {})


def _targets(monkeypatch, breadth_on, sizes):
    opens = _opens(_run(monkeypatch, breadth_on, sizes))
    assert 'AAA' in opens and 'BBB' in opens, f'both tickers should size, got {list(opens)}'
    return abs(opens['AAA']['target_usd']), abs(opens['BBB']['target_usd'])


def test_flag_off_equal_regardless_of_N(monkeypatch):
    # Flag OFF: N is ignored entirely -> equal base weights size equally.
    a, b = _targets(monkeypatch, breadth_on=False, sizes={'S1': 5180, 'S2': 50})
    assert a == pytest.approx(b)


def test_flag_on_broader_universe_gets_more(monkeypatch):
    # Flag ON: S1 (broad N=5180) upweighted vs S2 (narrow N=50).
    a, b = _targets(monkeypatch, breadth_on=True, sizes={'S1': 5180, 'S2': 50})
    assert a > b


def test_flag_on_equal_N_is_symmetric(monkeypatch):
    # Uniform factor -> no relative change (proves the effect comes from differential N).
    a, b = _targets(monkeypatch, breadth_on=True, sizes={'S1': 1000, 'S2': 1000})
    assert a == pytest.approx(b)


def test_flag_on_missing_N_defaults_to_unity(monkeypatch):
    # No stored size for either strategy -> factor 1.0 -> byte-identical to flag OFF.
    a, b = _targets(monkeypatch, breadth_on=True, sizes={})
    assert a == pytest.approx(b)
