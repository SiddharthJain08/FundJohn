"""tests/test_sizer_flatten_zero_conviction.py — FLATTEN-on-genuine-zero-conviction.

Covers the new zero-conviction flatten branch in
regime_blended_sizer._sharpe_cadence_path (the `if not ticker_w:` post-conviction-gate
empty check) + _maybe_flatten_zero_conviction.

The intended behavior: when a HEALTHY carried set (len(active) >= SIGNAL_SET_MIN_FLOOR)
is FULLY rejected by the corr-conviction gate AND the operator has armed the default-OFF
kill-switch OPENCLAW_FLATTEN_ON_ZERO_CONVICTION, the sizer FLATTENS the whole broker book
(orphan_close every held position) instead of silently HOLDing. Fires on the EOD lane
ONLY (OPENCLAW_EOD_RECONCILE=1 AND OPENCLAW_INTRADAY_REDEPLOY!=1) — an intraday redeploy
inherits EOD_RECONCILE=1 from the parent env, so the intraday exclusion is load-bearing.

Matrix:
  (a) EOD + flag ON + healthy set + ALL gated → orphan_close for EVERY held position.
      PINS the gross<=0 trap (the flatten must NOT be no-op'd).
  (b) EOD + flag ON + empty carried set (Case B) → [] (no flatten).
  (c) EOD + flag ON + thin set (< floor) + all gated → [] (hold).
  (d) Intraday + all gated → [] (unchanged). Two sub-cases: EOD unset (spec-literal),
      and the REALISTIC both-flags-set case (intraday subprocess inherits EOD_RECONCILE=1).
  (e) flag OFF (default) + EOD + all gated → [] (inert).
  (f) alert fires ONLY on the flatten branch (asserted per-test).
  (g) normal path (some tickers clear) → orders identical with the flag ON vs OFF
      (byte-identical pin) — the flag is inert on the normal path.

Mocking style mirrors tests/test_corr_cumsharpe_integration.py: every external surface
(weights DB, carried-set loader, lambda, broker, similarity load_groups, the two Discord
posters) is stubbed, so no DB / broker / network is touched.
"""
import sys
from datetime import date
from pathlib import Path
import unittest.mock as _mock

import pytest  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import execution.regime_blended_sizer as _sizer  # noqa: E402

NAV = 100_000.0
LAM = 2.0

# Ortho / feature flags cleared for every run so only the flags under test matter.
_ORTHO_FLAGS = (
    'OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_CORR_WEIGHT',
    'OPENCLAW_STRATEGY_ORTHO_SHADOW', 'OPENCLAW_STRATEGY_BRACKET_STACK',
    'OPENCLAW_STRATEGY_SIZE_SCALAR', 'OPENCLAW_STRATEGY_CORR_CUMSHARPE',
    'OPENCLAW_STRATEGY_CORR_CUMSHARPE_SHADOW', 'OPENCLAW_STRATEGY_BREADTH_WEIGHT',
    'OPENCLAW_STRATEGY_CADENCE_STOP_NORM', 'OPENCLAW_OPTION_DELTA_HEDGE',
    'OPENCLAW_OPTION_EXEC', 'OPENCLAW_ASSET_CORR_CAP', 'OPENCLAW_ASSET_CORR_CAP_SHADOW',
)


def _account(equity=NAV):
    return {'equity': equity, 'regt_buying_power': 2 * equity,
            'long_market_value': 0, 'cash': equity}


def _params(min_corr_cum_sharpe):
    return {'liquidity_param': 1.0, 'position_circuit_breaker_pct': 0.02,
            'min_corr_cum_sharpe': min_corr_cum_sharpe}


def _weights_row(sid, daily_weight=1.0):
    return {'strategy_id': sid, 'daily_weight': daily_weight,
            'effective_sharpe': daily_weight, 'cadence_days': 1.0}


