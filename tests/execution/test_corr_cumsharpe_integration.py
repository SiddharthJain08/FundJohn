"""End-to-end smoke for the corr-adjusted cumulative-Sharpe sizing + the
per-regime ACTING-STRATEGY conviction gate (operator directive 2026-08-22: the
S_adj floor was replaced by a minimum count of distinct strategies acting in
the ticker's net direction — regime_sizer_params.min_acting_strategies).

Drives _sharpe_cadence_path (via size_positions) with all ortho flags cleared,
all external surfaces stubbed (weights DB, carried-set, lambda, broker, confirmer,
similarity load_groups). Proves: the matrix loads, S_adj drives sizing and the
per-ticker cap with no flag set, and the per-regime acting-strategy minimum
controls ticker selection.
"""
import sys
from datetime import date
from pathlib import Path
import unittest.mock as _mock

import pytest  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
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


def _params(min_acting_strategies):
    return {'liquidity_param': 1.0, 'position_circuit_breaker_pct': 0.02,
            'min_cumulative_sharpe': 3.0, 'min_acting_strategies': min_acting_strategies}


def _run(monkeypatch, weights_rows, carried_rows, sim, min_acting_strategies):
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')  # EOD lane key (2026-07-29)
    # These tests verify the LEGACY fixed-proportional S_adj wiring + cap math;
    # the tangency default (2026-07-27) has its own tests (test_tangency_sadj).
    monkeypatch.setenv('OPENCLAW_TANGENCY_SADJ', '0')
    # The corr-adjusted gate is unconditional now — prove it fires with NO ortho
    # flag set (CORR_CUMSHARPE included in the clear-list).
    for gate in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_CORR_WEIGHT',
                 'OPENCLAW_STRATEGY_ORTHO_SHADOW', 'OPENCLAW_STRATEGY_BRACKET_STACK',
                 'OPENCLAW_STRATEGY_SIZE_SCALAR', 'OPENCLAW_STRATEGY_CORR_CUMSHARPE',
                 'OPENCLAW_STRATEGY_CORR_CUMSHARPE_SHADOW',
                 'OPENCLAW_OPTION_DELTA_HEDGE'):
        monkeypatch.delenv(gate, raising=False)
    import execution.strategy_similarity as _ss
    monkeypatch.setattr(_ss, 'load_groups',
                        lambda regime: {'block_map': {}, 'fold_map': {}, 'rep_map': {}, 'matrix': sim})
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals', lambda w, c=None, **_kw: list(carried_rows))
    monkeypatch.setattr(_sizer, '_load_lambda', lambda default=2.0, *, intraday=False: LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})
    with _mock.patch('execution.strategy_weights.load_current', return_value=list(weights_rows)):
        return _sizer.size_positions(
            signals=[], account_state=_account(), regime={'state': 'LOW_VOL'},
            run_date=date(2026, 6, 4), strategy_state={},
            regime_params=_params(min_acting_strategies), confirmer=lambda proposals: {})


def _opens(orders):
    return {o['ticker']: o for o in orders
            if o['action'] not in ('close_long', 'close_short')
            and o['strategy_id'] not in ('__close_orphan__', '__flip_close__')}


def test_on_path_activates_and_caps_on_corr_sadj(monkeypatch):
    # Two perfectly-correlated strategies (rho=1) on AAA, daily_weight 3 each.
    # S_adj = 18 / sqrt(36) = 3.0 (no double-count) — NOT the naive sum 6.
    # EOD per-ticker cap = CAP_FRAC * (|S_adj=3.0|+1) * LAM * NAV
    # (reads the live constant so operator retunes don't strand this test —
    # the hardcoded 0.05-era literal it used to carry did exactly that).
    sim = {'S1': {'S1': 1.0, 'S2': 1.0}, 'S2': {'S2': 1.0, 'S1': 1.0}}
    orders = _run(monkeypatch,
                  weights_rows=[_weights_row('S1', 3.0), _weights_row('S2', 3.0)],
                  carried_rows=[_carried('S1', 'AAA'), _carried('S2', 'AAA')],
                  sim=sim, min_acting_strategies=1)
    opens = _opens(orders)
    assert 'AAA' in opens, f'AAA should survive the acting gate, got {orders}'
    _expected = _sizer.PER_TICKER_CAP_SHARPE_FRAC * (3.0 + 1.0) * LAM * NAV
    assert abs(opens['AAA']['target_usd'] - _expected) < 1e-6, (
        f"cap must reflect S_adj=3.0 (corr-deflated), got {opens['AAA']['target_usd']}")


