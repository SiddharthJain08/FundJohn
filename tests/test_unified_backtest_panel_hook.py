import sys, inspect
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from backtest import unified_backtest

def test_run_backtest_invokes_panel_rebuild():
    src = inspect.getsource(unified_backtest.run_backtest)
    assert 'backtest_panel' in src and 'rebuild' in src, \
        "run_backtest must refresh the dashboard panel after persisting a run"
