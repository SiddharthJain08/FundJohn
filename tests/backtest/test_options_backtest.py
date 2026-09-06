import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
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


def test_simulate_dispatch_selects_correct_path():
    # The dispatch is a module-level selector so it's verifiable without a DB.
    from backtest import unified_backtest as ub
    from backtest import options_backtest as ob
    assert ub._simulate_for('option') is ob.simulate
    assert ub._simulate_for('equity') is ub._per_bar_simulate
    assert ub._simulate_for('crypto') is ub._per_bar_simulate
    assert ub._simulate_for('etp') is ub._per_bar_simulate


def test_parity_mae_helper():
    from scripts.options_parity_check import mae_fraction
    synth = [1.0, 2.0, 3.0]; real = [1.1, 1.8, 3.3]
    m = mae_fraction(synth, real)
    # mean(|.1|/1.1, |.2|/1.8, |.3|/3.3) ≈ mean(0.0909,0.111,0.0909)=0.0976
    assert abs(m - 0.0976) < 0.01


def test_iv_mae_helper_perfect_match_is_zero():
    from scripts.options_parity_check import iv_mae
    # identical synthetic vs real IV -> 0 MAE
    assert iv_mae([0.2, 0.3, 0.25], [0.2, 0.3, 0.25]) == 0.0
    # 10% high across the board -> 0.10 MAE
    assert abs(iv_mae([0.22, 0.33], [0.20, 0.30]) - 0.10) < 1e-9


def test_auto_backtest_refuses_option_strategy(tmp_path):
    # 2026-07-17: fixture updated — this test used to point at the live
    # S_short_straddle_vrp.py, which was reaped from the repo on 2026-07-14
    # (commit 1c2a17f, operator lifted the reaper exemption). The refusal
    # guard itself is UNCHANGED since it was added in commit 02cbf92 (SP-4 P0):
    # auto_backtest.run_backtest raises ValueError for instrument_class='option'
    # after contract validation, directing callers to
    # backtest.unified_backtest.run_backtest(..., instrument_class='option').
    # Pin the guard with a self-contained tmp_path fixture so the test no
    # longer depends on which strategies happen to be in the manifest.
    import importlib
    auto = importlib.import_module('strategies.auto_backtest')
    fp = tmp_path / 'S_tmp_option_refusal.py'
    fp.write_text(
        "from strategies.base import BaseStrategy\n"
        "\n"
        "class TmpOptionRefusalStrat(BaseStrategy):\n"
        "    id = 'T_tmp_option_refusal'; name = 'tmp option refusal fixture'\n"
        "    instrument_class = 'option'\n"
        "    active_in_regimes = ['LOW_VOL']\n"
        "\n"
        "    def generate_signals(self, prices, regime, universe, aux_data=None):\n"
        "        return []\n"
    )
    import pytest
    with pytest.raises(ValueError, match='option'):
        auto.run_backtest(str(fp))


# ── spec 2026-09-06 B.4: dividends, American exercise, dte-aware IV ──
def _isolated_masters(tmp_path, monkeypatch, spy_q=0.02):
    """No production masters: an empty surface path, a tiny corporate_actions file
    that gives SPY a `spy_q` trailing yield at a 500 spot, and no VIX9D file."""
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE_PATH', str(tmp_path / 'no_surface.parquet'))
    monkeypatch.setenv('OPENCLAW_VOL_INDICES_PARQUET', str(tmp_path / 'no_vol.parquet'))
    rows = [{'symbol': 'SPY', 'action_type': 'cash_dividend', 'ex_date': d.date(), 'cash_amount': spy_q * 500.0 / 4}
            for d in pd.date_range('2019-01-15', '2026-09-01', freq='3MS')]
    p = tmp_path / 'corporate_actions.parquet'; pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv('OPENCLAW_CORPORATE_ACTIONS_PARQUET', str(p))
    from backtest import dividends, synthetic_iv, vol_index
    dividends.clear_cache(); synthetic_iv.clear_cache(); vol_index._vix9d_series.cache_clear()


def test_option_spec_exercise_defaults_to_american_and_round_trips():
    from strategies.base import OptionSpec
    assert OptionSpec(underlying='SPY').exercise == 'american'
    assert OptionSpec.from_dict({'underlying': 'SPY', 'exercise': 'european'}).exercise == 'european'


def test_american_put_cycle_costs_at_least_the_european_one(tmp_path, monkeypatch):
    from backtest import options_backtest as ob
    from strategies.base import OptionSpec
    _isolated_masters(tmp_path, monkeypatch, spy_q=0.03)
    monkeypatch.setattr(ob, 'synthetic_iv_detail', lambda *a, **k: (0.30, 'realized'))
    idx = pd.date_range('2025-06-02', periods=60, freq='B')
    flat = pd.Series(500.0, index=idx)
    am = OptionSpec(underlying='SPY', right='put', strike_rule='atm', dte_target=30, exercise='american')
    eu = OptionSpec(underlying='SPY', right='put', strike_rule='atm', dte_target=30, exercise='european')
    stats = ob._new_stats()
    ca = ob._price_single_cycle(am, flat, idx[0], +1, 1.2, 21, 10, stats=stats)
    ce = ob._price_single_cycle(eu, flat, idx[0], +1, 1.2, 21, 10)
    assert ca['entry_price'] >= ce['entry_price'] > 0
    assert stats['q_positive'] > 0 and stats['exercise'] == {'american'}


def test_simulate_logs_iv_sources_and_keeps_trade_keys(tmp_path, monkeypatch, caplog):
    import logging
    _isolated_masters(tmp_path, monkeypatch)
    close_wide, bars = _trending_panels()
    regimes = pd.Series('LOW_VOL', index=close_wide.index)
    inst = _LongCallStrat()
    with caplog.at_level(logging.INFO):
        out = options_backtest.simulate(inst, close_wide, bars, regimes, close_wide.index[0], close_wide.index[-1],
                                        strategy_id='T_long_call', vrp_factor=1.1)
    assert len(out['trades']) >= 1
    t = out['trades'][0]
    assert set(t) == {'entry_date', 'exit_date', 'entry_price', 'exit_price', 'exit_reason', 'holding_days',
                      'pnl_pct', 'strike', 'expiry', 'iv_entry', 'signal_stop', 'signal_target',
                      'ticker', 'direction', 'entry_regime'}
    line = [r.message for r in caplog.records if r.message.startswith('[options_backtest] iv sources:')]
    assert len(line) == 1 and 'realized=' in line[0] and 'exercise=american' in line[0]
    # the 2022 panel predates the dividend fixture's coverage + 365 d?  No — coverage starts 2019, so q > 0 applies
    assert 'q>0 on 0 prices' not in line[0]
