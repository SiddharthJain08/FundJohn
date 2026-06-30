"""End-to-end ON-path smoke for the corr-adjusted cumulative-Sharpe gate.

Drives _sharpe_cadence_path (via size_positions) with OPENCLAW_STRATEGY_CORR_CUMSHARPE=1,
all external surfaces stubbed (weights DB, carried-set, lambda, broker, confirmer,
similarity load_groups). Proves: the matrix loads, the corr path activates, S_adj drives
both the gate (ticker selection) and sizing, and the per-regime corr floor controls selection.
"""
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


def _carried(sid, ticker, direction='LONG'):
    return {'strategy_id': sid, 'ticker': ticker, 'direction': direction,
            'signal_date': date(2026, 6, 3), 'entry_price': 100.0,
            'stop_loss': 95.0, 'target_1': 110.0, 'target_2': 120.0, 'signal_params': {}}


def _weights_row(sid, daily_weight):
    return {'strategy_id': sid, 'daily_weight': daily_weight,
            'effective_sharpe': daily_weight, 'cadence_days': 1.0}


def _params(min_corr_cum_sharpe):
    return {'liquidity_param': 1.0, 'position_circuit_breaker_pct': 0.02,
            'min_cumulative_sharpe': 3.0, 'min_corr_cum_sharpe': min_corr_cum_sharpe}


def _run(monkeypatch, weights_rows, carried_rows, sim, min_corr_cum_sharpe):
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.setenv('OPENCLAW_STRATEGY_CORR_CUMSHARPE', '1')
    for gate in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_CORR_WEIGHT',
                 'OPENCLAW_STRATEGY_ORTHO_SHADOW', 'OPENCLAW_STRATEGY_BRACKET_STACK',
                 'OPENCLAW_STRATEGY_SIZE_SCALAR', 'OPENCLAW_STRATEGY_CORR_CUMSHARPE_SHADOW',
                 'OPENCLAW_OPTION_DELTA_HEDGE'):
        monkeypatch.delenv(gate, raising=False)
    import execution.strategy_similarity as _ss
    monkeypatch.setattr(_ss, 'load_groups',
                        lambda regime: {'block_map': {}, 'fold_map': {}, 'rep_map': {}, 'matrix': sim})
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals', lambda w: list(carried_rows))
    monkeypatch.setattr(_sizer, '_load_lambda', lambda default=2.0, *, intraday=False: LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})
    with _mock.patch('execution.strategy_weights.load_current', return_value=list(weights_rows)):
        return _sizer.size_positions(
            signals=[], account_state=_account(), regime={'state': 'LOW_VOL'},
            run_date=date(2026, 6, 4), strategy_state={},
            regime_params=_params(min_corr_cum_sharpe), confirmer=lambda proposals: {})


def _opens(orders):
    return {o['ticker']: o for o in orders
            if o['action'] not in ('close_long', 'close_short')
            and o['strategy_id'] not in ('__close_orphan__', '__flip_close__')}


def test_on_path_activates_and_caps_on_corr_sadj(monkeypatch):
    # Two perfectly-correlated strategies (rho=1) on AAA, daily_weight 3 each.
    # S_adj = 18 / sqrt(36) = 3.0 (no double-count) — NOT the naive sum 6.
    # EOD per-ticker cap = 0.05 * |S_adj=3.0| * LAM * NAV = $30k.
    sim = {'S1': {'S1': 1.0, 'S2': 1.0}, 'S2': {'S2': 1.0, 'S1': 1.0}}
    orders = _run(monkeypatch,
                  weights_rows=[_weights_row('S1', 3.0), _weights_row('S2', 3.0)],
                  carried_rows=[_carried('S1', 'AAA'), _carried('S2', 'AAA')],
                  sim=sim, min_corr_cum_sharpe=1.0)
    opens = _opens(orders)
    assert 'AAA' in opens, f'AAA should survive the corr gate, got {orders}'
    assert abs(opens['AAA']['target_usd'] - 0.05 * 3.0 * LAM * NAV) < 1e-6, (
        f"cap must reflect S_adj=3.0 (corr-deflated), got {opens['AAA']['target_usd']}")


