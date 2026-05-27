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
