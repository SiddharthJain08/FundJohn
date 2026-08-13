"""Regime-flip cadence reset (operator directive 2026-08-13).

On the day the regime-of-record changes, every eligible rebalance-cadence
strategy must emit its book same-day instead of waiting for its calendar
boundary, and the sizer's active-window loaders must never feed a signal
minted under another regime into the new regime's book.

Pins the four pieces:
  1. StrategyBase.cadence_reset — the flag contract.
  2. A representative monthly strategy (TSMOM) bypasses its month-boundary
     gate when the engine stamps regime['cadence_reset'].
  3. engine._stamp_cadence_reset_on_flip — flip detection semantics,
     kill switch, manual force, fail-quiet on DB error.
  4. Both sizer signal loaders scope their SQL to the current regime.
"""
import sys
import math  # noqa: F401
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from strategies.base import BaseStrategy  # noqa: E402


# ── 1. flag contract ─────────────────────────────────────────────────────────

class _Dummy(BaseStrategy):
    id = 'dummy'
    name = 'dummy'
    description = 'dummy'
    active_in_regimes = ['LOW_VOL']

    def generate_signals(self, prices, regime, universe, aux_data=None):
        return []


def test_cadence_reset_flag_contract():
    s = _Dummy()
    assert s.cadence_reset({'state': 'LOW_VOL', 'cadence_reset': True}) is True
    assert s.cadence_reset({'state': 'LOW_VOL'}) is False
    assert s.cadence_reset({}) is False
    assert s.cadence_reset(None) is False
    # engine passes plain strings in some legacy paths — must not blow up
    assert s.cadence_reset('LOW_VOL') is False


# ── 2. monthly strategy bypasses its boundary gate on flip day ───────────────

def _tsmom_prices(end: str) -> pd.DataFrame:
    """Wide close panel: 320 business days ending at `end`, two BASKET
    tickers trending up (positive 12M return) with mild wobble (vol>0)."""
    idx = pd.bdate_range(end=end, periods=320)
    base = np.linspace(100.0, 160.0, len(idx))
    wob = 1.0 + 0.01 * np.sin(np.arange(len(idx)))
    return pd.DataFrame({'GLD': base * wob, 'SPY': (base + 5) * wob}, index=idx)


def _tsmom():
    from strategies.implementations.S_ast_time_series_momentum_effect import (
        AstTimeSeriesMomentumEffect)
    return AstTimeSeriesMomentumEffect()


def test_monthly_gate_blocks_mid_month_without_flag():
    # 2026-08-12 (Wed): index[-2] is 08-11 — same month, gate closed.
    prices = _tsmom_prices('2026-08-12')
    out = _tsmom().generate_signals(prices, {'state': 'TRANSITIONING'},
                                    ['GLD', 'SPY'], {})
    assert out == []


def test_monthly_gate_bypassed_on_flip_day():
    prices = _tsmom_prices('2026-08-12')
    out = _tsmom().generate_signals(
        prices, {'state': 'TRANSITIONING', 'cadence_reset': True},
        ['GLD', 'SPY'], {})
    assert out, 'flip-day cadence reset must open the monthly gate'
    assert all(s.direction == 'LONG' for s in out)


def test_monthly_gate_still_fires_on_boundary_without_flag():
    # 2026-09-01 (Tue): index[-2] is 08-31 — month boundary, gate open as before.
    prices = _tsmom_prices('2026-09-01')
    out = _tsmom().generate_signals(prices, {'state': 'TRANSITIONING'},
                                    ['GLD', 'SPY'], {})
    assert out, 'normal boundary behavior must be unchanged'


# ── 3. engine flip detection ─────────────────────────────────────────────────

class _FakeCur:
    def __init__(self, row=('LOW_VOL',), raise_on_execute=False):
        self.row = row
        self.raise_on_execute = raise_on_execute
        self.executed = []

    def execute(self, sql, params=None):
        if self.raise_on_execute:
            raise RuntimeError('db down')
        self.executed.append(sql)

    def fetchone(self):
        return self.row


@pytest.fixture
def _flip_env(monkeypatch):
    monkeypatch.delenv('OPENCLAW_CADENCE_RESET_ON_FLIP', raising=False)
    monkeypatch.delenv('OPENCLAW_FORCE_CADENCE_RESET', raising=False)


def _stamp(cur, regime):
    from execution.engine import _stamp_cadence_reset_on_flip
    _stamp_cadence_reset_on_flip(cur, regime)
    return regime


def test_flip_detected_stamps_reset(_flip_env):
    r = _stamp(_FakeCur(('LOW_VOL',)), {'state': 'TRANSITIONING'})
    assert r.get('cadence_reset') is True


def test_same_regime_no_stamp(_flip_env):
    r = _stamp(_FakeCur(('TRANSITIONING',)), {'state': 'TRANSITIONING'})
    assert 'cadence_reset' not in r


def test_no_prior_signals_counts_as_flip(_flip_env):
    r = _stamp(_FakeCur(None), {'state': 'LOW_VOL'})
    assert r.get('cadence_reset') is True


def test_db_error_fails_quiet(_flip_env):
    r = _stamp(_FakeCur(raise_on_execute=True), {'state': 'TRANSITIONING'})
    assert 'cadence_reset' not in r


def test_kill_switch(monkeypatch, _flip_env):
    monkeypatch.setenv('OPENCLAW_CADENCE_RESET_ON_FLIP', '0')
    cur = _FakeCur(('LOW_VOL',))
    r = _stamp(cur, {'state': 'TRANSITIONING'})
    assert 'cadence_reset' not in r and cur.executed == []


def test_manual_force(monkeypatch, _flip_env):
    monkeypatch.setenv('OPENCLAW_FORCE_CADENCE_RESET', '1')
    cur = _FakeCur(('TRANSITIONING',))
    r = _stamp(cur, {'state': 'TRANSITIONING'})
    assert r.get('cadence_reset') is True and cur.executed == []


# ── 4. sizer loaders are regime-scoped ───────────────────────────────────────

class _RecCursor:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchall(self):
        return []


class _RecConn:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, **kw):
        return _RecCursor(self.log)


def _capture_queries(monkeypatch):
    import psycopg2
    log = []
    monkeypatch.setattr(psycopg2, 'connect', lambda *a, **k: _RecConn(log))
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://stub/stub')
    return log


def test_active_window_loader_scopes_to_regime(monkeypatch):
    import execution.regime_blended_sizer as _sizer
    log = _capture_queries(monkeypatch)
    _sizer._load_active_window_signals('TRANSITIONING', {'S1': 1.0}, {'S1': 5.0})
    sql, params = log[0]
    assert 'regime_state = %s' in sql
    assert 'TRANSITIONING' in params


def test_approved_carried_loader_scopes_to_regime(monkeypatch):
    import execution.regime_blended_sizer as _sizer
    log = _capture_queries(monkeypatch)
    _sizer._load_approved_carried_signals({'S1': 1.0}, {'S1': 5.0},
                                          regime_state='TRANSITIONING')
    sql, params = log[0]
    assert 'regime_state = %s' in sql
    assert 'TRANSITIONING' in params


def test_approved_carried_loader_unscoped_without_regime(monkeypatch):
    # regime_state=None keeps the historical unscoped query (defensive path).
    import execution.regime_blended_sizer as _sizer
    log = _capture_queries(monkeypatch)
    _sizer._load_approved_carried_signals({'S1': 1.0}, {'S1': 5.0},
                                          regime_state=None)
    sql, params = log[0]
    assert 'regime_state = %s' not in sql
