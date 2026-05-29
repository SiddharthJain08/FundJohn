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


from strategies.implementations.S_sector_flow_confirmed_momentum import SectorFlowConfirmedMomentum


def _sector_prices():
    idx = pd.bdate_range('2023-01-01', periods=300)
    return pd.DataFrame({
        'AAPL': np.linspace(100, 170, 300),
        'MSFT': np.linspace(100, 160, 300),
        'XOM':  np.linspace(150, 90, 300),
        'XLK':  np.linspace(80, 130, 300),
        'XLE':  np.linspace(130, 85, 300),
        # Mixed broad market (SPY up, QQQ down) so confirm-mode can corroborate BOTH a
        # LONG (AAPL: XLK up + SPY up) and a SHORT (XOM: XLE down + QQQ down) in one bar —
        # sector_flow.confirm requires >=1 broad ETF aligned with the trade direction.
        'SPY':  np.linspace(380, 520, 300),
        'QQQ':  np.linspace(460, 300, 300),
    }, index=idx)


def test_sector_strategy_confirmation_mode_longs_aligned_names():
    s = SectorFlowConfirmedMomentum({'mode': 'confirm'})
    sigs = s.generate_signals(_sector_prices(), {'state': 'LOW_VOL'},
                              ['AAPL', 'MSFT', 'XOM'], None)
    dirs = {sig.ticker: sig.direction for sig in sigs}
    assert dirs.get('AAPL') == 'LONG'
    assert dirs.get('XOM') == 'SHORT'


def test_sector_strategy_basket_mode_emits_constituents_both_sides():
    s = SectorFlowConfirmedMomentum({'mode': 'basket', 'top_sectors': 1})
    sigs = s.generate_signals(_sector_prices(), {'state': 'LOW_VOL'},
                              ['AAPL', 'MSFT', 'XOM'], None)
    dirs = {sig.ticker: sig.direction for sig in sigs}
    assert dirs.get('AAPL') == 'LONG'
    assert dirs.get('XOM') == 'SHORT'


def test_sector_strategy_env_override_forces_basket(monkeypatch):
    monkeypatch.setenv('OPENCLAW_SECTOR_MODE', 'basket')
    s = SectorFlowConfirmedMomentum({'mode': 'confirm', 'top_sectors': 1})  # default says confirm
    sigs = s.generate_signals(_sector_prices(), {'state': 'LOW_VOL'}, ['AAPL', 'MSFT', 'XOM'], None)
    # env override -> basket mode runs (params with mode/sector present)
    assert any(sig.signal_params.get('mode') == 'basket' for sig in sigs)


def test_sector_strategy_empty_prices_safe():
    s = SectorFlowConfirmedMomentum()
    assert s.generate_signals(pd.DataFrame(), {'state': 'LOW_VOL'}, ['AAPL'], None) == []
