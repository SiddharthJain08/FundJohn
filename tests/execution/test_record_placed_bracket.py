"""W1: record_submission persists the legs actually submitted (from the
execute result), not the pre-submit per-strategy order dict."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from execution import alpaca_executor as ax


class _Cur:
    def __init__(self): self.params = None
    def execute(self, sql, params): self.params = params
    def close(self): pass


class _Conn:
    def __init__(self): self.cur = _Cur()
    def cursor(self): return self.cur
    def commit(self): pass


def _row(conn):
    # INSERT param order (record_submission): run_date, ticker, strategy_id,
    # direction, qty, entry_price, stop_price, target_price, ...
    p = conn.cur.params
    return {'entry': p[5], 'stop': p[6], 'target': p[7]}


def test_records_placed_legs_over_degenerate_order(monkeypatch):
    monkeypatch.setenv('OPENCLAW_RECORD_PLACED_BRACKET', '1')
    conn = _Conn()
    # order carries the stale per-strategy levels (WDC-style: target below entry)
    order = {'ticker': 'WDC', 'strategy_id': 'momentum_12_1', 'direction': 'long',
             'entry': 627.51, 'stop': 516.11, 't1': 604.79, 'pct_nav': 0.02}
    # result carries the legs actually submitted (post-recompute / stacked)
    result = {'status': 'submitted', 'qty': 46, 'notional': 28878.8,
              'order_id': 'oid1', 'http': None, 'reason': None,
              'entry': 627.80, 'stop': 611.89, 'target': 717.03}
    ax.record_submission(conn, '2026-06-15', order, result, 'day', 'bracket', 'coid1')
    r = _row(conn)
    assert abs(r['target'] - 717.03) < 1e-6   # placed TP, not 604.79
    assert abs(r['stop'] - 611.89) < 1e-6      # placed stop, not 516.11
    assert abs(r['entry'] - 627.80) < 1e-6


def test_falls_back_to_order_when_no_placed_legs(monkeypatch):
    # dtbp-skip path: resp has no stop/target → record the intended order levels
    monkeypatch.setenv('OPENCLAW_RECORD_PLACED_BRACKET', '1')
    conn = _Conn()
    order = {'ticker': 'AAPL', 'strategy_id': 's', 'direction': 'long',
             'entry': 100.0, 'stop': 95.0, 't1': 110.0, 'pct_nav': 0.01}
    resp = {'status': 'skipped_dtbp', 'qty': 0, 'order_id': None, 'entry': 100.0}
    ax.record_submission(conn, '2026-06-15', order, resp, 'day', 'simple', 'coid2')
    r = _row(conn)
    assert abs(r['stop'] - 95.0) < 1e-6
    assert abs(r['target'] - 110.0) < 1e-6


def test_gate_off_is_legacy_behavior(monkeypatch):
    monkeypatch.delenv('OPENCLAW_RECORD_PLACED_BRACKET', raising=False)
    conn = _Conn()
    order = {'ticker': 'WDC', 'strategy_id': 's', 'direction': 'long',
             'entry': 627.51, 'stop': 516.11, 't1': 604.79, 'pct_nav': 0.02}
    result = {'status': 'submitted', 'qty': 46, 'order_id': 'oid1',
              'entry': 627.80, 'stop': 611.89, 'target': 717.03}
    ax.record_submission(conn, '2026-06-15', order, result, 'day', 'bracket', 'coid1')
    r = _row(conn)
    assert abs(r['target'] - 604.79) < 1e-6   # legacy: reads order['t1']
    assert abs(r['stop'] - 516.11) < 1e-6


def test_degenerate_recorded_target_is_logged_not_silent(monkeypatch):
    """The W1 safety guarantee: a recorded target on the wrong side of entry
    (here a long whose placed TP is BELOW entry) emits a visible WARN — it is
    never recorded silently."""
    monkeypatch.setenv('OPENCLAW_RECORD_PLACED_BRACKET', '1')
    logs = []
    monkeypatch.setattr(ax, 'log', lambda m: logs.append(m))
    conn = _Conn()
    order = {'ticker': 'BADTP', 'strategy_id': 's', 'direction': 'long',
             'entry': 100.0, 'stop': 90.0, 't1': 110.0, 'pct_nav': 0.01}
    # placed result has a degenerate long TP (96 < entry 100)
    result = {'status': 'submitted', 'qty': 10, 'order_id': 'oid1',
              'entry': 100.0, 'stop': 90.0, 'target': 96.0}
    ax.record_submission(conn, '2026-06-15', order, result, 'day', 'bracket', 'coid1')
    assert any('DEGENERATE' in m for m in logs), logs
    assert abs(_row(conn)['target'] - 96.0) < 1e-6   # still recorded (placed value)


def test_healthy_target_emits_no_degenerate_warn(monkeypatch):
    monkeypatch.setenv('OPENCLAW_RECORD_PLACED_BRACKET', '1')
    logs = []
    monkeypatch.setattr(ax, 'log', lambda m: logs.append(m))
    conn = _Conn()
    order = {'ticker': 'OK', 'strategy_id': 's', 'direction': 'long',
             'entry': 100.0, 'stop': 90.0, 't1': 110.0, 'pct_nav': 0.01}
    result = {'status': 'submitted', 'qty': 10, 'order_id': 'oid1',
              'entry': 100.0, 'stop': 90.0, 'target': 110.0}
    ax.record_submission(conn, '2026-06-15', order, result, 'day', 'bracket', 'coid1')
    assert not any('DEGENERATE' in m for m in logs), logs
