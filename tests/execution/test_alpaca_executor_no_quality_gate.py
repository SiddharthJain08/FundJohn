import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))


def test_executor_module_has_no_signal_quality_gate():
    import execution.alpaca_executor as ex
    assert not hasattr(ex, 'check_signal_quality')
    assert 'daily_signal_summary' not in inspect.getsource(ex)
