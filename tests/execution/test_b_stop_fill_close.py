"""B1 (spec item 3): a broker stop fill must close the ledger, not just post.

engine.update_pnl only infers close_reason='stop_loss' at EOD from a parquet
close crossing the stop, so an intraday broker stop fill left signal_pnl 'open'
and the stop-out cooldown (_load_recent_stopouts) never saw it — the same name
was re-bought on the next cycle. run_exit_fill_reporter now closes every HELD
ledger row on that ticker/side via drop_signal_close(reason='stop_loss').

Fix round 1 (review) adds two more layers on top of the original wiring:
  - a recency gate — `now - 48h` ONLY (see "recency gate" tests below) — so a
    chronologically-ancient fill surfacing for the first time via the Task-1
    symbol-scoped read is never posted/closed against today's fresh position.
    Round 1 first tried folding a persisted `watermark` into the cutoff
    (`max(watermark, now-48h)`); fix round 2 reverted that after the
    coordinator confirmed it was a defect — the closed-order window can
    surface an OLDER fill after a NEWER one already advanced the watermark,
    and folding the watermark into the cutoff would wrongly gate that
    still-legitimate fill out forever. `watermark` is kept in the state JSON
    purely as an informational "newest filled_at processed", never as a gate.
  - a `pending` retry list so a fill whose close FAILS (DB blip) is retried on
    a later tick instead of being silently dropped — see the "pending retry"
    tests below.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
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


# ── timestamp helpers (recency gate is wall-clock relative) ────────────────

def _iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def _recent(minutes=60):
    """A filled_at well inside the recency window (default 48h)."""
    return _iso(datetime.now(timezone.utc) - timedelta(minutes=minutes))


def _stale(hours=72):
    """A filled_at past the recency window (default 48h)."""
    return _iso(datetime.now(timezone.utc) - timedelta(hours=hours))


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
            'filled_at': _recent()}


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


def test_ah_exit_fill_closes_matching_open_signals(monkeypatch):
    """Missing test case named by the review: ah_exit must close end-to-end
    like a stop, at the unit level (see test_ah_exit_fill_closes_end_to_end_
    like_a_stop below for the full run_exit_fill_reporter wiring)."""
    cur = _Cursor()
    monkeypatch.setattr(orc, '_held_signal_rows', lambda c, t: [('sig-1', 'LONG')])
    n = ah._close_signals_for_fill(_fill(kind='ah_exit'), conn_factory=lambda: _Conn(cur))
    assert n == 1
    assert cur.inserts()[0][1][10] == 'stop_loss'


def test_db_failure_returns_negative_one_and_never_raises(monkeypatch):
    """DB-connect failure is a FAILURE, not "nothing to close" — it must be
    distinguishable from the legitimate 0-rows-matched outcome so the caller
    can retry it (review fix round 1, finding 2)."""
    def _boom():
        raise RuntimeError('postgres down')
    assert ah._close_signals_for_fill(_fill(), conn_factory=_boom) == -1


def test_query_failure_returns_negative_one_and_never_raises(monkeypatch):
    monkeypatch.setattr(orc, '_held_signal_rows',
                        lambda c, t: (_ for _ in ()).throw(RuntimeError('boom')))
    assert ah._close_signals_for_fill(_fill(), conn_factory=lambda: _Conn(_Cursor())) == -1


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


def _state_path(tmp_path):
    return tmp_path / 'seen.json'


def _seed_state(tmp_path, **fields):
    """Write a pre-existing (non-first-run) state file."""
    body = {'seen': []}
    body.update(fields)
    _state_path(tmp_path).write_text(json.dumps(body))


def _stop_order(filled_at=None, oid='o1', symbol='AAA'):
    return [{'id': oid, 'symbol': symbol, 'side': 'sell', 'status': 'filled',
             'type': 'stop', 'stop_price': '10.00', 'filled_qty': '10',
             'filled_avg_price': '9.50', 'filled_at': filled_at or _recent()}]


def _ah_exit_order(filled_at=None, oid='o3', symbol='AAA'):
    return [{'id': oid, 'symbol': symbol, 'side': 'sell', 'status': 'filled',
             'type': 'limit', 'client_order_id': f'ahsx_{symbol}_1',
             'limit_price': '9.00', 'filled_qty': '10',
             'filled_avg_price': '9.00', 'filled_at': filled_at or _recent()}]


def test_first_run_seeds_without_closing_anything(monkeypatch, tmp_path):
    closed = []
    _wire(monkeypatch, tmp_path, _stop_order(), closed)
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['fills_seen'] == 1
    assert stats['reported'] == 0
    assert stats['signals_closed'] == 0
    assert closed == []


def test_second_run_closes_then_third_run_is_a_noop(monkeypatch, tmp_path):
    closed = []
    order = _stop_order()
    _wire(monkeypatch, tmp_path, order, closed)
    ah.run_exit_fill_reporter(dry_run=False)          # seed
    _seed_state(tmp_path)                             # state exists, empty seen
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['signals_closed'] == 1 and len(closed) == 1
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['signals_closed'] == 0 and len(closed) == 1


def test_take_profit_fill_is_reported_but_never_closed_as_stop_loss(monkeypatch, tmp_path):
    closed = []
    tp = [{'id': 'o2', 'symbol': 'AAA', 'side': 'sell', 'status': 'filled',
           'type': 'limit', 'order_class': 'oco', 'limit_price': '12.00',
           'filled_qty': '10', 'filled_avg_price': '12.05',
           'filled_at': _recent()}]
    _wire(monkeypatch, tmp_path, tp, closed)
    _seed_state(tmp_path)
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


def test_fetch_failure_leaves_ledger_untouched(monkeypatch, tmp_path):
    """Missing test case named by the review: ok=False must not touch the
    ledger — nor even write a state file (the early return happens before any
    state is read or written)."""
    state_p = _state_path(tmp_path)
    monkeypatch.setenv('OPENCLAW_EXIT_FILLS_STATE', str(state_p))
    import execution.stop_reattach as sr
    monkeypatch.setattr(sr, 'fetch_recent_closed_orders', lambda *a, **k: (False, []))
    posts = []
    monkeypatch.setattr(sr, '_post_alert', lambda msg, channel=None: posts.append(msg))
    closed = []
    monkeypatch.setattr(ah, '_open_signal_tickers', lambda **k: ['AAA'])
    monkeypatch.setattr(ah, '_close_signals_for_fill', lambda f, **k: closed.append(f) or 1)
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats == {'fills_seen': 0, 'reported': 0, 'signals_closed': 0}
    assert closed == [] and posts == []
    assert not state_p.exists()


def test_ah_exit_fill_closes_end_to_end_like_a_stop(monkeypatch, tmp_path):
    """Missing test case named by the review, at the run_exit_fill_reporter
    wiring level: an ah_exit fill routes through _CLOSING_FILL_KINDS exactly
    like a stop fill."""
    closed = []
    _wire(monkeypatch, tmp_path, _ah_exit_order(), closed)
    _seed_state(tmp_path)
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['reported'] == 1
    assert stats['signals_closed'] == 1
    assert len(closed) == 1 and closed[0]['kind'] == 'ah_exit'


# ── recency gate (review fix round 1, finding 1) ────────────────────────────

def test_stale_fill_is_skipped_but_marked_seen(monkeypatch, tmp_path):
    closed = []
    order = _stop_order(filled_at=_stale(hours=72), oid='old1')
    _wire(monkeypatch, tmp_path, order, closed)
    _seed_state(tmp_path)             # existing (non-first-run) state
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['fills_seen'] == 1
    assert stats['reported'] == 0
    assert stats['signals_closed'] == 0
    assert closed == []
    st = json.loads(_state_path(tmp_path).read_text())
    assert 'old1' in st['seen']       # never resurfaces on a later run


def test_recent_fill_is_posted_and_closed_and_watermark_advances(monkeypatch, tmp_path):
    """Since fix round 2 the watermark is informational only — it does not
    gate anything (see test_fill_older_than_watermark_but_within_48h_is_
    posted_and_closed below). This test pins that the informational contract
    still holds: a processed fill's filled_at still gets recorded."""
    closed = []
    fill_iso = _recent(minutes=30)
    order = _stop_order(filled_at=fill_iso, oid='new1')
    _wire(monkeypatch, tmp_path, order, closed)
    _seed_state(tmp_path)
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['reported'] == 1
    assert stats['signals_closed'] == 1
    assert len(closed) == 1
    st = json.loads(_state_path(tmp_path).read_text())
    assert st.get('watermark')
    wm = datetime.fromisoformat(st['watermark'].replace('Z', '+00:00'))
    fill_dt = datetime.fromisoformat(fill_iso.replace('Z', '+00:00'))
    assert wm >= fill_dt


