import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from strategies.base import BaseStrategy  # noqa: E402


def test_default_false():
    assert BaseStrategy.benchmark_sleeve is False


def test_subclass_can_opt_in():
    class _B(BaseStrategy):
        id = 'ZZT_bench'; name = 'x'
        benchmark_sleeve = True
        def generate_signals(self, prices, regime, universe, aux_data=None):
            return []
    assert _B.benchmark_sleeve is True
