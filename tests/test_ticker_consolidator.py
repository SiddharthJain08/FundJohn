import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import pytest
from execution.ticker_consolidator import consolidate, _direction_leader_bracket

def _sig(sid, ticker, direction, kelly_p=0.5, memo_mult=1.0,
         entry=100.0, stop=95.0, t1=110.0, signal_id=None):
    return {
        'signal_id': signal_id or hash((sid, ticker)),
        'strategy_id': sid, 'ticker': ticker, 'direction': direction,
        'kelly_p': kelly_p, 'strategy_memo_mult': memo_mult,
        'entry_price': entry, 'stop_loss': stop, 'take_profit_1': t1,
    }

PARAMS = {'liquidity_param': 1.0, 'min_signal_notional_usd': 100,
          'position_circuit_breaker_pct': 0.02}

def test_three_longs_net_long():
    sigs = [_sig('S1', 'AAPL', 1, kelly_p=0.4),
            _sig('S2', 'AAPL', 1, kelly_p=0.3),
            _sig('S3', 'AAPL', 1, kelly_p=0.5)]
    out = consolidate(sigs, regt_buying_power=100_000, params=PARAMS)
    assert len(out) == 1
    o = out[0]
    assert o['ticker'] == 'AAPL'
    assert o['direction'] == 1
    assert o['preliminary_size_usd'] == pytest.approx((0.4 + 0.3 + 0.5) * 100_000 * 1.0)
    assert sum(c['attribution_weight'] for c in o['contributions']) == pytest.approx(1.0)

def test_two_longs_one_short_nets_long():
    sigs = [_sig('S1', 'AAPL', 1, kelly_p=0.5),
            _sig('S2', 'AAPL', 1, kelly_p=0.3),
            _sig('S3', 'AAPL', -1, kelly_p=0.2)]
    out = consolidate(sigs, regt_buying_power=100_000, params=PARAMS)
    assert len(out) == 1
    assert out[0]['direction'] == 1
    assert out[0]['preliminary_size_usd'] == pytest.approx((0.5 + 0.3 - 0.2) * 100_000)

def test_direction_tie_emits_no_order():
    sigs = [_sig('S1', 'AAPL', 1, kelly_p=0.5),
            _sig('S2', 'AAPL', -1, kelly_p=0.5)]
    out = consolidate(sigs, regt_buying_power=100_000, params=PARAMS)
    assert out == []

def test_single_signal_degenerate():
    sigs = [_sig('S1', 'AAPL', 1, kelly_p=0.4)]
    out = consolidate(sigs, regt_buying_power=100_000, params=PARAMS)
    assert len(out) == 1
    assert out[0]['contributions'][0]['attribution_weight'] == 1.0

def test_below_min_notional_skipped():
    sigs = [_sig('S1', 'AAPL', 1, kelly_p=0.0001)]
    out = consolidate(sigs, regt_buying_power=100_000, params=PARAMS)
    assert out == []

def test_liquidity_param_scales_size():
    sigs = [_sig('S1', 'AAPL', 1, kelly_p=0.5)]
    out_full = consolidate(sigs, regt_buying_power=100_000, params={**PARAMS, 'liquidity_param': 1.0})
    out_half = consolidate(sigs, regt_buying_power=100_000, params={**PARAMS, 'liquidity_param': 0.5})
    assert out_half[0]['preliminary_size_usd'] == pytest.approx(out_full[0]['preliminary_size_usd'] * 0.5)

def test_memo_multiplier_in_er_weight():
    sigs = [_sig('S1', 'AAPL', 1, kelly_p=0.4, memo_mult=2.0)]
    out = consolidate(sigs, regt_buying_power=100_000, params=PARAMS)
    assert out[0]['preliminary_size_usd'] == pytest.approx(0.4 * 2.0 * 100_000)

def test_direction_leader_bracket_picks_largest_long():
    sigs = [_sig('S1', 'AAPL', 1, kelly_p=0.2, stop=98, t1=105),
            _sig('S2', 'AAPL', 1, kelly_p=0.5, stop=92, t1=120)]
    bracket = _direction_leader_bracket(sigs, winning_direction=1)
    assert bracket['stop_loss'] == 92
    assert bracket['take_profit_1'] == 120

def test_multi_ticker_independent_groups():
    sigs = [_sig('S1', 'AAPL', 1, kelly_p=0.3),
            _sig('S2', 'MSFT', -1, kelly_p=0.4)]
    out = consolidate(sigs, regt_buying_power=100_000, params=PARAMS)
    assert {o['ticker'] for o in out} == {'AAPL', 'MSFT'}
