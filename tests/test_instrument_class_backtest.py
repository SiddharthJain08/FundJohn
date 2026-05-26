import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from backtest.unified_backtest import resolve_cost_model_bps, INSTRUMENT_COST_BPS  # noqa


def test_equity_and_etp_share_cost_model():
    assert resolve_cost_model_bps('equity') == resolve_cost_model_bps('etp')


def test_unknown_class_falls_back_to_equity_cost():
    assert resolve_cost_model_bps('weird') == resolve_cost_model_bps('equity')