def _carried(sid, ticker, direction='LONG'):
    return {'strategy_id': sid, 'ticker': ticker, 'direction': direction,
            'signal_date': date(2026, 7, 6), 'entry_price': 100.0,
            'stop_loss': 95.0, 'target_1': 110.0, 'target_2': 120.0,
            'signal_params': {}}


def _healthy_set(n=10, weight=1.0):
    """n single-strategy tickers → each ticker's S_adj == its (single) daily_weight."""
    weights = [_weights_row('S%d' % i, weight) for i in range(n)]
    carried = [_carried('S%d' % i, 'T%d' % i) for i in range(n)]
    return weights, carried


def _run(monkeypatch, *, weights_rows, carried_rows, broker, min_corr_cum_sharpe,
         eod=True, intraday=False, flatten_flag=None, confirmer=None):
    """Drive size_positions with everything external stubbed. Returns (orders, alerts)
    where `alerts` is the list of _post_flatten_alert (regime, active_count, broker)
    calls captured during the run."""
    if eod:
        monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    else:
        monkeypatch.delenv('OPENCLAW_EOD_RECONCILE', raising=False)
    if intraday:
        monkeypatch.setenv('OPENCLAW_INTRADAY_REDEPLOY', '1')
    else:
        monkeypatch.delenv('OPENCLAW_INTRADAY_REDEPLOY', raising=False)
    if flatten_flag is None:
        monkeypatch.delenv('OPENCLAW_FLATTEN_ON_ZERO_CONVICTION', raising=False)
    else:
        monkeypatch.setenv('OPENCLAW_FLATTEN_ON_ZERO_CONVICTION', flatten_flag)
    for gate in _ORTHO_FLAGS:
        monkeypatch.delenv(gate, raising=False)

    import execution.strategy_similarity as _ss
    monkeypatch.setattr(_ss, 'load_groups',
                        lambda regime: {'block_map': {}, 'fold_map': {},
                                        'rep_map': {}, 'matrix': {}})
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals',
                        lambda w, c=None, **_kw: list(carried_rows))
    monkeypatch.setattr(_sizer, '_load_active_window_signals',
                        lambda r, w, c: list(carried_rows))
    monkeypatch.setattr(_sizer, '_load_lambda',
                        lambda default=2.0, *, intraday=False: LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: dict(broker))
    # Silence the (unrelated) corr diagnostic poster so no DB/network is touched.
    monkeypatch.setattr(_sizer, '_post_corr_cumsharpe_log', lambda line: None)

    alerts = []
    monkeypatch.setattr(_sizer, '_post_flatten_alert',
                        lambda regime, n, brk: alerts.append((regime, n, dict(brk))))

    with _mock.patch('execution.strategy_weights.load_current',
                     return_value=list(weights_rows)):
        orders = _sizer.size_positions(
            signals=[], account_state=_account(), regime={'state': 'LOW_VOL'},
            run_date=date(2026, 7, 6), strategy_state={},
            regime_params=_params(min_corr_cum_sharpe),
            confirmer=confirmer or (lambda proposals: {}))
    return orders, alerts


def _is_close(o):
    return (o['strategy_id'] in ('__close_orphan__', '__flip_close__')
            or o['action'] in ('close_long', 'close_short'))


# ---------------------------------------------------------------------------
# (a) EOD + flag ON + healthy set + ALL gated → flatten every held position.
#     Pins the gross<=0 trap: the flatten must return NON-EMPTY orphan closes.
# ---------------------------------------------------------------------------