def test_fill_older_than_watermark_but_within_48h_is_posted_and_closed(monkeypatch, tmp_path):
    """Ruling revised in fix round 2: the watermark must NOT participate in
    the recency cutoff — only `now - 48h` gates. The closed-order window can
    surface an OLDER fill after a NEWER one already advanced the watermark
    (e.g. a 10:00 fill missed by the 10:10 read but present at 10:20, after a
    10:15 fill advanced the watermark) — that fill must still be posted and
    closed. Here the persisted watermark (1h ago) is newer than this fill
    (20h ago), but 20h is still well within the 48h floor."""
    closed = []
    watermark = _iso(datetime.now(timezone.utc) - timedelta(hours=1))   # newer than the fill
    fill_iso = _stale(hours=20)                                          # older than watermark, inside 48h
    order = _stop_order(filled_at=fill_iso, oid='older_than_watermark')
    _wire(monkeypatch, tmp_path, order, closed)
    _seed_state(tmp_path, watermark=watermark)
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['reported'] == 1
    assert stats['signals_closed'] == 1
    assert len(closed) == 1


# ── pending retry (review fix round 1, finding 2) ───────────────────────────

def test_close_failure_marks_pending_then_retries_and_clears_on_next_healthy_run(
        monkeypatch, tmp_path):
    closed = []
    order = _stop_order(filled_at=_recent(), oid='fail1')
    state_p = _state_path(tmp_path)
    monkeypatch.setenv('OPENCLAW_EXIT_FILLS_STATE', str(state_p))
    import execution.stop_reattach as sr
    monkeypatch.setattr(sr, 'fetch_recent_closed_orders', lambda *a, **k: (True, order))
    posts = []
    monkeypatch.setattr(sr, '_post_alert', lambda msg, channel=None: posts.append(msg))
    monkeypatch.setattr(ah, '_open_signal_tickers', lambda **k: ['AAA'])
    _seed_state(tmp_path)

    # First run: close FAILS (simulated DB blip) — must be recorded pending,
    # not silently dropped, and the fill still gets reported once.
    monkeypatch.setattr(ah, '_close_signals_for_fill', lambda f, **k: -1)
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['reported'] == 1
    assert stats['signals_closed'] == 0
    assert len(posts) == 1
    st = json.loads(state_p.read_text())
    assert st['pending'] == ['fail1']

    # Second run: DB healthy again, same fill still in the closed-orders
    # window (broker history doesn't move) — retried silently, no re-post.
    monkeypatch.setattr(ah, '_close_signals_for_fill',
                        lambda f, **k: closed.append(f) or 1)
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['signals_closed'] == 1
    assert len(closed) == 1
    assert len(posts) == 1            # no re-post on retry
    st = json.loads(state_p.read_text())
    assert st['pending'] == []


def test_pending_retry_not_found_in_window_stays_pending(monkeypatch, tmp_path):
    """A pending id that has aged out of the broker's returned window (e.g.
    scoping shifted) is left in `pending` rather than dropped — it keeps
    waiting for a run where it reappears."""
    state_p = _state_path(tmp_path)
    monkeypatch.setenv('OPENCLAW_EXIT_FILLS_STATE', str(state_p))
    import execution.stop_reattach as sr
    monkeypatch.setattr(sr, 'fetch_recent_closed_orders', lambda *a, **k: (True, []))
    monkeypatch.setattr(sr, '_post_alert', lambda msg, channel=None: None)
    monkeypatch.setattr(ah, '_open_signal_tickers', lambda **k: ['AAA'])
    monkeypatch.setattr(ah, '_close_signals_for_fill', lambda f, **k: 1)
    _seed_state(tmp_path, pending=['ghost1'])
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['signals_closed'] == 0
    st = json.loads(state_p.read_text())
    assert st['pending'] == ['ghost1']
