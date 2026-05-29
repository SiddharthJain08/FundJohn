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
