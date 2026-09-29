"""C1 amendment 2: drawdown on CUMULATIVE ALPHA P&L. Fake conn/cursor only —
no Postgres, no CLI, no network."""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import account_breaker as ab            # noqa: E402
from execution import alpaca_trader as at              # noqa: E402
from execution import regime_liquidator as rl          # noqa: E402

SESSION = date(2026, 9, 29)
EPOCH = datetime(2026, 9, 29, 13, 30, tzinfo=timezone.utc)


def _fill(t, side, q, px, minute, aid=None):
    return {'ticker': t, 'side': side, 'qty': q, 'price': px,
            'filled_at': datetime(2026, 9, 29, 14, minute, tzinfo=timezone.utc),
            'activity_id': aid or f'a{minute}'}


def _pos(qty, avg, cur, side='long', mv=None):
    return {'qty': qty, 'side': side, 'avg_entry_price': avg, 'current_price': cur,
            'market_value': str(mv if mv is not None else qty * cur)}


# ── pure alpha_pnl ──────────────────────────────────────────────────────────

def test_fifo_buy_buy_sell():
    fills = [_fill('AAPL', 'buy', 10, 100, 1), _fill('AAPL', 'buy', 10, 110, 2),
             _fill('AAPL', 'sell', 15, 120, 3)]
    # FIFO: 10@100 -> +200, 5@110 -> +50; 5 @110 lot remains open
    r = ab.alpha_pnl(100_000, {'AAPL': _pos(5, 110, 115)}, set(), fills, [])
    assert r['realized'] == pytest.approx(250.0)
    assert r['unrealized'] == pytest.approx(25.0)
    assert r['alpha_pnl'] == pytest.approx(275.0)
    assert r['unmatched'] == 0


def test_epoch_lot_matched_by_later_sell():
    lots = [{'ticker': 'MSFT', 'qty': 20, 'avg_entry_price': 300, 'side': 'long'}]
    r = ab.alpha_pnl(100_000, {'MSFT': _pos(10, 300, 310)}, set(),
                     [_fill('MSFT', 'sell', 10, 320, 5)], lots)
    assert r['realized'] == pytest.approx(200.0)
    assert r['unrealized'] == pytest.approx(100.0)
    assert r['unmatched'] == 0


def test_unmatched_sell_is_fail_open_and_counted(caplog):
    with caplog.at_level('WARNING', logger=ab.logger.name):
        r = ab.alpha_pnl(100_000, {}, set(), [_fill('XYZ', 'sell', 7, 50, 1)], [])
    assert r['unmatched'] == 1 and r['realized'] == 0.0
    assert any('unmatched fill' in m for m in (x.getMessage() for x in caplog.records))


def test_partially_unmatched_sell_realizes_only_the_matched_part():
    lots = [{'ticker': 'A', 'qty': 4, 'avg_entry_price': 10, 'side': 'long'}]
    r = ab.alpha_pnl(1, {}, set(), [_fill('A', 'sell', 10, 12, 1)], lots)
    assert r['realized'] == pytest.approx(8.0) and r['unmatched'] == 1


def test_short_position_sign():
    up = ab.alpha_pnl(1, {'TSLA': _pos(-10, 200, 210, side='short')}, set(), [], [])
    assert up['unrealized'] == pytest.approx(-100.0)
    dn = ab.alpha_pnl(1, {'TSLA': _pos(-10, 200, 190, side='short')}, set(), [], [])
    assert dn['unrealized'] == pytest.approx(100.0)


def test_short_epoch_lot_covered_by_buy():
    lots = [{'ticker': 'TSLA', 'qty': 10, 'avg_entry_price': 200, 'side': 'short'}]
    r = ab.alpha_pnl(1, {}, set(), [_fill('TSLA', 'buy', 10, 180, 1)], lots)
    assert r['realized'] == pytest.approx(200.0)


