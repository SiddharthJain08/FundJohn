"""B1 (spec item 3): a broker stop fill must close the ledger, not just post.

engine.update_pnl only infers close_reason='stop_loss' at EOD from a parquet
close crossing the stop, so an intraday broker stop fill left signal_pnl 'open'
and the stop-out cooldown (_load_recent_stopouts) never saw it — the same name
was re-bought on the next cycle. run_exit_fill_reporter now closes every HELD
ledger row on that ticker/side via drop_signal_close(reason='stop_loss').
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import afterhours_tp as ah  # noqa: E402
from execution import open_reconcile as orc  # noqa: E402

_SIGNAL_ROW = ('sig-1', 'S_x', 'ws-1', date(2026, 9, 10), 'LONG',
               100.0, 100.0, date(2026, 9, 11))


class _Cursor:
    """Records every execute; replays one SELECT row for drop_signal_close."""
    def __init__(self, row=_SIGNAL_ROW, held=()):
        self.row = row
        self.held = list(held)
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((' '.join(sql.split()), params))

    def fetchone(self):
        return self.row

    def fetchall(self):
        return []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def inserts(self):
        return [c for c in self.calls if c[0].startswith('INSERT INTO signal_pnl')]


class _Conn:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.commits += 1
        return False


# ── drop_signal_close(closed_at=…) ──────────────────────────────────────────

def _closed_at_param(cur):
    """signal_pnl INSERT params: index 3 = pnl_date, index 9 = closed_at."""
    return cur.inserts()[0][1][9]


def _pnl_date_param(cur):
    return cur.inserts()[0][1][3]


def test_closed_at_defaults_to_today():
    cur = _Cursor()
    orc.drop_signal_close(cur, 'sig-1', 'AAA', 10.0, reason='stop_loss')
    assert _closed_at_param(cur) == date.today()


def test_closed_at_accepts_broker_iso_string_with_z():
    cur = _Cursor()
    orc.drop_signal_close(cur, 'sig-1', 'AAA', 10.0, reason='stop_loss',
                          closed_at='2026-09-11T19:31:02.412Z')
    assert _closed_at_param(cur) == date(2026, 9, 11)


def test_closed_at_accepts_a_datetime():
    cur = _Cursor()
    orc.drop_signal_close(cur, 'sig-1', 'AAA', 10.0, reason='stop_loss',
                          closed_at=datetime(2026, 9, 11, 19, 31, tzinfo=timezone.utc))
    assert _closed_at_param(cur) == date(2026, 9, 11)


def test_closed_at_garbage_falls_back_to_today():
    cur = _Cursor()
    orc.drop_signal_close(cur, 'sig-1', 'AAA', 10.0, reason='stop_loss',
                          closed_at='not-a-timestamp')
    assert _closed_at_param(cur) == date.today()


def test_pnl_date_stays_today_so_the_conflict_key_is_unchanged():
    cur = _Cursor()
    orc.drop_signal_close(cur, 'sig-1', 'AAA', 10.0, reason='stop_loss',
                          closed_at='2026-01-02T10:00:00Z')
    assert _pnl_date_param(cur) == date.today()


# ── _close_signals_for_fill ─────────────────────────────────────────────────

def _fill(kind='stop', side='sell', symbol='AAA', price=9.5, oid='f1'):
    return {'id': oid, 'symbol': symbol, 'side': side, 'qty': 10.0,
            'price': price, 'level': 10.0, 'kind': kind,
            'filled_at': '2026-09-11T19:31:02Z'}


def test_stop_fill_closes_matching_open_signals(monkeypatch):
    cur = _Cursor()
    monkeypatch.setattr(orc, '_held_signal_rows',
                        lambda c, t: [('sig-1', 'LONG'), ('sig-2', 'LONG')])
    n = ah._close_signals_for_fill(_fill(), conn_factory=lambda: _Conn(cur))
    assert n == 2
    reasons = [c[1][10] for c in cur.inserts()]
    prices = [c[1][4] for c in cur.inserts()]
    assert reasons == ['stop_loss', 'stop_loss']
    assert prices == [9.5, 9.5]


def test_sell_exit_does_not_close_short_rows(monkeypatch):
    cur = _Cursor()
    monkeypatch.setattr(orc, '_held_signal_rows', lambda c, t: [('sig-9', 'SHORT')])
    assert ah._close_signals_for_fill(_fill(), conn_factory=lambda: _Conn(cur)) == 0
    assert cur.inserts() == []


def test_buy_exit_closes_short_rows(monkeypatch):
    cur = _Cursor()
    monkeypatch.setattr(orc, '_held_signal_rows', lambda c, t: [('sig-9', 'SHORT')])
    assert ah._close_signals_for_fill(_fill(side='buy'),
                                      conn_factory=lambda: _Conn(cur)) == 1


def test_fill_on_ticker_with_no_open_signal_only_logs(monkeypatch):
    cur = _Cursor()
    monkeypatch.setattr(orc, '_held_signal_rows', lambda c, t: [])
    assert ah._close_signals_for_fill(_fill(), conn_factory=lambda: _Conn(cur)) == 0
    assert cur.inserts() == []


def test_db_failure_returns_zero_and_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError('postgres down')
    assert ah._close_signals_for_fill(_fill(), conn_factory=_boom) == 0


# ── run_exit_fill_reporter wiring ───────────────────────────────────────────

def _wire(monkeypatch, tmp_path, orders, closed):
    monkeypatch.setenv('OPENCLAW_EXIT_FILLS_STATE', str(tmp_path / 'seen.json'))
    import execution.stop_reattach as sr
    monkeypatch.setattr(sr, 'fetch_recent_closed_orders',
                        lambda *a, **k: (True, orders))
    monkeypatch.setattr(sr, '_post_alert', lambda msg, channel=None: None)
    monkeypatch.setattr(ah, '_open_signal_tickers', lambda **k: ['AAA'])
    monkeypatch.setattr(ah, '_close_signals_for_fill',
                        lambda f, **k: closed.append(f) or 1)


_STOP_ORDER = [{'id': 'o1', 'symbol': 'AAA', 'side': 'sell', 'status': 'filled',
                'type': 'stop', 'stop_price': '10.00', 'filled_qty': '10',
                'filled_avg_price': '9.50', 'filled_at': '2026-09-11T19:31:02Z'}]


def test_first_run_seeds_without_closing_anything(monkeypatch, tmp_path):
    closed = []
    _wire(monkeypatch, tmp_path, _STOP_ORDER, closed)
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['fills_seen'] == 1
    assert stats['reported'] == 0
    assert stats['signals_closed'] == 0
    assert closed == []


def test_second_run_closes_then_third_run_is_a_noop(monkeypatch, tmp_path):
    closed = []
    _wire(monkeypatch, tmp_path, _STOP_ORDER, closed)
    ah.run_exit_fill_reporter(dry_run=False)          # seed
    (tmp_path / 'seen.json').write_text(json.dumps({'seen': []}))  # state exists, empty
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['signals_closed'] == 1 and len(closed) == 1
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['signals_closed'] == 0 and len(closed) == 1


def test_take_profit_fill_is_reported_but_never_closed_as_stop_loss(monkeypatch, tmp_path):
    closed = []
    tp = [{'id': 'o2', 'symbol': 'AAA', 'side': 'sell', 'status': 'filled',
           'type': 'limit', 'order_class': 'oco', 'limit_price': '12.00',
           'filled_qty': '10', 'filled_avg_price': '12.05',
           'filled_at': '2026-09-11T19:31:02Z'}]
    _wire(monkeypatch, tmp_path, tp, closed)
    (tmp_path / 'seen.json').write_text(json.dumps({'seen': []}))
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['reported'] == 1 and stats['signals_closed'] == 0 and closed == []


def test_reporter_scopes_the_fetch_to_open_signal_tickers(monkeypatch, tmp_path):
    seen_args = {}
    monkeypatch.setenv('OPENCLAW_EXIT_FILLS_STATE', str(tmp_path / 'seen.json'))
    import execution.stop_reattach as sr

    def _fetch(symbols=None, **k):
        seen_args['symbols'] = symbols
        return True, []
    monkeypatch.setattr(sr, 'fetch_recent_closed_orders', _fetch)
    monkeypatch.setattr(sr, '_post_alert', lambda msg, channel=None: None)
    monkeypatch.setattr(ah, '_open_signal_tickers', lambda **k: ['AAA', 'BBB'])
    ah.run_exit_fill_reporter(dry_run=False)
    assert seen_args['symbols'] == ['AAA', 'BBB']
