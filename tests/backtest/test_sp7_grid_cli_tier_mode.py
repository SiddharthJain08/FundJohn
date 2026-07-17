"""SP-7 Phase B Task 7 — grid CLI tier mode arg-wiring + trade_sha determinism."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import universe_grid_cli as cli


def test_trade_sha_deterministic_and_order_independent():
    t1 = {'ticker': 'AAPL', 'entry_date': '2024-01-03', 'direction': 'long', 'exit_date': '2024-01-10'}
    t2 = {'ticker': 'MSFT', 'entry_date': '2024-01-04', 'direction': 'short', 'exit_date': '2024-01-11'}
    assert cli.trade_sha([t1, t2]) == cli.trade_sha([t2, t1])
    assert cli.trade_sha([]) == cli.trade_sha([])
    assert cli.trade_sha([t1]) != cli.trade_sha([t2])


def test_main_rejects_mixed_modes(capsys):
    rc = cli.main_with_args(['--strategy', 'x', '--start', '2024-01-01',
                             '--end', '2024-02-01',
                             '--resolver-override', 'sp500',
                             '--membership-artifact', '/tmp/a.parquet',
                             '--tier', 'sp500'])
    assert rc == 2


def test_main_requires_tier_with_artifact():
    rc = cli.main_with_args(['--strategy', 'x', '--start', '2024-01-01',
                             '--end', '2024-02-01',
                             '--membership-artifact', '/tmp/a.parquet'])
    assert rc == 2
