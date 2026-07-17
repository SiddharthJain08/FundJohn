import dataclasses
from strategies.base import OptionSpec


def test_asdict_from_dict_roundtrip():
    spec = OptionSpec(underlying='SPY', structure='straddle', hedge='delta',
                      strike_rule='atm', dte_target=30, roll_dte=7)
    d = dataclasses.asdict(spec)
    assert d['structure'] == 'straddle' and d['hedge'] == 'delta'
    rebuilt = OptionSpec.from_dict(d)
    assert rebuilt == spec


def test_from_dict_ignores_unknown_keys():
    spec = OptionSpec.from_dict({'underlying': 'SPY', 'structure': 'strangle',
                                 'bogus_key': 123})
    assert spec.underlying == 'SPY' and spec.structure == 'strangle'


def test_from_dict_none_and_malformed_failsclosed():
    assert OptionSpec.from_dict(None) is None
    assert OptionSpec.from_dict({}) is None           # no underlying -> can't build
    assert OptionSpec.from_dict('not a dict') is None