def test_bench_tickers_excluded_from_both_legs_case_insensitive():
    fills = [_fill('SPY', 'buy', 10, 500, 1), _fill('spy', 'sell', 10, 510, 2)]
    lots = [{'ticker': 'SPY', 'qty': 5, 'avg_entry_price': 400, 'side': 'long'}]
    r = ab.alpha_pnl(1, {'SPY': _pos(100, 500, 520)}, {'spy'}, fills, lots)
    assert r['alpha_pnl'] == 0.0 and r['n_positions'] == 0


def test_fills_are_ordered_by_time_not_input_order():
    fills = [_fill('A', 'sell', 5, 20, 2), _fill('A', 'buy', 5, 10, 1)]
    r = ab.alpha_pnl(1, {}, set(), fills, [])
    assert r['realized'] == pytest.approx(50.0) and r['unmatched'] == 0


def test_unparseable_rows_never_raise():
    r = ab.alpha_pnl(1, {'A': {'qty': 'x'}, 'B': {'qty': 3, 'side': 'long'}}, set(),
                     [{'ticker': 'A', 'side': 'buy', 'qty': None, 'price': 'z'}], [])
    assert r['unpriced'] == 1


def test_sleeve_rebalance_leaves_alpha_pnl_flat_while_legacy_alpha_nav_drops():
    """The 2026-09-28 defect: Friday -> Monday the beta budget moved cash into
    SPY. Alpha book unchanged; equity ~flat."""
    alpha_book = {'AAPL': _pos(100, 100, 105)}
    fri = dict(alpha_book, SPY={**_pos(1, 1, 1), 'market_value': '81000'})
    mon = dict(alpha_book, SPY={**_pos(1, 1, 1), 'market_value': '86600'})
    a_fri = ab.alpha_pnl(95_500, fri, {'SPY'}, [], [])['alpha_pnl']
    a_mon = ab.alpha_pnl(95_400, mon, {'SPY'}, [], [])['alpha_pnl']
    assert a_fri == a_mon == pytest.approx(500.0)
    nav_fri, _ = ab.alpha_nav(95_500, fri, {'SPY'})
    nav_mon, _ = ab.alpha_nav(95_400, mon, {'SPY'})
    assert nav_fri == pytest.approx(14_500) and nav_mon == pytest.approx(8_800)
    assert nav_mon / nav_fri - 1 < -0.39            # legacy: a phantom -39 % "drawdown"
    hwm = max(a_fri, a_mon)
    assert (a_mon - hwm) / 95_400 == 0.0


# ── evaluate: dd_override ───────────────────────────────────────────────────

def test_dd_override_drives_the_rule_and_dd_nav_is_ignored():
    # legacy NAV dd is -0.39 (would breach) but the override says -0.01
    st = ab.evaluate(8_800, 14_500, 95_400, 95_500, dd_override=-0.01)
    assert st['breach'] is False and st['rule'] == 'none'
    assert st['dd_nav'] == pytest.approx(8_800 / 14_500 - 1)
    st = ab.evaluate(14_500, 14_500, 95_400, 95_500, dd_override=-0.10)
    assert st['breach'] is True and st['rule'] == 'drawdown'


def test_evaluate_without_override_is_unchanged():
    st = ab.evaluate(90.0, 100.0, 100.0, None)
    assert st['dd'] == pytest.approx(-0.10) and st['rule'] == 'drawdown'


# ── DB layer: fake cursor keyed on SQL ──────────────────────────────────────

