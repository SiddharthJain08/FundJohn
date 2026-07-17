# tests/test_bracket_stacking_backtest.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import pandas as pd
from scripts.backtest_bracket_stacking import compare_event


def _bars():
    # 6 forward sessions; rises ~+12% then pulls back. high tags 110 on day 3,
    # 112 on day 4, never hits 115.
    idx = pd.to_datetime(['2026-01-02', '2026-01-03', '2026-01-04',
                          '2026-01-05', '2026-01-06', '2026-01-07'])
    return pd.DataFrame({
        'high':  [103, 108, 110, 112, 111, 109],
        'low':   [ 99, 102, 106, 108, 107, 105],
        'close': [102, 107, 109, 111, 109, 106],
    }, index=idx)


def test_compare_event_distinguishes_policies():
    # Two uncorrelated blocks, both long. Max-weight pick (B) carries tp 5%; the OTHER
    # block (A) carries the larger 8% take -> sharpe-wtd stacked take = 8%·⅔+5%·⅓ = 7%.
    cands = [
        {'sid': 'A', 'direction': 1, 'weight': 5.0, 'entry': 100.0,
         'stop': 98.0, 't1': 108.0, 't2': None},   # block1: stop 2%, tp 8% (the max take)
        {'sid': 'B', 'direction': 1, 'weight': 9.0, 'entry': 100.0,
         'stop': 96.0, 't1': 105.0, 't2': None},   # block2: stop 4%, tp 5% (max-weight pick)
    ]
    res = compare_event(
        ticker='TST', dir_sign=1, candidates=cands,
        bars=_bars(), entry_date=pd.Timestamp('2026-01-01'),
        block_map={'A': 1, 'B': 2}, eff_sharpe={'A': 2.0, 'B': 1.0},
        max_hold_days=5)
    # current = max-weight pick (B): tp 105 -> target hit day2 (high 108>=105) -> +5%.
    assert res['current']['exit_reason'] == 'target'
    assert abs(res['current']['pnl_pct'] - 0.05) < 1e-9
    # stacked: tp = sharpe-wtd mean = +7% (107) -> target hit day2 (high 108) -> +7%;
    # stop = 2%·⅔ + 4%·⅓ = 8/3 % (97.33, never hit).
    assert res['stacked']['exit_reason'] == 'target'
    assert abs(res['stacked']['pnl_pct'] - 0.07) < 1e-9