def test_a_eod_flag_on_healthy_all_gated_flattens_every_position(monkeypatch):
    weights, carried = _healthy_set(n=10)          # 10 healthy signals
    broker = {'HELDA': 8_000.0, 'HELDB': -5_000.0, 'HELDC': 12_500.0}
    orders, alerts = _run(monkeypatch, weights_rows=weights, carried_rows=carried,
                          broker=broker, min_corr_cum_sharpe=5.0,  # >> S_adj=1.0 → all gated
                          eod=True, intraday=False, flatten_flag='1')

    assert orders, 'flatten must emit orders (gross<=0 trap must be bypassed), got []'
    assert {o['ticker'] for o in orders} == set(broker), \
        'must flatten EVERY held broker position, got %r' % [o['ticker'] for o in orders]
    for o in orders:
        assert o['strategy_id'] == '__close_orphan__', \
            'every flatten order must be an orphan_close, got %r' % o
        assert _is_close(o), 'every flatten order must be a close, got %r' % o
        assert o['target_usd'] == 0.0, 'flatten target must be 0, got %r' % o
        assert o['source_mode'] == 'sharpe_cadence'
    # (f) alert fires exactly once, naming the carried-set size + the flattened book.
    assert len(alerts) == 1, 'flatten alert must fire exactly once'
    regime, n, brk = alerts[0]
    assert regime == 'LOW_VOL' and n == 10 and brk == broker


# ---------------------------------------------------------------------------
# (b) EOD + flag ON + empty carried set (Case B) → [] and NO flatten.
# ---------------------------------------------------------------------------

def test_b_eod_flag_on_empty_carried_returns_empty_no_flatten(monkeypatch):
    # Empty approved carried set → the Case-B early return fires BEFORE the gate;
    # the flatten branch is never reached (data-absence, not conviction-rejection).
    orders, alerts = _run(monkeypatch, weights_rows=[_weights_row('S0')],
                          carried_rows=[], broker={'HELDA': 8_000.0},
                          min_corr_cum_sharpe=5.0, eod=True, flatten_flag='1')
    assert orders == [], 'empty carried set must return [] (Case B), got %r' % orders
    assert alerts == [], 'flatten must NOT fire on an empty carried set'


# ---------------------------------------------------------------------------
# (c) EOD + flag ON + THIN set (< SIGNAL_SET_MIN_FLOOR) + all gated → [] (hold).
# ---------------------------------------------------------------------------

def test_c_eod_flag_on_thin_set_all_gated_holds(monkeypatch):
    assert _sizer.SIGNAL_SET_MIN_FLOOR == 10
    weights, carried = _healthy_set(n=3)           # thin: 3 < 10
    orders, alerts = _run(monkeypatch, weights_rows=weights, carried_rows=carried,
                          broker={'HELDA': 8_000.0, 'HELDB': -5_000.0},
                          min_corr_cum_sharpe=5.0, eod=True, flatten_flag='1')
    assert orders == [], 'thin gated set must HOLD (return []), got %r' % orders
    assert alerts == [], 'flatten must NOT fire on a thin carried set'


# ---------------------------------------------------------------------------
# (d) Intraday + all gated → [] (unchanged). The load-bearing safety property.
# ---------------------------------------------------------------------------

def test_d1_intraday_eod_unset_all_gated_holds(monkeypatch):
    # Spec-literal: OPENCLAW_INTRADAY_REDEPLOY=1, OPENCLAW_EOD_RECONCILE unset.
    weights, carried = _healthy_set(n=10)
    orders, alerts = _run(monkeypatch, weights_rows=weights, carried_rows=carried,
                          broker={'HELDA': 8_000.0}, min_corr_cum_sharpe=5.0,
                          eod=False, intraday=True, flatten_flag='1')
    assert orders == [], 'intraday redeploy must never flatten, got %r' % orders
    assert alerts == [], 'flatten must NOT fire on an intraday redeploy'