class FakeDB:
    """Minimal stateful cursor for account_breaker_state / epoch / fills."""

    def __init__(self, *, peak_pnl=None, epoch_at=None, lots=(), fills=(),
                 halted=False, breached_at=None, peak_nav=None, open_eq=95_400.0):
        self.open_eq = open_eq
        self.peak_pnl, self.epoch_at = peak_pnl, epoch_at
        self.lots, self.fills = list(lots), list(fills)
        self.halted, self.breached_at, self.peak_nav = halted, breached_at, peak_nav
        self.calls, self._one, self._all = [], None, []
        self.rowcount = 1
        self.epoch_inserts = 0

    def execute(self, sql, params=None):
        f = ' '.join(sql.split())
        self.calls.append((f, params))
        self._one, self._all = None, []
        if f.startswith('SELECT halted'):
            self._one = (self.halted, 'drawdown' if self.halted else None, self.breached_at,
                         self.peak_nav, None, None, False)
        elif f.startswith('SELECT peak_alpha_pnl, alpha_epoch_at'):
            self._all = [(self.peak_pnl, self.epoch_at)]
        elif f.startswith('SELECT opening_equity'):
            self._one = (self.open_eq,)
        elif f.startswith('SELECT ticker, qty, avg_entry_price, side'):
            self._all = [(l['ticker'], l['qty'], l['avg_entry_price'], l['side']) for l in self.lots]
        elif f.startswith('SELECT ticker, side, qty, price'):
            self._all = [(x['ticker'], x['side'], x['qty'], x['price'], x['filled_at'],
                          x['activity_id']) for x in self.fills]
        elif f.startswith('UPDATE account_breaker_state SET alpha_epoch_at'):
            if self.epoch_at is not None:
                self.rowcount = 0
            else:
                self.rowcount = 1
                self.epoch_at = EPOCH
        elif f.startswith('INSERT INTO account_breaker_alpha_epoch'):
            self.epoch_inserts += 1
            self.lots.append({'ticker': params[0], 'qty': params[1],
                              'avg_entry_price': params[2], 'side': params[3]})
        elif f.startswith('UPDATE account_breaker_state SET peak_alpha_pnl'):
            self.peak_pnl = params[0]
        elif 'rearmed_at = NOW()' in f:
            self.halted = False

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all

    def sql(self, needle):
        return [c for c in self.calls if needle in c[0]]


class FakeConn:
    autocommit = False

    def __init__(self, cur):
        self._cur, self.committed = cur, 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed += 1

    def close(self):
        pass


BOOK = {'AAPL': _pos(100, 100, 105), 'SPY': _pos(1, 1, 1, mv=81_000)}


def test_first_tick_writes_the_epoch_once_in_one_savepoint():
    db = FakeDB()
    conn = FakeConn(db)
    res = ab.compute_alpha_pnl_tick(db, conn, 95_500, BOOK, {'SPY'})
    assert db.epoch_inserts == 1                       # AAPL only, SPY excluded
    assert db.lots == [{'ticker': 'AAPL', 'qty': 100.0, 'avg_entry_price': 100.0, 'side': 'long'}]
    names = [c[0] for c in db.calls if c[0].startswith(('SAVEPOINT sp_ab_alpha_epoch',
                                                        'RELEASE SAVEPOINT sp_ab_alpha_epoch'))]
    assert names == ['SAVEPOINT sp_ab_alpha_epoch', 'RELEASE SAVEPOINT sp_ab_alpha_epoch']
    assert conn.committed == 1
    assert res['alpha_pnl'] == pytest.approx(500.0) and res['dd_pnl'] == 0.0
    # second tick: epoch already set -> no further write
    ab.compute_alpha_pnl_tick(db, conn, 95_500, BOOK, {'SPY'})
    assert db.epoch_inserts == 1 and len(db.sql('SET alpha_epoch_at')) == 1


def test_epoch_never_moves_when_already_set():
    db = FakeDB(epoch_at=EPOCH)
    assert ab.take_alpha_epoch(db, BOOK, {'SPY'}) is False     # rowcount 0 -> rolled back
    assert db.epoch_inserts == 0 and db.epoch_at == EPOCH


def test_hwm_persists_and_dd_is_nav_denominated():
    db = FakeDB(peak_pnl=2_000.0, epoch_at=EPOCH,
                lots=[{'ticker': 'AAPL', 'qty': 100, 'avg_entry_price': 100, 'side': 'long'}])
    res = ab.compute_alpha_pnl_tick(db, FakeConn(db), 100_000, BOOK, {'SPY'})
    assert res['hwm'] == 2_000.0
    assert res['dd_pnl'] == pytest.approx((500 - 2_000) / 100_000)
    # a new high ratchets the hwm
    book = {'AAPL': _pos(100, 100, 130), 'SPY': BOOK['SPY']}
    res = ab.compute_alpha_pnl_tick(db, FakeConn(db), 100_000, book, {'SPY'})
    assert res['hwm'] == 3_000.0 and res['dd_pnl'] == 0.0


