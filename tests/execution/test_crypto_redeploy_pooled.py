"""Crypto engine Phase 3, Task 8 — the crypto redeploy re-runs the POOLED
sizer for the crypto subset (OPENCLAW_CRYPTO_POOLED_SIZING=1) and keeps the
legacy budget model when the flag is unset. No DB / Redis / broker."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import crypto_redeploy_sizer as crs  # noqa: E402

NAV = 100_000.0
NO_HOLD = {'sleeve': None, 'tickers': {}}


def _raw(ticker, sid, direction, usd, *, bracket=True, current=0.0, target=None):
    """Order dict shaped exactly as regime_blended_sizer._emit_orders_from_targets
    builds it (bracket-less ⇒ close/reduce, close_only set by the adapter)."""
    b = {'entry': 100.0, 'stop': 90.0, 't1': 120.0, 't2': 130.0} if bracket else {}
    return {
        'ticker': ticker, 'strategy_id': sid, 'direction': direction,
        'notional_usd': abs(usd), 'pct_nav': abs(usd) / NAV, 'shares': 0,
        'entry': b.get('entry'), 'stop': b.get('stop'), 't1': b.get('t1'), 't2': b.get('t2'),
        'kelly_final': abs(usd) / NAV, 'ev': 0.0, 'p_t1': 0.5, 'source_mode': 'sharpe_cadence',
        'target_usd': usd if target is None else target, 'current_usd': current,
        'contributing_strategies': [sid], 'contributions': [{'strategy_id': sid,
                                                             'attribution_weight': 1.0}],
        'corr_cum_sharpe': 1.0, 'acting_strategies': 1, 'flip_action': None, 'action': 'open_long',
    }


BOOK = [
    _raw('SPY', 'S_beta_spy', 'long', 30_000.0),
    _raw('AAPL', '__close_orphan__', 'short', 5_000.0, bracket=False, current=5_000.0, target=0.0),
    _raw('BTC-USD', 'S_beta_btc', 'long', 12_000.0),
    _raw('ETH-USD', '__close_orphan__', 'short', 3_000.0, bracket=False, current=3_000.0, target=0.0),
]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv('OPENCLAW_CRYPTO_HOLD_DISABLE', '1')
    monkeypatch.delenv('OPENCLAW_INSTRUMENT_CLASS_ROUTING', raising=False)
    monkeypatch.setenv('OPENCLAW_CRYPTO_POOLED_SIZING', '1')


def _pooled(runner=None, hold=NO_HOLD, regime=('LOW_VOL', {'liquidity_param': 1.0})):
    calls = []

    def _runner(acct, eq_state, eq_params, cr_state, cr_params):
        calls.append((eq_state, eq_params, cr_state, cr_params))
        return [dict(o) for o in BOOK]
    out = crs.size_crypto_positions(
        {'equity': NAV}, {'state': 'HIGH_VOL'},
        pooled_runner=runner or _runner,
        regime_loader=lambda: regime,
        crypto_loader=lambda: ('HIGH_VOL', {'liquidity_param': 0.8}),
        hold_state=hold)
    return out, calls


def test_pooled_keeps_only_crypto_orders_and_counts_discards(capsys):
    orders, calls = _pooled()
    assert calls == [('LOW_VOL', {'liquidity_param': 1.0}, 'HIGH_VOL', {'liquidity_param': 0.8})]
    assert sorted(o['ticker'] for o in orders) == ['BTC-USD', 'ETH-USD']
    assert 'crypto_redeploy: equity_orders_discarded=2 crypto_orders=2' in capsys.readouterr().out


def test_pooled_orders_meet_the_executor_contract():
    by = {o['ticker']: o for o in _pooled()[0]}
    btc, eth = by['BTC-USD'], by['ETH-USD']
    assert not btc.get('close_only') and btc['direction'] == 'long'
    assert btc['pct_nav'] == pytest.approx(0.12) and btc['notional_usd'] == 12_000.0
    assert btc['strategy_id'] == 'S_beta_btc'
    assert eth['close_only'] is True and eth['direction'] == 'short'
    assert eth['notional_usd'] == 3_000.0


def test_pooled_with_class_routing_on_still_emits_crypto(monkeypatch):
    monkeypatch.setenv('OPENCLAW_INSTRUMENT_CLASS_ROUTING', '1')
    import execution.regime_blended_sizer_live as _live
    monkeypatch.setattr(_live, 'instrument_class_for',
                        lambda sid: 'crypto' if sid == 'S_beta_btc' else 'equity')
    by = {o['ticker']: o for o in _pooled()[0]}
    assert set(by) == {'BTC-USD', 'ETH-USD'} and by['BTC-USD']['pct_nav'] > 0


def test_pooled_sleeve_hold_drops_buys_keeps_closes():
    orders, _ = _pooled(hold={'sleeve': {'since': 'x'}, 'tickers': {}})
    assert [o['ticker'] for o in orders] == ['ETH-USD'] and orders[0]['close_only']


def test_pooled_ticker_hold_drops_that_ticker_buy_only():
    orders, _ = _pooled(hold={'sleeve': None, 'tickers': {'BTC/USD': {}}})
    assert [o['ticker'] for o in orders] == ['ETH-USD']
    orders, _ = _pooled(hold={'sleeve': None, 'tickers': {'SOL-USD': {}}})
    assert sorted(o['ticker'] for o in orders) == ['BTC-USD', 'ETH-USD']


def test_pooled_no_equity_regime_holds_crypto():
    def _never(*a):
        raise AssertionError('runner must not run without an equity regime')
    orders, _ = _pooled(runner=_never, regime=(None, None))
    assert orders == []


def test_pooled_no_crypto_regime_no_orders():
    def _never(*a):
        raise AssertionError('runner must not run without a crypto regime')
    out = crs.size_crypto_positions({'equity': NAV}, {'state': None}, pooled_runner=_never,
                                    regime_loader=lambda: ('LOW_VOL', {}),
                                    crypto_loader=lambda: (None, None), hold_state=NO_HOLD)
    assert out == []


def test_default_runner_never_consumes_the_equity_force_fire_key(monkeypatch):
    import execution.regime_blended_sizer as _rbs
    monkeypatch.setattr(_rbs, '_check_force_fire_flag',
                        lambda: (_ for _ in ()).throw(AssertionError('force-fire key consumed')))
    seen = {}

    def _fake_path(signals, acct, regime_state, params, confirmer, **kw):
        seen.update(signals=signals, regime_state=regime_state, params=params, **kw)
        return [dict(o) for o in BOOK]
    monkeypatch.setattr(_rbs, '_sharpe_cadence_path', _fake_path)
    orders = crs._default_pooled_runner({'equity': NAV}, 'LOW_VOL', {'liquidity_param': 1.0},
                                        'HIGH_VOL', {'liquidity_param': 0.8})
    assert len(orders) == 4
    assert seen == {'signals': [], 'regime_state': 'LOW_VOL', 'params': {'liquidity_param': 1.0},
                    'crypto_regime_state': 'HIGH_VOL', 'crypto_params': {'liquidity_param': 0.8}}


def test_flag_unset_runs_the_legacy_budget_model(monkeypatch):
    monkeypatch.delenv('OPENCLAW_CRYPTO_POOLED_SIZING', raising=False)

    def _never(*a):
        raise AssertionError('pooled runner must not run with the flag unset')
    orders = crs.size_crypto_positions(
        {'equity': NAV}, {'state': 'LOW_VOL'},
        broker_loader=lambda: {'SPY': 1.0},
        weights_loader=lambda rg: [{'strategy_id': 'S_c', 'daily_weight': 1.0}],
        signals_loader=lambda rg, w: [{'strategy_id': 'S_c', 'ticker': 'BTC-USD',
                                       'direction': 'long'}],
        crypto_strategy_ids={'S_c'}, all_live_weight_sum=2.0, hold_state=NO_HOLD,
        pooled_runner=_never)
    assert orders == [{'ticker': 'BTC-USD', 'direction': 'long', 'notional_usd': 100_000.0,
                       'pct_nav': 1.0, 'strategy_id': 'crypto_redeploy', 'close_only': False}]


# ── scripts/redeploy_crypto.py wiring ────────────────────────────────────────

def _run_script(monkeypatch, orders):
    sys.path.insert(0, str(ROOT / 'scripts'))
    import redeploy_crypto as rc
    import execution.crypto_redeploy_sizer as _crs
    import execution.alpaca_executor as ae
    monkeypatch.setenv('OPENCLAW_CRYPTO_REDEPLOY', '1')
    monkeypatch.setattr(rc, '_redis', lambda: None)
    monkeypatch.setattr(rc, '_load_account_state', lambda: {'equity': NAV})
    import regime.crypto_regime as _cr
    monkeypatch.setattr(_cr, 'load_crypto_regime_state', lambda: {'state': 'HIGH_VOL'})
    monkeypatch.setattr(_crs, 'size_crypto_positions', lambda *a, **k: [dict(o) for o in orders])
    sent = []
    monkeypatch.setattr(ae, 'execute_single',
                        lambda sess, equity, order, run_date: sent.append(order) or {'status': 'submitted'})
    assert rc.main(['--reason', 'CRYPTO_HMM_A_B', '--date', '2026-11-11']) == 0
    return sent


def test_script_pooled_tags_a_wave_for_unique_coids(monkeypatch, capsys):
    sent = _run_script(monkeypatch, [{'ticker': 'BTC-USD', 'direction': 'long',
                                      'pct_nav': 0.1, 'strategy_id': 'S_beta_btc'}])
    assert len(sent) == 1 and len(sent[0]['_wave']) == 6 and sent[0]['_wave'].isdigit()
    assert 'sizing mode: pooled' in capsys.readouterr().out


def test_script_legacy_mode_unchanged(monkeypatch, capsys):
    monkeypatch.delenv('OPENCLAW_CRYPTO_POOLED_SIZING', raising=False)
    sent = _run_script(monkeypatch, [{'ticker': 'BTC-USD', 'direction': 'long',
                                      'pct_nav': 0.1, 'strategy_id': 'crypto_redeploy'}])
    assert len(sent) == 1 and '_wave' not in sent[0]
    assert 'sizing mode: legacy_budget' in capsys.readouterr().out
