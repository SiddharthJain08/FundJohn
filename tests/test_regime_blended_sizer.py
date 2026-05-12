from __future__ import annotations

import pytest
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.regime_blended_sizer import size_positions, _select_mode  # noqa: E402

def test_select_mode_low_vol_consolidate():
    assert _select_mode('LOW_VOL') == 'consolidate'
    assert _select_mode('TRANSITIONING') == 'consolidate'

def test_select_mode_high_vol_independent():
    assert _select_mode('HIGH_VOL') == 'independent'
    assert _select_mode('CRISIS') == 'independent'

def test_select_mode_unknown_defaults_independent():
    # Defensive: unknown regime → safest mode (independent, no LLM, mechanical sizing).
    assert _select_mode('UNKNOWN') == 'independent'

def _sig(sid, ticker='AAPL', direction=1, kelly_p=0.4, memo_mult=1.0,
         entry=100, stop=95, t1=110, target_pct_nav=0.05):
    return {
        'signal_id': hash((sid, ticker, direction)),
        'strategy_id': sid, 'ticker': ticker, 'direction': direction,
        'kelly_p': kelly_p, 'strategy_memo_mult': memo_mult,
        'target_pct_nav': target_pct_nav,
        'entry_price': entry, 'stop_loss': stop, 'take_profit_1': t1,
    }

def _account(equity=100_000, regt_bp=400_000):
    return {'equity': equity, 'regt_buying_power': regt_bp,
            'long_market_value': 0, 'cash': equity}

def _params(regime):
    base = {'min_signal_notional_usd': 100, 'position_circuit_breaker_pct': 0.02}
    return {**base, 'liquidity_param':
            {'LOW_VOL': 1.0, 'TRANSITIONING': 0.75, 'HIGH_VOL': 0.5, 'CRISIS': 0.25}[regime]}

def test_low_vol_consolidate_calls_tradejohn_and_emits_per_ticker():
    sigs = [_sig('S1', 'AAPL', 1, kelly_p=0.4),
            _sig('S2', 'AAPL', 1, kelly_p=0.3)]
    state = {'S1': {'last_fire_date': None, 'next_fire_date': None},
             'S2': {'last_fire_date': None, 'next_fire_date': None}}
    confirmer_called = []
    def fake_confirmer(proposals, runner=None):
        confirmer_called.append(proposals)
        return {p['ticker']: {'action': 'approve', 'multiplier': 1.0, 'rationale': ''}
                for p in proposals}
    orders = size_positions(
        signals=sigs, account_state=_account(), regime={'state': 'LOW_VOL'},
        run_date=date(2026, 5, 12), strategy_state=state,
        regime_params=_params('LOW_VOL'),
        confirmer=fake_confirmer,
    )
    assert len(orders) == 1
    assert orders[0]['ticker'] == 'AAPL'
    assert len(confirmer_called) == 1

def test_high_vol_independent_skips_tradejohn_uses_target_pct_nav():
    sigs = [_sig('S1', 'AAPL', 1, target_pct_nav=0.05)]
    state = {'S1': {'last_fire_date': None, 'next_fire_date': None}}
    orders = size_positions(
        signals=sigs, account_state=_account(equity=100_000), regime={'state': 'HIGH_VOL'},
        run_date=date(2026, 5, 12), strategy_state=state,
        regime_params=_params('HIGH_VOL'),
        confirmer=lambda p, runner=None: {},  # should not be called
    )
    assert len(orders) == 1
    # qty = (target_pct_nav × NAV × λ) / entry = (0.05 × 100_000 × 0.5) / 100 = 25
    assert orders[0]['qty'] == pytest.approx(25.0)

def test_tradejohn_veto_zeroes_size():
    sigs = [_sig('S1', 'AAPL', 1, kelly_p=0.5)]
    state = {'S1': {'last_fire_date': None, 'next_fire_date': None}}
    def vetoing(proposals, runner=None):
        return {p['ticker']: {'action': 'veto', 'multiplier': 0.0, 'rationale': 'earnings'}
                for p in proposals}
    orders = size_positions(
        signals=sigs, account_state=_account(), regime={'state': 'LOW_VOL'},
        run_date=date(2026, 5, 12), strategy_state=state,
        regime_params=_params('LOW_VOL'), confirmer=vetoing,
    )
    assert orders == []

def test_tradejohn_scale_applies_multiplier():
    sigs = [_sig('S1', 'AAPL', 1, kelly_p=0.5, memo_mult=1.0)]
    state = {'S1': {'last_fire_date': None, 'next_fire_date': None}}
    def scaling(proposals, runner=None):
        return {p['ticker']: {'action': 'scale', 'multiplier': 0.5, 'rationale': ''}
                for p in proposals}
    orders = size_positions(
        signals=sigs, account_state=_account(regt_bp=400_000), regime={'state': 'LOW_VOL'},
        run_date=date(2026, 5, 12), strategy_state=state,
        regime_params=_params('LOW_VOL'), confirmer=scaling,
    )
    # preliminary = 0.5 × 1.0 × 400_000 × 1.0 = 200_000; scaled = 100_000
    assert orders[0]['notional_usd'] == pytest.approx(100_000.0)

def test_cadence_pending_signal_skipped():
    sigs = [_sig('S1', 'AAPL', 1)]
    state = {'S1': {'last_fire_date': date(2026, 5, 11),
                    'next_fire_date': date(2026, 5, 14),
                    'avg_holding_days': 3.0, 'source': 'live_signal_pnl'}}
    orders = size_positions(
        signals=sigs, account_state=_account(), regime={'state': 'LOW_VOL'},
        run_date=date(2026, 5, 12), strategy_state=state,
        regime_params=_params('LOW_VOL'),
        confirmer=lambda p, runner=None: {},
    )
    assert orders == []

def test_high_vol_missing_target_pct_nav_falls_back_one_percent(caplog):
    sig = _sig('S1', 'AAPL', 1)
    sig.pop('target_pct_nav')  # simulate strategy missing from sizing recs
    state = {'S1': {'last_fire_date': None, 'next_fire_date': None}}
    orders = size_positions(
        signals=[sig], account_state=_account(equity=100_000), regime={'state': 'HIGH_VOL'},
        run_date=date(2026, 5, 12), strategy_state=state,
        regime_params=_params('HIGH_VOL'),
        confirmer=lambda p, runner=None: {},
    )
    # 1% NAV fallback × λ=0.5 / entry=100 = 5
    assert orders[0]['qty'] == pytest.approx(5.0)
    assert any('missing_strategy_sizing' in rec.message for rec in caplog.records)