def test_d2_intraday_with_eod_inherited_all_gated_holds(monkeypatch):
    # REALISTIC + DANGEROUS: the intraday subprocess inherits EOD_RECONCILE=1
    # (scripts/redeploy_pipeline.py: env = {**os.environ}) AND sets
    # INTRADAY_REDEPLOY=1. The INTRADAY exclusion is what prevents a liquidation on a
    # momentary regime transition. This is the test that pins that safety property.
    weights, carried = _healthy_set(n=10)
    orders, alerts = _run(monkeypatch, weights_rows=weights, carried_rows=carried,
                          broker={'HELDA': 8_000.0, 'HELDB': -3_000.0},
                          min_corr_cum_sharpe=5.0,
                          eod=True, intraday=True, flatten_flag='1')
    assert orders == [], (
        'intraday redeploy with EOD_RECONCILE inherited must NOT flatten — an '
        'intraday flatten would liquidate the book on a momentary regime blip. '
        'got %r' % orders)
    assert alerts == [], 'flatten must NOT fire when INTRADAY_REDEPLOY=1'


# ---------------------------------------------------------------------------
# (e) flag OFF (default) + EOD + all gated → [] (inert until the operator arms it).
# ---------------------------------------------------------------------------

def test_e_flag_off_default_eod_all_gated_is_inert(monkeypatch):
    weights, carried = _healthy_set(n=12)
    # flatten_flag=None → the kill-switch env var is UNSET (default-OFF).
    orders, alerts = _run(monkeypatch, weights_rows=weights, carried_rows=carried,
                          broker={'HELDA': 8_000.0, 'HELDB': -5_000.0},
                          min_corr_cum_sharpe=5.0, eod=True, flatten_flag=None)
    assert orders == [], 'flag OFF must be inert (historical HOLD), got %r' % orders
    assert alerts == [], 'flatten must NOT fire while the kill-switch is OFF'

    # Also explicitly OFF ('0') must be inert.
    orders0, alerts0 = _run(monkeypatch, weights_rows=weights, carried_rows=carried,
                            broker={'HELDA': 8_000.0}, min_corr_cum_sharpe=5.0,
                            eod=True, flatten_flag='0')
    assert orders0 == [] and alerts0 == []


# ---------------------------------------------------------------------------
# (g) Normal path (some tickers clear) → orders identical with flag ON vs OFF.
#     Byte-identical pin: the flatten flag must be inert on the normal path.
# ---------------------------------------------------------------------------

def _normal_run(monkeypatch, flatten_flag):
    # Two single-strategy tickers clear the low floor (S_adj=3.0 >= 0.5); the broker
    # holds T0 (a resize delta) and ORPH (an orphan → close). Exercises the full
    # emission tail (delta + open + orphan_close).
    weights = [_weights_row('S0', 3.0), _weights_row('S1', 3.0)]
    carried = [_carried('S0', 'T0'), _carried('S1', 'T1')]
    broker = {'T0': 5_000.0, 'ORPH': 4_000.0}
    return _run(monkeypatch, weights_rows=weights, carried_rows=carried,
                broker=broker, min_corr_cum_sharpe=0.5, eod=True, flatten_flag=flatten_flag)


def test_g_normal_path_byte_identical_flag_on_vs_off(monkeypatch):
    orders_off, alerts_off = _normal_run(monkeypatch, flatten_flag=None)
    orders_on, alerts_on = _normal_run(monkeypatch, flatten_flag='1')

    assert orders_off, 'normal path must emit orders (sanity), got []'
    assert orders_off == orders_on, (
        'the flatten flag must be INERT on the normal path — orders diverged.\n'
        'OFF=%r\nON =%r' % (orders_off, orders_on))
    # The flatten alert must NEVER fire when tickers clear the gate.
    assert alerts_off == [] and alerts_on == [], 'no flatten alert on the normal path'

    # Structure pin: an orphan_close for ORPH + live opens for T0/T1, all sharpe_cadence.
    by_ticker = {o['ticker']: o for o in orders_on}
    assert 'ORPH' in by_ticker and by_ticker['ORPH']['strategy_id'] == '__close_orphan__'
    assert _is_close(by_ticker['ORPH'])
    for t in ('T0', 'T1'):
        assert t in by_ticker and by_ticker[t]['source_mode'] == 'sharpe_cadence'