def test_acting_gate_min2_keeps_two_confirming_strategies(monkeypatch):
    # Two strategies long AAA → acting=2 ≥ min 2 → survives.
    sim = {'S1': {'S1': 1.0, 'S2': 0.0}, 'S2': {'S2': 1.0, 'S1': 0.0}}
    orders = _run(monkeypatch,
                  weights_rows=[_weights_row('S1', 3.0), _weights_row('S2', 3.0)],
                  carried_rows=[_carried('S1', 'AAA'), _carried('S2', 'AAA')],
                  sim=sim, min_acting_strategies=2)
    assert 'AAA' in _opens(orders), 'two confirming strategies must clear min_acting=2'


def test_acting_gate_min3_drops_two_strategy_ticker(monkeypatch):
    sim = {'S1': {'S1': 1.0, 'S2': 0.0}, 'S2': {'S2': 1.0, 'S1': 0.0}}
    orders = _run(monkeypatch,
                  weights_rows=[_weights_row('S1', 3.0), _weights_row('S2', 3.0)],
                  carried_rows=[_carried('S1', 'AAA'), _carried('S2', 'AAA')],
                  sim=sim, min_acting_strategies=3)
    assert 'AAA' not in _opens(orders), 'min_acting=3 > 2 contributors must drop AAA'


def test_acting_gate_counts_only_net_direction(monkeypatch):
    # S1,S2 long + S3 short on AAA: net long (2 vs 1, equal weights) → acting=2.
    # An opposing contributor must NOT count toward the minimum.
    sids = ['S1', 'S2', 'S3']
    sim = {a: {b: (1.0 if a == b else 0.0) for b in sids} for a in sids}
    rows = [_weights_row(s, 3.0) for s in sids]
    carried = [_carried('S1', 'AAA'), _carried('S2', 'AAA'), _carried('S3', 'AAA', 'SHORT')]
    kept = _run(monkeypatch, weights_rows=rows, carried_rows=carried, sim=sim,
                min_acting_strategies=2)
    assert 'AAA' in _opens(kept) and _opens(kept)['AAA']['direction'] == 'long'
    dropped = _run(monkeypatch, weights_rows=rows, carried_rows=carried, sim=sim,
                   min_acting_strategies=3)
    assert 'AAA' not in _opens(dropped), 'the short contributor must not count as acting long'


def test_acting_gate_repeated_rows_of_one_strategy_count_once(monkeypatch):
    # Cadence-window aggregation can carry the same strategy several times.
    sim = {'S1': {'S1': 1.0}}
    orders = _run(monkeypatch,
                  weights_rows=[_weights_row('S1', 3.0)],
                  carried_rows=[_carried('S1', 'AAA'), _carried('S1', 'AAA'), _carried('S1', 'AAA')],
                  sim=sim, min_acting_strategies=2)
    assert 'AAA' not in _opens(orders), 'three rows of ONE strategy is acting=1, not 3'


def test_acting_gate_min1_is_open_and_sizing_still_uses_sadj(monkeypatch):
    # Single-strategy ticker passes at the floor setting; its dollar target is
    # still the S_adj-driven (|S|+1) cap — the sizing conventions are untouched.
    sim = {'S1': {'S1': 1.0}}
    orders = _run(monkeypatch, weights_rows=[_weights_row('S1', 3.0)],
                  carried_rows=[_carried('S1', 'AAA')], sim=sim, min_acting_strategies=1)
    opens = _opens(orders)
    assert 'AAA' in opens
    _expected = _sizer.PER_TICKER_CAP_SHARPE_FRAC * (3.0 + 1.0) * LAM * NAV
    assert abs(opens['AAA']['target_usd'] - _expected) < 1e-6
    assert opens['AAA']['acting_strategies'] == 1


