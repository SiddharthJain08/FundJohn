"""Regression test for the sample-size weighted Sharpe blend.

Pure function — no DB. Runs standalone with `python3 tests/test_sharpe_blend.py`.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from execution.strategy_weights import _effective_sharpe


def test_blend_typical():
    bt   = {'bt_sharpe': 2.0, 'bt_n': 200}
    live = {'live_sharpe': 1.0, 'live_n': 50}
    eff, *_ = _effective_sharpe(bt, live)
    expected = (200 * 2.0 + 50 * 1.0) / 250  # = 1.8
    assert abs(eff - expected) < 1e-9, f'{eff} vs {expected}'


def test_live_only():
    eff, *_ = _effective_sharpe(None, {'live_sharpe': 0.8, 'live_n': 30})
    assert eff == 0.8


def test_bt_only():
    eff, *_ = _effective_sharpe({'bt_sharpe': 1.5, 'bt_n': 100}, None)
    assert eff == 1.5


def test_both_missing():
    eff, *_ = _effective_sharpe(None, None)
    assert eff is None


def test_live_zero_sample():
    """Newly-promoted strategy: bt only, live_n=0 → eff = bt."""
    eff, *_ = _effective_sharpe(
        {'bt_sharpe': 2.5, 'bt_n': 100},
        {'live_sharpe': None, 'live_n': 0},
    )
    assert eff == 2.5


def test_live_overtakes_backtest():
    """As live trades accumulate, the blend skews toward live."""
    # Year 1: bt_n=200 outweighs live_n=10
    eff_year1, *_ = _effective_sharpe({'bt_sharpe': 2.0, 'bt_n': 200}, {'live_sharpe': 0.5, 'live_n': 10})
    # Year 5: live_n=500 now dominates bt_n=200
    eff_year5, *_ = _effective_sharpe({'bt_sharpe': 2.0, 'bt_n': 200}, {'live_sharpe': 0.5, 'live_n': 500})
    # year5 should be much closer to 0.5 than year1
    assert abs(eff_year5 - 0.5) < abs(eff_year1 - 0.5)


if __name__ == '__main__':
    fns = [(n, fn) for n, fn in list(globals().items()) if n.startswith('test_') and callable(fn)]
    fails = 0
    for name, fn in fns:
        try:
            fn()
            print('PASS', name)
        except AssertionError as e:
            print('FAIL', name, str(e))
            fails += 1
    sys.exit(1 if fails else 0)
