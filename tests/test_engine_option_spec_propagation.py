"""
TDD tests for SP-5.1c Task 2: write_signals serializes option_spec into signal_params.

Invariant: equity signals (option_spec=None) produce NO 'option_spec' key in params
(byte-identical with pre-SP-5.1c behaviour). Option signals get a 'option_spec' dict.
"""
from strategies.base import Signal, OptionSpec
import execution.engine as eng


def _make_option_signal():
    spec = OptionSpec(underlying='SPY', structure='straddle', hedge='delta', strike_rule='atm')
    return Signal(
        ticker='SPY',
        direction='BUY_VOL',
        entry_price=500.0,
        stop_loss=0.0,
        target_1=0.0,
        target_2=0.0,
        target_3=0.0,
        position_size_pct=0.0,
        confidence='HIGH',
        signal_params={'foo': 1},
        option_spec=spec,
    )


def _make_equity_signal():
    return Signal(
        ticker='AAPL',
        direction='LONG',
        entry_price=100.0,
        stop_loss=95.0,
        target_1=110.0,
        target_2=120.0,
        target_3=130.0,
        position_size_pct=0.05,
        confidence='HIGH',
        signal_params={'foo': 1},
    )


def test_option_spec_serialized_into_params():
    sig = _make_option_signal()
    params = eng._params_with_option_spec(sig)
    assert params['foo'] == 1
    assert params['option_spec']['structure'] == 'straddle'
    assert params['option_spec']['hedge'] == 'delta'
    assert params['option_spec']['underlying'] == 'SPY'
    assert params['option_spec']['strike_rule'] == 'atm'


def test_equity_signal_has_no_option_spec_key():
    sig = _make_equity_signal()
    params = eng._params_with_option_spec(sig)
    assert params['foo'] == 1
    assert 'option_spec' not in params   # byte-identical for equity


def test_features_still_folded_correctly():
    """Regression: features key must still appear under the 'features' subkey."""
    sig = _make_equity_signal()
    sig.features = {'hv30': 0.25, 'beta': 1.1}
    params = eng._params_with_option_spec(sig)
    assert params['features']['hv30'] == 0.25
    assert params['features']['beta'] == 1.1
    assert 'option_spec' not in params


def test_option_spec_with_features():
    """Option signal with both features and option_spec — both must be present."""
    sig = _make_option_signal()
    sig.features = {'iv30': 0.20}
    params = eng._params_with_option_spec(sig)
    assert 'option_spec' in params
    assert 'features' in params
    assert params['features']['iv30'] == 0.20
    assert params['option_spec']['structure'] == 'straddle'