def test_unreadable_state_falls_back_to_legacy():
    class Broken(FakeDB):
        def execute(self, sql, params=None):
            if 'alpha_epoch_at' in sql and sql.lstrip().startswith('SELECT'):
                raise RuntimeError('column does not exist')
            super().execute(sql, params)
    db = Broken()
    assert ab.compute_alpha_pnl_tick(db, FakeConn(db), 1e5, BOOK, {'SPY'}) is None


def test_clear_halt_resets_hwm_not_epoch():
    db = FakeDB(peak_pnl=5_000.0, epoch_at=EPOCH, halted=True)
    assert ab.clear_halt(db, 9_000.0, alpha_pnl_now=1_234.0) is True
    assert db.peak_pnl == 1_234.0 and db.epoch_at == EPOCH
    assert db.sql('alpha_epoch_at =') == [] and db.epoch_inserts == 0
    # legacy-only callers (no alpha_pnl_now) leave the hwm alone
    db2 = FakeDB(peak_pnl=5_000.0, epoch_at=EPOCH)
    ab.clear_halt(db2, 9_000.0)
    assert db2.peak_pnl == 5_000.0


def test_save_state_persists_peak_alpha_pnl_after_the_latch_write():
    db = FakeDB()
    ab.save_state(db, halted=True, reason='drawdown', breached_at=EPOCH, peak=1.0, dd=-0.1,
                  daily=None, pending_flatten=True, peak_alpha_pnl=777.0)
    kinds = [c[0] for c in db.calls if c[0].startswith('UPDATE')]
    assert kinds[0].startswith('UPDATE account_breaker_state SET halted')
    assert db.peak_pnl == 777.0


def test_format_line_appends_new_fields_and_keeps_existing():
    st = {'peak': 14_500.0, 'dd': -0.0123, 'dd_nav': -0.39, 'daily': -0.001,
          'rule': 'none', 'breach': False}
    pnl = {'alpha_pnl': 500.0, 'realized': 100.0, 'unrealized': 400.0, 'hwm': 800.0,
           'dd_pnl': -0.0123, 'unmatched': 2}
    line = ab.format_line('shadow', equity=95_400.0, bench_mv=86_600.0, alpha=8_800.0, st=st,
                          open_equity=95_500.0, open_src='stored', halted=False, pnl=pnl)
    print(line)
    assert line.startswith('[account_breaker] shadow equity=95400.00 bench_mv=86600.00 '
                           'alpha_nav=8800.00 peak=14500.00 dd=-0.0123 ')
    assert ('halted=0 | alpha_pnl=500.00 realized=100.00 unrealized=400.00 hwm=800.00 '
            'dd_pnl=-0.0123 unmatched=2 | legacy alpha_nav=8800.00 dd_nav=-0.3900') in line
    # without pnl the line is byte-identical to the legacy format
    assert '|' not in ab.format_line('shadow', equity=1.0, bench_mv=0.0, alpha=1.0, st=st,
                                     open_equity=1.0, open_src='x', halted=False)


# ── run_once end to end ─────────────────────────────────────────────────────

@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv('POSTGRES_URI', 'postgres://stub')
    monkeypatch.setenv(ab.NAV_OHLC_PATH_ENV, str(tmp_path / 'missing.json'))
    monkeypatch.delenv(ab.ARM_ENV, raising=False)
    monkeypatch.delenv(ab.REARM_ENV, raising=False)
    monkeypatch.setattr(ab, '_SETTLE_S', 0)
    monkeypatch.setattr(rl, '_market_is_open', lambda: True)
    monkeypatch.setattr(rl, '_load_open_orders', lambda: [])
    monkeypatch.setattr(at, '_alpaca_session', lambda: object())
    monkeypatch.setattr(ab, 'bench_tickers', lambda cur: {'SPY'})
    monkeypatch.setattr(rl, '_post_to_discord', lambda ch, msg: True)
    h = {}

    def install(db, equity, book):
        conn = FakeConn(db)
        monkeypatch.setattr(ab.psycopg2, 'connect', lambda *_a, **_k: conn)
        monkeypatch.setattr(at, '_fetch_account_state', lambda s: {'equity': equity})
        monkeypatch.setattr(rl, '_load_broker_positions', lambda: dict(book))
        return conn
    h['install'] = install
    return h


