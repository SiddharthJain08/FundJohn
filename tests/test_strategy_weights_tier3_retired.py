"""Pin the Option B follow-up (2026-07-12): the former Tier-3 fallback in
strategy_weights._load_backtest_sharpe (strategy_registry.backtest_sharpe ×
eligible regimes) is RETIRED. Strategies with no rows in Tier 1 (canonical
strategy_backtest_regimes) or Tier 2 (legacy strategy_regime_backtests) must
get NO bt entry — and the retired registry mirror must never be queried.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from execution.strategy_weights import _load_backtest_sharpe  # noqa: E402


class _StubCursor:
    """Records every executed SQL string; returns no rows anywhere."""

    def __init__(self, log):
        self._log = log

    def execute(self, sql, params=None):
        self._log.append(sql)

    def __iter__(self):
        return iter(())

    def fetchone(self):
        return None  # pipeline_config cap read -> fail-safe default

    def fetchall(self):
        return []


class _StubConn:
    def __init__(self):
        self.executed = []

    def cursor(self, cursor_factory=None):
        return _StubCursor(self.executed)

    def rollback(self):
        pass


def test_missing_strategies_get_no_bt_entry_and_registry_is_never_queried():
    conn = _StubConn()
    out = _load_backtest_sharpe(conn, ['S_ghost_no_backtest', 'S_other_ghost'])
    # No tier served them -> no fabricated entries.
    assert out == {}
    # Tier 1 + Tier 2 (+ the cap read) ran; the retired mirror did not.
    joined = '\n'.join(conn.executed)
    assert 'strategy_backtest_regimes' in joined
    assert 'strategy_regime_backtests' in joined
    assert 'strategy_registry' not in joined
    assert 'backtest_sharpe' not in joined  # the mirror column, gone entirely


def test_empty_input_short_circuits_without_queries():
    conn = _StubConn()
    assert _load_backtest_sharpe(conn, []) == {}
    assert conn.executed == []
