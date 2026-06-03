from strategies.base import OptionSpec


def test_vertical_structure_and_width_default():
    spec = OptionSpec(underlying='SPY', structure='vertical', right='call')
    assert spec.structure == 'vertical'
    assert spec.spread_width_pct == 0.03   # default


def test_vertical_width_override():
    spec = OptionSpec(underlying='SPY', structure='vertical', right='put', spread_width_pct=0.05)
    assert spec.spread_width_pct == 0.05


def test_existing_specs_unchanged():
    s = OptionSpec(underlying='SPY', structure='straddle')
    assert s.structure == 'straddle' and s.spread_width_pct == 0.03  # field present, default
