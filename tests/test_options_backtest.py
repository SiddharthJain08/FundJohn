import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from strategies.base import Signal, OptionSpec  # noqa


def test_signal_backward_compatible_without_option_spec():
    s = Signal(ticker='AAPL', direction='LONG', entry_price=100.0,
               stop_loss=93.0, target_1=108.0, target_2=0.0, target_3=0.0,
               position_size_pct=0.05, confidence='MED')
    assert s.option_spec is None


def test_option_spec_defaults():
    spec = OptionSpec(underlying='SPY', right='call')
    assert spec.strike_rule == 'target_delta'
    assert spec.target_delta == 0.30
    assert spec.dte_target == 30
    assert spec.structure == 'single'
    assert spec.hedge == 'none'
    assert spec.roll_dte == 7


import numpy as np, pandas as pd
from strategies.base import BaseStrategy
from backtest import options_backtest


def _trending_panels(n=400, drift=0.0008, seed=1):
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, 0.01, n)
    px = 100 * np.cumprod(1 + rets)
    idx = pd.date_range('2022-01-03', periods=n, freq='B')
    close = pd.Series(px, index=idx)
    close_wide = pd.DataFrame({'SPY': close})
    bars = pd.DataFrame({'open': close, 'high': close * 1.005,
                         'low': close * 0.995, 'close': close}, index=idx)
    return close_wide, {'SPY': bars}


class _LongCallStrat(BaseStrategy):
    id = 'T_long_call'; name = 'test long call'; min_lookback = 30
    instrument_class = 'option'; MAX_SIGNALS = 1
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    def generate_signals(self, prices, regime, universe, aux_data=None):
        if 'SPY' not in prices.columns or len(prices) < self.min_lookback:
            return []
        if len(prices) != self.min_lookback + 5:
            return []
        S = float(prices['SPY'].iloc[-1])
        return [Signal(ticker='SPY', direction='LONG', entry_price=S,
                       stop_loss=S * 0.9, target_1=S * 1.1, target_2=0.0, target_3=0.0,
                       position_size_pct=0.05, confidence='MED',
                       option_spec=OptionSpec(underlying='SPY', right='call',
                                              structure='single', dte_target=30))]


def test_single_leg_long_call_produces_trades():
    close_wide, bars = _trending_panels()
    regimes = pd.Series('LOW_VOL', index=close_wide.index)
    inst = _LongCallStrat(); inst.active_in_regimes = list(['LOW_VOL','TRANSITIONING','HIGH_VOL','CRISIS'])
    out = options_backtest.simulate(inst, close_wide, bars, regimes,
                                    close_wide.index[0], close_wide.index[-1],
                                    strategy_id='T_long_call', vrp_factor=1.1)
    assert out['days_with_signals'] >= 1
    assert len(out['trades']) >= 1
    t = out['trades'][0]
    for k in ('ticker', 'direction', 'entry_date', 'exit_date', 'pnl_pct', 'holding_days', 'entry_regime'):
        assert k in t
    assert isinstance(t['pnl_pct'], float)
    assert t['pnl_pct'] > -1.0


class _ShortStraddleStrat(BaseStrategy):
    id = 'T_short_straddle'; name = 'test straddle'; min_lookback = 30
    instrument_class = 'option'; MAX_SIGNALS = 1
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    def generate_signals(self, prices, regime, universe, aux_data=None):
        if 'SPY' not in prices.columns or len(prices) != self.min_lookback + 5:
            return []
        S = float(prices['SPY'].iloc[-1])
        return [Signal(ticker='SPY', direction='SELL_VOL', entry_price=S,
                       stop_loss=S * 0.9, target_1=S * 1.1, target_2=0.0, target_3=0.0,
                       position_size_pct=0.05, confidence='MED',
                       option_spec=OptionSpec(underlying='SPY', structure='straddle',
                                              hedge='delta', dte_target=30, roll_dte=7))]


def test_short_straddle_with_delta_hedge_produces_trade():
    # Calm tape (low realized vol) but priced with a VRP markup → short vol earns the premium.
    close_wide, bars = _trending_panels(drift=0.0, seed=7)
    regimes = pd.Series('LOW_VOL', index=close_wide.index)
    inst = _ShortStraddleStrat()
    out = options_backtest.simulate(inst, close_wide, bars, regimes,
                                    close_wide.index[0], close_wide.index[-1],
                                    strategy_id='T_short_straddle', vrp_factor=1.3)
    assert len(out['trades']) >= 1
    t = out['trades'][0]
    assert t['direction'] == 'short'
    assert 'hedge_pnl_pct' in t


def test_roll_produces_multiple_cycles_over_long_hold():
    close_wide, bars = _trending_panels(n=400, drift=0.0, seed=3)
    regimes = pd.Series('LOW_VOL', index=close_wide.index)
    inst = _ShortStraddleStrat()
    out = options_backtest.simulate(inst, close_wide, bars, regimes,
                                    close_wide.index[0], close_wide.index[-1],
                                    strategy_id='T_short_straddle', vrp_factor=1.3,
                                    max_hold_days=120)
    # 120 trading days / ~30-DTE rolls → expect >1 cycle from the single signal
    assert len(out['trades']) >= 2


def test_delta_hedge_reduces_directional_loss_and_has_signal_keys():
    import numpy as np, pandas as pd
    from backtest import options_backtest as ob
    from strategies.base import OptionSpec
    idx = pd.date_range('2022-01-03', periods=90, freq='B')
    trend = pd.Series(100 * np.cumprod(1 + np.full(90, 0.005)), index=idx)  # +0.5%/day
    hedged = OptionSpec(underlying='X', structure='straddle', hedge='delta', dte_target=30, roll_dte=7)
    naked  = OptionSpec(underlying='X', structure='straddle', hedge='none',  dte_target=30, roll_dte=7)
    ch = ob._price_multileg_cycle(hedged, trend, idx[0], -1, 1.2, 21, 30)
    cn = ob._price_multileg_cycle(naked,  trend, idx[0], -1, 1.2, 21, 30)
    # short straddle in an up-trend: delta-hedge must shrink the directional loss
    assert abs(ch['pnl_pct']) < abs(cn['pnl_pct'])
    assert ch['hedge_pnl_pct'] > 0           # long-share hedge gains in the up-move
    assert ch['signal_stop'] is None and ch['signal_target'] is None