def test_on_path_corr_floor_drops_ticker(monkeypatch):
    # Same S_adj=3.0, but floor 5.0 > 3.0 -> AAA dropped (selection by corr floor).
    sim = {'S1': {'S1': 1.0, 'S2': 1.0}, 'S2': {'S2': 1.0, 'S1': 1.0}}
    orders = _run(monkeypatch,
                  weights_rows=[_weights_row('S1', 3.0), _weights_row('S2', 3.0)],
                  carried_rows=[_carried('S1', 'AAA'), _carried('S2', 'AAA')],
                  sim=sim, min_corr_cum_sharpe=5.0)
    assert 'AAA' not in _opens(orders), 'corr floor 5.0 > S_adj 3.0 must drop AAA'


def _run_shadow(monkeypatch, weights_rows, carried_rows, sim):
    """Shadow flag ON, live flag OFF: legacy routing + shadow metrics logged."""
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.setenv('OPENCLAW_STRATEGY_CORR_CUMSHARPE_SHADOW', '1')
    monkeypatch.delenv('OPENCLAW_STRATEGY_CORR_CUMSHARPE', raising=False)
    for gate in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_CORR_WEIGHT',
                 'OPENCLAW_STRATEGY_ORTHO_SHADOW', 'OPENCLAW_STRATEGY_BRACKET_STACK',
                 'OPENCLAW_STRATEGY_SIZE_SCALAR', 'OPENCLAW_OPTION_DELTA_HEDGE'):
        monkeypatch.delenv(gate, raising=False)
    import execution.strategy_similarity as _ss
    monkeypatch.setattr(_ss, 'load_groups',
                        lambda regime: {'block_map': {}, 'fold_map': {}, 'rep_map': {}, 'matrix': sim})
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals', lambda w: list(carried_rows))
    monkeypatch.setattr(_sizer, '_load_lambda', lambda default=2.0, *, intraday=False: LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})
    with _mock.patch('execution.strategy_weights.load_current', return_value=list(weights_rows)):
        return _sizer.size_positions(
            signals=[], account_state=_account(), regime={'state': 'LOW_VOL'},
            run_date=date(2026, 6, 4), strategy_state={},
            regime_params=_params(1.0), confirmer=lambda proposals: {})


def test_on_path_emits_live_diagnostics(monkeypatch, caplog):
    import logging
    posted = []
    monkeypatch.setattr(_sizer, '_post_corr_cumsharpe_log', lambda line: posted.append(line))
    sim = {'S1': {'S1': 1.0, 'S2': 0.0}, 'S2': {'S2': 1.0, 'S1': 0.0}}
    with caplog.at_level(logging.INFO):
        orders = _run(monkeypatch,
                      weights_rows=[_weights_row('S1', 3.0), _weights_row('S2', 3.0)],
                      carried_rows=[_carried('S1', 'AAA'), _carried('S2', 'AAA')],
                      sim=sim, min_corr_cum_sharpe=0.5)
    assert 'AAA' in _opens(orders)
    assert 'corr_cumsharpe.live[' in caplog.text
    assert posted and 'corr_cumsharpe.live[' in posted[0]
    assert 'rec_floor=' in posted[0] and 'cap_binds=' in posted[0]


def test_post_helper_is_failsafe_without_db(monkeypatch):
    # The poster must NEVER raise (it runs inside the live trade step).
    monkeypatch.delenv('POSTGRES_URI', raising=False)
    _sizer._post_corr_cumsharpe_log('corr_cumsharpe.live[X]: smoke')  # no exception == pass


def test_shadow_runs_and_logs_without_routing_change(monkeypatch, caplog):
    import logging
    sim = {'S1': {'S1': 1.0, 'S2': 0.0}, 'S2': {'S2': 1.0, 'S1': 0.0}}
    with caplog.at_level(logging.INFO):
        orders = _run_shadow(monkeypatch,
                             weights_rows=[_weights_row('S1', 3.0), _weights_row('S2', 3.0)],
                             carried_rows=[_carried('S1', 'AAA'), _carried('S2', 'AAA')],
                             sim=sim)
    # Shadow routes nothing extra: the LEGACY path still emits the AAA open.
    assert 'AAA' in _opens(orders)
    # The shadow glue ran end-to-end (NOT swallowed by the except) and logged the
    # richer metrics the rollout depends on.
    assert 'corr_cumsharpe.shadow:' in caplog.text
    assert 'cap_binds=' in caplog.text and 'sign_flips=' in caplog.text
