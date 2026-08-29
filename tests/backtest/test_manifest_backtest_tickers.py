"""Amendment 1 D-C2: manifest metadata.backtest_tickers -> load_prices_panels(tickers=)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub  # noqa: E402


def _manifest(tmp_path, entry):
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps({'strategies': {'S_x': entry}}))
    return p


def test_reads_sorted_unique_symbols(tmp_path):
    p = _manifest(tmp_path, {'state': 'live', 'metadata': {'backtest_tickers': ['SPY', 'QQQ', 'SPY', '']}})
    assert ub._manifest_backtest_tickers('S_x', manifest_path=p) == ['QQQ', 'SPY']


def test_absent_or_invalid_is_none(tmp_path):
    assert ub._manifest_backtest_tickers('S_x', manifest_path=_manifest(tmp_path, {'state': 'live'})) is None
    assert ub._manifest_backtest_tickers('S_x', manifest_path=_manifest(tmp_path, {'metadata': {'backtest_tickers': []}})) is None
    assert ub._manifest_backtest_tickers('S_x', manifest_path=_manifest(tmp_path, {'metadata': {'backtest_tickers': 'SPY'}})) is None
    assert ub._manifest_backtest_tickers('S_other', manifest_path=_manifest(tmp_path, {'state': 'live'})) is None
    assert ub._manifest_backtest_tickers('S_x', manifest_path=tmp_path / 'missing.json') is None
