import numpy as np
import pandas as pd
import pytest
from strategies.confirmation import momentum_base as mb


def _ramp_prices():
    idx = pd.bdate_range('2024-01-01', periods=120)
    return pd.DataFrame({
        'AAA': np.linspace(100, 150, 120),
        'BBB': np.full(120, 100.0),
        'CCC': np.linspace(100, 70, 120),
    }, index=idx)


def test_momentum_scores_signs():
    p = _ramp_prices()
    scores = mb.momentum_scores(p, ['AAA', 'BBB', 'CCC'], lookback=63, skip=5)
    assert scores['AAA'] > 0
    assert scores['CCC'] < 0
    assert abs(scores['BBB']) < 1e-9


def test_momentum_scores_skips_short_history():
    p = _ramp_prices().iloc[:30]
    scores = mb.momentum_scores(p, ['AAA'], lookback=63, skip=5)
    assert scores == {}


def test_rank_long_short_directionality():
    scores = {'AAA': 0.5, 'BBB': 0.0, 'CCC': -0.3, 'DDD': 0.4, 'EEE': -0.2}
    longs, shorts = mb.rank_long_short(scores, decile=0.4, max_each=10)
    assert 'AAA' in longs and 'DDD' in longs
    assert 'CCC' in shorts and 'EEE' in shorts
    assert 'BBB' not in longs and 'BBB' not in shorts


from strategies.confirmation import options_flow as of


def test_options_flow_long_confirmed_by_low_pcr():
    passes, score = of.confirm('LONG', {'pc_ratio': 0.5, 'skew_20d': -0.03})
    assert passes is True
    assert score > 0


def test_options_flow_long_rejected_by_high_pcr():
    passes, _ = of.confirm('LONG', {'pc_ratio': 1.4})
    assert passes is False


def test_options_flow_short_confirmed_by_high_pcr():
    passes, score = of.confirm('SHORT', {'pc_ratio': 1.3, 'skew_20d': 0.04})
    assert passes is True
    assert score > 0


def test_options_flow_missing_data_is_unconfirmed():
    assert of.confirm('LONG', None) == (False, 0.0)
    assert of.confirm('LONG', {}) == (False, 0.0)
    assert of.confirm('LONG', {'pc_ratio': None}) == (False, 0.0)


def test_options_flow_pcr_gates_even_without_skew():
    passes, score = of.confirm('LONG', {'pc_ratio': 0.6})
    assert passes is True
    assert score > 0


from strategies.confirmation import sector_map as sm


def test_sector_map_known_tickers():
    assert sm.TICKER_SECTOR['AAPL'] == 'Tech'
    assert sm.TICKER_SECTOR['JPM'] == 'Fin'
    assert sm.SECTOR_ETF['Tech'] == 'XLK'
    assert sm.SECTOR_ETF['Fin'] == 'XLF'


def test_etf_for_ticker():
    assert sm.etf_for_ticker('AAPL') == 'XLK'
    assert sm.etf_for_ticker('XOM') == 'XLE'
    assert sm.etf_for_ticker('UNKNOWN_TICKER') is None


def test_every_sector_has_an_etf():
    for sector in set(sm.TICKER_SECTOR.values()):
        assert sector in sm.SECTOR_ETF, f'sector {sector} missing ETF'


def test_constituents_reverse_lookup():
    tech = sm.constituents('Tech')
    assert 'AAPL' in tech and 'MSFT' in tech
    assert all(sm.TICKER_SECTOR[t] == 'Tech' for t in tech)


from strategies.confirmation import sector_flow as sf


def _trend_prices(direction_map):
    idx = pd.bdate_range('2024-01-01', periods=260)
    cols = {}
    for t, d in direction_map.items():
        if d == 'up':
            cols[t] = np.linspace(80, 120, 260)
        elif d == 'down':
            cols[t] = np.linspace(120, 80, 260)
        else:
            cols[t] = np.full(260, 100.0)
    return pd.DataFrame(cols, index=idx)


def test_sector_flow_long_confirmed_when_sector_and_market_up():
    p = _trend_prices({'AAPL': 'up', 'XLK': 'up', 'SPY': 'up', 'QQQ': 'up'})
    passes, score = sf.confirm('LONG', 'AAPL', p, sm, as_of=p.index[-1])
    assert passes is True
    assert score > 0


def test_sector_flow_long_rejected_when_sector_down():
    p = _trend_prices({'AAPL': 'up', 'XLK': 'down', 'SPY': 'up', 'QQQ': 'up'})
    passes, _ = sf.confirm('LONG', 'AAPL', p, sm, as_of=p.index[-1])
    assert passes is False


def test_sector_flow_short_confirmed_when_sector_and_market_down():
    p = _trend_prices({'XOM': 'down', 'XLE': 'down', 'SPY': 'down', 'QQQ': 'down'})
    passes, _ = sf.confirm('SHORT', 'XOM', p, sm, as_of=p.index[-1])
    assert passes is True


def test_sector_flow_unmapped_ticker_unconfirmed():
    p = _trend_prices({'ZZZZ': 'up', 'SPY': 'up', 'QQQ': 'up'})
    assert sf.confirm('LONG', 'ZZZZ', p, sm, as_of=p.index[-1]) == (False, 0.0)
