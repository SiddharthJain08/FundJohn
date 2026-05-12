"""End-to-end: HIGH_VOL independent path skips LLM, uses target_pct_nav × NAV × λ."""
import sys
from datetime import date
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

pytestmark = pytest.mark.integration


def _build_signals():
    """4 strategies, 6 signals across 6 different tickers (no consolidation in independent mode)."""
    return [
        {'signal_id': 'sig-H1-AAPL', 'strategy_id': 'H1', 'ticker': 'AAPL',
         'direction': 1, 'entry': 100, 'stop': 95, 't1': 110, 'p_t1': 0.6,
         'entry_price': 100, 'stop_loss': 95, 'take_profit_1': 110, 'target_1': 110,
         'target_pct_nav': 0.05, 'strategy_memo_mult': 1.0},
        {'signal_id': 'sig-H2-MSFT', 'strategy_id': 'H2', 'ticker': 'MSFT',
         'direction': 1, 'entry': 200, 'stop': 190, 't1': 220, 'p_t1': 0.6,
         'entry_price': 200, 'stop_loss': 190, 'take_profit_1': 220, 'target_1': 220,
         'target_pct_nav': 0.05, 'strategy_memo_mult': 1.0},
        {'signal_id': 'sig-H3-GOOGL', 'strategy_id': 'H3', 'ticker': 'GOOGL',
         'direction': -1, 'entry': 150, 'stop': 158, 't1': 135, 'p_t1': 0.6,
         'entry_price': 150, 'stop_loss': 158, 'take_profit_1': 135, 'target_1': 135,
         'target_pct_nav': 0.05, 'strategy_memo_mult': 1.0},
        {'signal_id': 'sig-H1-AMZN', 'strategy_id': 'H1', 'ticker': 'AMZN',
         'direction': 1, 'entry': 180, 'stop': 170, 't1': 200, 'p_t1': 0.6,
         'entry_price': 180, 'stop_loss': 170, 'take_profit_1': 200, 'target_1': 200,
         'target_pct_nav': 0.05, 'strategy_memo_mult': 1.0},
        {'signal_id': 'sig-H2-NVDA', 'strategy_id': 'H2', 'ticker': 'NVDA',
         'direction': 1, 'entry': 800, 'stop': 760, 't1': 880, 'p_t1': 0.6,
         'entry_price': 800, 'stop_loss': 760, 'take_profit_1': 880, 'target_1': 880,
         'target_pct_nav': 0.05, 'strategy_memo_mult': 1.0},
        {'signal_id': 'sig-H4-TSLA', 'strategy_id': 'H4', 'ticker': 'TSLA',
         'direction': -1, 'entry': 250, 'stop': 263, 't1': 225, 'p_t1': 0.6,
         'entry_price': 250, 'stop_loss': 263, 'take_profit_1': 225, 'target_1': 225,
         'target_pct_nav': 0.05, 'strategy_memo_mult': 1.0},
    ]


def test_high_vol_independent_cycle_emits_per_signal_orders():
    from execution.regime_blended_sizer import size_positions

    signals = _build_signals()
    account = {'equity': 100_000, 'regt_buying_power': 400_000,
               'long_market_value': 0, 'cash': 100_000}
    regime = {'state': 'HIGH_VOL'}
    params = {'liquidity_param': 0.5, 'min_signal_notional_usd': 100,
              'position_circuit_breaker_pct': 0.01}
    state = {sig['strategy_id']: {'last_fire_date': None, 'next_fire_date': None,
                                   'avg_holding_days': 1.0, 'source': 'bootstrap_daily'}
             for sig in signals}

    confirmer_calls = []

    def fake_confirmer(proposals, runner=None):
        confirmer_calls.append(proposals)
        return {p['ticker']: {'action': 'approve', 'multiplier': 1.0, 'rationale': 'test'}
                for p in proposals}

    orders = size_positions(
        signals=signals, account_state=account, regime=regime,
        run_date=date(2026, 5, 12), strategy_state=state,
        regime_params=params, confirmer=fake_confirmer,
    )

    assert len(orders) == 6, f'expected 6 per-signal orders, got {len(orders)}'
    # No consolidation — each ticker has exactly one order
    tickers = sorted(o['ticker'] for o in orders)
    assert tickers == ['AAPL', 'AMZN', 'GOOGL', 'MSFT', 'NVDA', 'TSLA']

    # TradeJohn confirmer NOT called in independent path
    assert len(confirmer_calls) == 0

    # All orders have source_mode='independent'
    assert all(o['source_mode'] == 'independent' for o in orders)

    # Sizing math for AAPL: target_pct_nav=0.05 × NAV=$100k × λ=0.5 / entry=100 = 25 shares = $2500 notional
    aapl = next(o for o in orders if o['ticker'] == 'AAPL')
    assert aapl['qty'] == pytest.approx(25.0)
    assert aapl['notional_usd'] == pytest.approx(2500.0)


def test_high_vol_missing_target_pct_nav_falls_back_to_one_percent(caplog):
    import logging
    from execution.regime_blended_sizer import size_positions

    sig = {'signal_id': 'sig-MISSING', 'strategy_id': 'S_unsized', 'ticker': 'AAPL',
           'direction': 1, 'entry': 100, 'stop': 95, 't1': 110, 'p_t1': 0.6,
           'entry_price': 100, 'stop_loss': 95, 'take_profit_1': 110, 'target_1': 110,
           'strategy_memo_mult': 1.0}
    # No target_pct_nav set
    account = {'equity': 100_000, 'regt_buying_power': 400_000,
               'long_market_value': 0, 'cash': 100_000}
    state = {'S_unsized': {'last_fire_date': None, 'next_fire_date': None,
                            'avg_holding_days': 1.0, 'source': 'bootstrap_daily'}}
    params = {'liquidity_param': 0.5, 'min_signal_notional_usd': 100,
              'position_circuit_breaker_pct': 0.01}

    with caplog.at_level(logging.WARNING, logger='execution.regime_blended_sizer'):
        orders = size_positions(
            signals=[sig], account_state=account, regime={'state': 'HIGH_VOL'},
            run_date=date(2026, 5, 12), strategy_state=state,
            regime_params=params, confirmer=lambda p, runner=None: {},
        )
    assert len(orders) == 1
    # Fallback: 1% NAV × λ=0.5 / entry=100 = 5 shares
    assert orders[0]['qty'] == pytest.approx(5.0)
    assert any('missing_strategy_sizing' in rec.getMessage() for rec in caplog.records)
