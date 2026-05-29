import numpy as np
import pandas as pd
from strategies.implementations.S_options_flow_confirmed_momentum import OptionsFlowConfirmedMomentum


def _prices():
    idx = pd.bdate_range('2023-06-01', periods=120)
    return pd.DataFrame({
        'AAA': np.linspace(100, 160, 120),
        'BBB': np.linspace(100, 60, 120),
        'CCC': np.full(120, 100.0),
    }, index=idx)


def test_options_strategy_longs_confirmed_by_bullish_flow():
    s = OptionsFlowConfirmedMomentum()
    regime = {'state': 'LOW_VOL'}
    aux = {'options': {
        'AAA': {'pc_ratio': 0.5, 'skew_20d': -0.03},
        'BBB': {'pc_ratio': 1.3, 'skew_20d': 0.04},
    }}
    sigs = s.generate_signals(_prices(), regime, ['AAA', 'BBB', 'CCC'], aux)
    by_dir = {sig.ticker: sig.direction for sig in sigs}
    assert by_dir.get('AAA') == 'LONG'
    assert by_dir.get('BBB') == 'SHORT'


def test_options_strategy_skips_when_flow_contradicts():
    s = OptionsFlowConfirmedMomentum()
    regime = {'state': 'LOW_VOL'}
    aux = {'options': {'AAA': {'pc_ratio': 1.5}}}
    sigs = s.generate_signals(_prices(), regime, ['AAA', 'BBB', 'CCC'], aux)
    assert all(sig.ticker != 'AAA' for sig in sigs)


def test_options_strategy_no_options_no_signals():
    s = OptionsFlowConfirmedMomentum()
    sigs = s.generate_signals(_prices(), {'state': 'LOW_VOL'}, ['AAA', 'BBB'], {'options': {}})
    assert sigs == []


def test_options_strategy_empty_prices_safe():
    s = OptionsFlowConfirmedMomentum()
    assert s.generate_signals(pd.DataFrame(), {'state': 'LOW_VOL'}, ['AAA'], None) == []