def _last_line(caplog):
    return [m for m in (r.getMessage() for r in caplog.records)
            if m.startswith('[account_breaker] ')][-1]


def test_run_once_clean_tick_uses_pnl_measure_and_ignores_dd_nav(wired, caplog):
    """Legacy peak 14.5k vs alpha_nav 8.8k is dd_nav -0.39; alpha P&L is flat."""
    db = FakeDB(peak_pnl=500.0, epoch_at=EPOCH, peak_nav=14_500.0,
                lots=[{'ticker': 'AAPL', 'qty': 100, 'avg_entry_price': 100, 'side': 'long'}])
    book = {'AAPL': _pos(100, 100, 105), 'SPY': _pos(1, 1, 1, mv=86_600)}
    wired['install'](db, 95_400.0, book)
    with caplog.at_level('INFO', logger=ab.logger.name):
        assert ab.run_once(session_date=SESSION) == 0
    line = _last_line(caplog)
    assert 'rule=none' in line and 'breach=0' in line
    assert 'alpha_pnl=500.00' in line and 'dd_nav=-0.39' in line and 'dd_pnl=0.0000' in line
    assert db.peak_pnl == 500.0


def test_run_once_dd_pnl_breach_at_minus_ten_percent(wired, caplog):
    db = FakeDB(peak_pnl=20_000.0, epoch_at=EPOCH, peak_nav=1e9,
                lots=[{'ticker': 'AAPL', 'qty': 100, 'avg_entry_price': 100, 'side': 'long'}])
    # alpha_pnl = 100 * (105 - 100) = 500 ; (500 - 20000)/100000 = -0.195 -> breach
    book = {'AAPL': _pos(100, 100, 105), 'SPY': _pos(1, 1, 1, mv=1_000)}
    wired['install'](db, 100_000.0, book)
    with caplog.at_level('INFO', logger=ab.logger.name):
        assert ab.run_once(session_date=SESSION) == 0
    line = _last_line(caplog)
    assert 'rule=drawdown' in line and 'breach=1' in line and 'halted=0' in line  # shadow
    assert 'flatten_ok=1' in line


def test_run_once_first_tick_snapshots_epoch_and_never_breaches(wired, caplog):
    db = FakeDB()
    conn = wired['install'](db, 100_000.0, BOOK)
    with caplog.at_level('INFO', logger=ab.logger.name):
        assert ab.run_once(session_date=SESSION) == 0
    assert db.epoch_inserts == 1 and db.epoch_at == EPOCH
    line = _last_line(caplog)
    assert 'breach=0' in line and 'dd_pnl=0.0000' in line
    assert db.peak_pnl == 500.0
    assert conn.committed >= 2


def test_run_once_rearm_resets_hwm_and_keeps_epoch(wired, monkeypatch):
    breached = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setenv(ab.REARM_ENV, breached.isoformat())
    db = FakeDB(peak_pnl=20_000.0, epoch_at=EPOCH, halted=True, breached_at=breached,
                peak_nav=1e9, lots=[{'ticker': 'AAPL', 'qty': 100, 'avg_entry_price': 100,
                                     'side': 'long'}])
    wired['install'](db, 100_000.0, BOOK)
    assert ab.run_once(session_date=SESSION) == 0
    assert db.epoch_at == EPOCH and db.epoch_inserts == 0
    assert db.peak_pnl == 500.0                      # reset to the current alpha_pnl
