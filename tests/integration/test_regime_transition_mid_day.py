"""Mid-day regime transition: LOW_VOL→CRISIS flips mode dispatch."""
import sys
from datetime import date
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

pytestmark = pytest.mark.integration


def _signals_for_strategies(strategy_ids, ticker='AAPL'):
    return [{
        'signal_id': f'sig-{sid}',
        'strategy_id': sid,
        'ticker': ticker,
        'direction': 1,
        'entry': 100, 'stop': 95, 't1': 110, 'p_t1': 0.6,
        'entry_price': 100, 'stop_loss': 95, 'take_profit_1': 110, 'target_1': 110,
        'target_pct_nav': 0.05, 'strategy_memo_mult': 1.0,
    } for sid in strategy_ids]


def test_low_vol_to_crisis_mode_flip():
    """First call in LOW_VOL produces consolidate path; second call in CRISIS produces independent path."""
    from execution.regime_blended_sizer import size_positions

    signals = _signals_for_strategies(['S1', 'S2', 'S3'])
    account = {'equity': 100_000, 'regt_buying_power': 400_000,
               'long_market_value': 0, 'cash': 100_000}
    state = {sid: {'last_fire_date': None, 'next_fire_date': None,
                   'avg_holding_days': 1.0, 'source': 'bootstrap_daily'}
             for sid in ['S1', 'S2', 'S3']}

    confirmer_call_count = [0]

    def confirmer(proposals, runner=None):
        confirmer_call_count[0] += 1
        return {p['ticker']: {'action': 'keep', 'rationale': ''}
                for p in proposals}

    # Cycle 1: LOW_VOL → consolidate (1 ticker, calls confirmer)
    low_vol_orders = size_positions(
        signals=signals, account_state=account, regime={'state': 'LOW_VOL'},
        run_date=date(2026, 5, 12), strategy_state=state,
        regime_params={'liquidity_param': 1.0, 'min_signal_notional_usd': 100,
                       'position_circuit_breaker_pct': 0.02},
        confirmer=confirmer,
    )
    assert len(low_vol_orders) == 1, (
        f'LOW_VOL: all 3 signals on same ticker should consolidate to 1 order, got {len(low_vol_orders)}'
    )
    assert low_vol_orders[0]['source_mode'] == 'consolidate'
    assert confirmer_call_count[0] == 1

    # Cycle 2: CRISIS (after liquidator fired between cycles) → independent (3 orders, no LLM)
    crisis_orders = size_positions(
        signals=signals, account_state=account, regime={'state': 'CRISIS'},
        run_date=date(2026, 5, 13), strategy_state=state,
        regime_params={'liquidity_param': 0.25, 'min_signal_notional_usd': 500,
                       'position_circuit_breaker_pct': 0.005},
        confirmer=confirmer,
    )
    assert len(crisis_orders) == 3, (
        f'CRISIS: 3 signals should emit 3 independent orders (no consolidation), got {len(crisis_orders)}'
    )
    assert all(o['source_mode'] == 'independent' for o in crisis_orders)
    assert confirmer_call_count[0] == 1  # Still 1 — CRISIS skipped confirmer
