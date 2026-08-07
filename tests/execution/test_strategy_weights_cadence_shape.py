"""Field-shape drift fix 2026-08-07: the regime-agnostic Sharpe override must
not drop per-regime backtest cadence.

Before the fix, `_apply_regime_agnostic_override` replaced each (sid, regime)
bt entry with `dict(overall[sid])` containing only bt_sharpe/bt_n — silently
DROPPING avg_holding_days, so daily_weight's divisor fell back to the
strategy-level cadence for every agnostic strategy (live victim: S12_insider).
The directive unifies SHARPE across regimes; it says nothing about cadence.

Pins:
  (a) a tier-populated per-regime avg_holding_days SURVIVES the override;
  (b) a regime with no prior entry gets the run-level avg_holding_days;
  (c) bt_sharpe / bt_n ARE unified from the overall run in both cases.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import execution.strategy_weights as sw  # noqa: E402


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_a, **_k):
        pass

    def __iter__(self):
        return iter(self._rows)


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self, *_a, **_k):
        return _FakeCursor(self._rows)


def test_override_preserves_per_regime_cadence(monkeypatch, tmp_path):
    # Manifest with one agnostic strategy, rooted in a tmp tree.
    (tmp_path / 'src' / 'strategies').mkdir(parents=True)
    (tmp_path / 'src' / 'strategies' / 'manifest.json').write_text(json.dumps({
        'strategies': {'S_x': {'metadata': {'regime_agnostic_sharpe': True}}}}))
    monkeypatch.setattr(sw, 'ROOT', tmp_path)

    conn = _FakeConn([{'strategy_id': 'S_x', 'total_sharpe': 1.0,
                       'total_trades': 50, 'avg_holding_days': 4.2}])
    bt = {('S_x', 'LOW_VOL'): {'bt_sharpe': 2.0, 'bt_n': 10,
                               'avg_holding_days': 7.7}}
    active = [{'strategy_id': 'S_x',
               'eligible_regimes': ['LOW_VOL', 'CRISIS']}]

    sw._apply_regime_agnostic_override(conn, bt, active)

    # (a) tier-populated per-regime cadence survives.
    assert bt[('S_x', 'LOW_VOL')]['avg_holding_days'] == 7.7
    # (b) regime with no prior entry falls back to the run-level figure.
    assert bt[('S_x', 'CRISIS')]['avg_holding_days'] == 4.2
    # (c) Sharpe and n are unified from the overall run in both regimes.
    for R in ('LOW_VOL', 'CRISIS'):
        assert bt[('S_x', R)]['bt_sharpe'] == 1.0
        assert bt[('S_x', R)]['bt_n'] == 50


def test_override_noop_without_agnostic_strategies(monkeypatch, tmp_path):
    (tmp_path / 'src' / 'strategies').mkdir(parents=True)
    (tmp_path / 'src' / 'strategies' / 'manifest.json').write_text(
        json.dumps({'strategies': {'S_y': {'metadata': {}}}}))
    monkeypatch.setattr(sw, 'ROOT', tmp_path)

    bt = {('S_y', 'LOW_VOL'): {'bt_sharpe': 2.0, 'bt_n': 10,
                               'avg_holding_days': 3.3}}
    before = {k: dict(v) for k, v in bt.items()}
    sw._apply_regime_agnostic_override(_FakeConn([]), bt,
                                       [{'strategy_id': 'S_y',
                                         'eligible_regimes': ['LOW_VOL']}])
    assert bt == before