def test_on_path_emits_live_diagnostics(monkeypatch, caplog):
    import logging
    posted = []
    monkeypatch.setattr(_sizer, '_post_corr_cumsharpe_log', lambda line: posted.append(line))
    sim = {'S1': {'S1': 1.0, 'S2': 0.0}, 'S2': {'S2': 1.0, 'S1': 0.0}}
    with caplog.at_level(logging.INFO):
        orders = _run(monkeypatch,
                      weights_rows=[_weights_row('S1', 3.0), _weights_row('S2', 3.0)],
                      carried_rows=[_carried('S1', 'AAA'), _carried('S2', 'AAA')],
                      sim=sim, min_acting_strategies=1)
    assert 'AAA' in _opens(orders)
    assert 'corr_cumsharpe.live[' in caplog.text
    assert posted and 'corr_cumsharpe.live[' in posted[0]
    assert 'acting_min=1' in posted[0] and 'acting_dist=' in posted[0] and 'cap_binds=' in posted[0]


def test_post_helper_is_failsafe_without_db(monkeypatch):
    # The poster must NEVER raise (it runs inside the live trade step).
    monkeypatch.delenv('POSTGRES_URI', raising=False)
    _sizer._post_corr_cumsharpe_log('corr_cumsharpe.live[X]: smoke')  # no exception == pass


def test_matrix_load_failure_degrades_to_sparse_default(monkeypatch, caplog):
    """Similarity-matrix load failure (transient DB) must NOT crash and must NOT
    revert to the retired legacy naive gate. The (now unconditional) corr gate
    runs with sparse-default correlations (assume ~independent) and logs a loud
    warning for observability. This is the ONE path whose behavior changed vs the
    old flag-gated code (which fell back to the legacy gate); pinned here so the
    degraded-but-safe behavior is deliberate, not accidental.

    NOTE the sparse-default under-deflates correlated bets on exactly the day
    correlation data is missing — a documented risk-posture tradeoff, partially
    mitigated by the separate asset-corr cap (different correlation source)."""
    import logging
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')  # EOD lane key (2026-07-29)
    # These tests verify the LEGACY fixed-proportional S_adj wiring + cap math;
    # the tangency default (2026-07-27) has its own tests (test_tangency_sadj).
    monkeypatch.setenv('OPENCLAW_TANGENCY_SADJ', '0')
    for gate in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_CORR_WEIGHT',
                 'OPENCLAW_STRATEGY_ORTHO_SHADOW', 'OPENCLAW_STRATEGY_BRACKET_STACK',
                 'OPENCLAW_STRATEGY_SIZE_SCALAR', 'OPENCLAW_STRATEGY_CORR_CUMSHARPE',
                 'OPENCLAW_STRATEGY_CORR_CUMSHARPE_SHADOW', 'OPENCLAW_OPTION_DELTA_HEDGE'):
        monkeypatch.delenv(gate, raising=False)

    import execution.strategy_similarity as _ss

    def _boom(regime):
        raise RuntimeError('similarity DB down')

    monkeypatch.setattr(_ss, 'load_groups', _boom)
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals', lambda w, c=None, **_kw: [_carried('S1', 'AAA')])
    monkeypatch.setattr(_sizer, '_load_lambda', lambda default=2.0, *, intraday=False: LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})
    with caplog.at_level(logging.WARNING):
        with _mock.patch('execution.strategy_weights.load_current',
                         return_value=[_weights_row('S1', 3.0)]):
            orders = _sizer.size_positions(
                signals=[], account_state=_account(), regime={'state': 'LOW_VOL'},
                run_date=date(2026, 6, 4), strategy_state={},
                regime_params=_params(0.5), confirmer=lambda proposals: {})
    # Single strategy on AAA -> S_adj == daily_weight (3.0) regardless of matrix;
    # clears floor 0.5. The gate ran (no crash, no legacy revert) and warned.
    assert 'AAA' in _opens(orders), 'corr gate must still size when the matrix fails to load'
    assert 'load_groups failed' in caplog.text, 'load failure must be logged (observable)'
