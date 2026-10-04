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
from execution import alpaca_reconcile as ar           # noqa: E402
from execution import stop_reattach as sr              # noqa: E402
from execution import alpaca_trader as at              # noqa: E402
from execution import regime_liquidator as rl          # noqa: E402

_REAL_FETCH_SINCE = ar.fetch_fills_since      # the autouse fixture below stubs the module attr
SESSION = date(2026, 9, 29)
EPOCH = datetime(2026, 9, 29, 13, 30, tzinfo=timezone.utc)


def _fill(t, side, q, px, minute, aid=None):
    return {'ticker': t, 'side': side, 'qty': q, 'price': px,
            'filled_at': datetime(2026, 9, 29, 14, minute, tzinfo=timezone.utc),
            'activity_id': aid or f'a{minute}'}


def _pos(qty, avg, cur, side='long', mv=None, upl=None):
    p = {'qty': qty, 'side': side, 'avg_entry_price': avg, 'current_price': cur,
         'market_value': str(mv if mv is not None else qty * cur)}
    if upl is not None:
        p['unrealized_pl'] = str(upl)
    return p


FEED = []          # broker FILL activities the stubbed CLI returns (raw shape)


@pytest.fixture(autouse=True)
def _no_broker(monkeypatch):
    """Never touch the Alpaca CLI: the since-epoch pull and the closed-order
    enrichment are stubbed."""
    FEED.clear()
    monkeypatch.setattr(ar, 'fetch_fills_since', lambda after, **k: list(FEED))
    monkeypatch.setattr(sr, 'fetch_recent_closed_orders', lambda *a, **k: (False, []))
    monkeypatch.setattr(ab, '_LAST_ACTIVITY_ID', None)


def _act(aid, sym, side, q, px, minute):
    return {'id': aid, 'order_id': 'o' + aid, 'symbol': sym, 'side': side,
            'qty': str(q), 'price': str(px),
            'transaction_time': f'2026-09-29T14:{minute:02d}:00Z'}


# ── pure alpha_pnl ──────────────────────────────────────────────────────────

def test_average_cost_buy_buy_sell():
    fills = [_fill('AAPL', 'buy', 10, 100, 1), _fill('AAPL', 'buy', 10, 110, 2),
             _fill('AAPL', 'sell', 15, 120, 3)]
    # avg 105; sell 15 -> 15*(120-105) = 225; 5 @105 remain
    r = ab.alpha_pnl(100_000, {'AAPL': _pos(5, 105, 115)}, set(), fills, [])
    assert r['realized'] == pytest.approx(225.0)
    assert r['unrealized'] == pytest.approx(50.0)
    assert r['alpha_pnl'] == pytest.approx(275.0)
    assert r['unmatched'] == 0 and r['recon'] == 0


def test_epoch_lot_matched_by_later_sell():
    lots = [{'ticker': 'MSFT', 'qty': 20, 'avg_entry_price': 300, 'side': 'long'}]
    r = ab.alpha_pnl(100_000, {'MSFT': _pos(10, 300, 310)}, set(),
                     [_fill('MSFT', 'sell', 10, 320, 5)], lots)
    assert r['realized'] == pytest.approx(200.0)
    assert r['unrealized'] == pytest.approx(100.0)
    assert r['unmatched'] == 0 and r['recon'] == 0


def test_unknown_side_is_counted_never_dropped_silently(caplog):
    with caplog.at_level('WARNING', logger=ab.logger.name):
        r = ab.alpha_pnl(100_000, {}, set(), [_fill('XYZ', 'weird', 7, 50, 1)], [])
    assert r['unmatched'] == 1 and r['realized'] == 0.0
    assert any('unknown side' in x.getMessage() for x in caplog.records)


def test_oversized_sell_realizes_the_long_part_and_opens_a_short():
    lots = [{'ticker': 'A', 'qty': 4, 'avg_entry_price': 10, 'side': 'long'}]
    r = ab.alpha_pnl(1, {}, set(), [_fill('A', 'sell', 10, 12, 1)], lots)
    assert r['realized'] == pytest.approx(8.0) and r['unmatched'] == 0
    assert r['recon'] == 1                       # ledger short 6, broker flat


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
    assert r['unmatched'] == 1                   # the unusable fill is counted


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

def test_dd_drives_the_rule():
    st = ab.evaluate(-0.01, 95_400, 95_500)
    assert st['breach'] is False and st['dd'] == -0.01
    st = ab.evaluate(-0.10, 95_400, 95_500)
    assert st['breach'] is True and st['rule'] == 'drawdown'


def test_evaluate_without_a_measure_skips_the_drawdown_rule():
    """P4: no legacy NAV fallback — dd None == skip_drawdown; daily still runs."""
    st = ab.evaluate(None, 100.0, None)
    assert st['dd'] == 0.0 and st['rule'] == 'none' and st['breach'] is False
    assert 'dd_nav' not in st and 'peak' not in st
    assert ab.evaluate(None, 90_000, 95_500)['rule'] == 'daily_loss'


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
        self.watermark = None
        self.peak_nav_writes = 0

    def execute(self, sql, params=None):
        f = ' '.join(sql.split())
        self.calls.append((f, params))
        self._one, self._all = None, []
        if f.startswith('SELECT halted'):
            self._one = (self.halted, 'drawdown' if self.halted else None, self.breached_at,
                         self.peak_nav, None, None, False)
        elif f.startswith('SELECT last_synced_filled_at'):
            self._all = [(self.watermark,)]
        elif f.startswith('UPDATE account_breaker_state SET last_synced_filled_at'):
            self.watermark = max(x for x in (self.watermark, params[1]) if x is not None)
        elif f.startswith('SELECT peak_alpha_pnl, alpha_epoch_at'):
            self._all = [(self.peak_pnl, self.epoch_at)]
        elif f.startswith('SELECT opening_equity'):
            self._one = (self.open_eq,)
        elif f.startswith('SELECT ticker, qty, avg_entry_price, side'):
            self._all = [(l['ticker'], l['qty'], l['avg_entry_price'], l['side']) for l in self.lots]
        elif f.startswith('SELECT ticker, side, qty, price'):
            self._all = [(x['ticker'], x['side'], x['qty'], x['price'], x['filled_at'],
                          x['activity_id']) for x in self.fills]
        elif f.startswith('SELECT activity_id FROM broker_fills'):
            have = {x['activity_id'] for x in self.fills}
            self._all = [(i,) for i in params[0] if i in have]
        elif f.startswith('INSERT INTO broker_fills'):
            self.fill_inserts = getattr(self, 'fill_inserts', 0) + 1
            if params[0] not in {x['activity_id'] for x in self.fills}:
                self.fills.append({'ticker': params[4], 'side': params[5], 'qty': params[8],
                                   'price': params[9], 'activity_id': params[0],
                                   'filled_at': datetime.fromisoformat(
                                       params[10].replace('Z', '+00:00'))})
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
            if 'peak_alpha_pnl = NULL' in f:
                self.peak_pnl = None
            elif 'peak_alpha_pnl = %s' in f:
                self.peak_pnl = params[0]
        if 'peak_alpha_nav =' in f and f.startswith('UPDATE'):
            self.peak_nav_writes += 1

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
    # no alpha_pnl_now (distrusted ledger): the hwm is CLEARED in the same UPDATE
    db2 = FakeDB(peak_pnl=5_000.0, epoch_at=EPOCH)
    ab.clear_halt(db2, 9_000.0)
    assert db2.peak_pnl is None and db2.sql('SET peak_alpha_pnl') == []


def test_save_state_persists_peak_alpha_pnl_after_the_latch_write():
    db = FakeDB()
    ab.save_state(db, halted=True, reason='drawdown', breached_at=EPOCH, peak=1.0, dd=-0.1,
                  daily=None, pending_flatten=True, peak_alpha_pnl=777.0)
    kinds = [c[0] for c in db.calls if c[0].startswith('UPDATE')]
    assert kinds[0].startswith('UPDATE account_breaker_state SET halted')
    assert db.peak_pnl == 777.0


def test_format_line_appends_new_fields_and_keeps_existing():
    st = {'peak': 14_500.0, 'dd': -0.0123, 'daily': -0.001,
          'rule': 'none', 'breach': False}
    pnl = {'alpha_pnl': 500.0, 'realized': 100.0, 'unrealized': 400.0, 'hwm': 800.0,
           'dd_pnl': -0.0123, 'unmatched': 2, 'recon': 3, 'excluded': 1,
           'excluded_by_class': {'crypto': 1}}
    line = ab.format_line('shadow', equity=95_400.0, bench_mv=86_600.0, alpha=8_800.0, st=st,
                          open_equity=95_500.0, open_src='stored', halted=False, pnl=pnl)
    print(line)
    assert line.startswith('[account_breaker] shadow equity=95400.00 bench_mv=86600.00 '
                           'alpha_nav=8800.00 peak=14500.00 dd=-0.0123 ')
    assert ('halted=0 | alpha_pnl=500.00 realized=100.00 unrealized=400.00 hwm=800.00 '
            'dd_pnl=-0.0123 unmatched=2 recon=3 excluded=crypto:1 rebased=0') in line
    assert line.endswith('excluded=crypto:1 rebased=0') and 'legacy' not in line and 'dd_nav' not in line     # P4
    # without pnl the line is byte-identical to the legacy format
    assert '|' not in ab.format_line('shadow', equity=1.0, bench_mv=0.0, alpha=1.0, st=st,
                                     open_equity=1.0, open_src='x', halted=False)


# ── run_once end to end ─────────────────────────────────────────────────────

@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv('POSTGRES_URI', 'postgres://stub')
    monkeypatch.setenv(ab.NAV_OHLC_PATH_ENV, str(tmp_path / 'missing.json'))
    monkeypatch.setenv(ab.FALLBACK_POST_PATH_ENV, str(tmp_path / 'fallback_post.ts'))
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
    assert 'alpha_pnl=500.00' in line and 'dd_pnl=0.0000' in line
    assert 'dd_nav' not in line and 'legacy' not in line and 'excluded=0' in line   # P4
    assert db.peak_nav_writes == 0                                  # P4: column no longer written
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


# ── fix round 1: F1 fill sync, F2 average cost, F3 fallback, F4 scope ───────

def test_exit_fill_absent_from_broker_fills_is_fetched_and_upserted():
    db = FakeDB(epoch_at=EPOCH,
                lots=[{'ticker': 'AAPL', 'qty': 100, 'avg_entry_price': 100, 'side': 'long'}])
    FEED.append(_act('x1', 'AAPL', 'sell', 100, 110, 5))
    assert db.fills == []                              # reconcile never ran
    res = ab.compute_alpha_pnl_tick(db, FakeConn(db), 100_000, {'MSFT': _pos(1, 10, 10)},
                                    {'SPY'})
    assert getattr(db, 'fill_inserts', 0) == 1 and len(db.fills) == 1
    assert res['realized'] == pytest.approx(1_000.0)   # +$1,000 exit no longer invisible
    assert res['unmatched'] == 0
    assert ab._LAST_ACTIVITY_ID == 'x1'
    # idempotent: the same feed next tick inserts nothing new into the ledger
    res2 = ab.compute_alpha_pnl_tick(db, FakeConn(db), 100_000, {'MSFT': _pos(1, 10, 10)},
                                     {'SPY'})
    assert len(db.fills) == 1 and res2['realized'] == pytest.approx(1_000.0)


def test_fill_fetch_failure_falls_back_no_partial_ledger(monkeypatch):
    def boom(after, **k):
        raise RuntimeError('cli down')
    monkeypatch.setattr(ar, 'fetch_fills_since', boom)
    db = FakeDB(epoch_at=EPOCH)
    assert ab.compute_alpha_pnl_tick(db, FakeConn(db), 1e5, BOOK, {'SPY'}) is None


def test_fetch_window_is_epoch_floored_utc(monkeypatch):
    seen = []
    monkeypatch.setattr(ar, 'fetch_fills_since', lambda after, **k: seen.append(after) or [])
    db = FakeDB(epoch_at=EPOCH)
    ab.compute_alpha_pnl_tick(db, FakeConn(db), 1e5, BOOK, {'SPY'})
    assert seen == ['2026-09-29T13:30:00Z']


def test_post_epoch_short_opened_then_covered():
    fills = [_fill('NVDA', 'sell', 10, 50, 1), _fill('NVDA', 'buy', 10, 45, 2)]
    r = ab.alpha_pnl(1, {}, set(), fills, [])
    assert r['realized'] == pytest.approx(50.0)
    assert r['unmatched'] == 0 and r['recon'] == 0
    # ...and while it is still open the short's unrealized is signed correctly
    r = ab.alpha_pnl(1, {'NVDA': _pos(-10, 50, 48, side='short')}, set(),
                     [_fill('NVDA', 'sell', 10, 50, 1)], [])
    assert r['realized'] == 0.0 and r['unrealized'] == pytest.approx(20.0) and r['recon'] == 0


def test_long_to_short_flip():
    fills = [_fill('A', 'buy', 10, 100, 1), _fill('A', 'sell', 15, 110, 2),
             _fill('A', 'buy', 5, 100, 3)]
    r = ab.alpha_pnl(1, {}, set(), fills, [])
    # 10*(110-100) on the long; 5 short opened @110 then covered @100 -> +50
    assert r['realized'] == pytest.approx(150.0) and r['recon'] == 0
    r = ab.alpha_pnl(1, {'A': _pos(-5, 110, 105, side='short')}, set(), fills[:2], [])
    assert r['realized'] == pytest.approx(100.0)
    assert r['unrealized'] == pytest.approx(25.0) and r['recon'] == 0


def test_sell_short_is_a_sell():
    fills = [_fill('T', 'sell_short', 10, 200, 1), _fill('T', 'buy', 4, 190, 2)]
    r = ab.alpha_pnl(1, {'T': _pos(-6, 200, 195, side='short')}, set(), fills, [])
    assert r['realized'] == pytest.approx(40.0) and r['unmatched'] == 0
    assert r['unrealized'] == pytest.approx(30.0) and r['recon'] == 0


def test_negative_qty_with_no_side_is_a_short_sale():
    f = _fill('T', None, -10, 200, 1)
    r = ab.alpha_pnl(1, {'T': _pos(-10, 200, 190, side='short')}, set(), [f], [])
    assert r['unmatched'] == 0 and r['recon'] == 0
    assert r['unrealized'] == pytest.approx(100.0)
    # positive qty with no side is genuinely unknown -> counted
    assert ab.alpha_pnl(1, {}, set(), [_fill('T', '', 10, 200, 1)], [])['unmatched'] == 1


def test_scaled_in_partial_sell_under_average_cost_is_phantom_free():
    fills = [_fill('AAPL', 'buy', 10, 100, 1), _fill('AAPL', 'buy', 10, 120, 2),
             _fill('AAPL', 'sell', 10, 110, 3)]
    # broker avg 110 -> sell at 110 realizes exactly 0 (FIFO would book +100)
    for pos in (_pos(10, 110, 115), _pos(10, 110, 115, upl=50)):
        r = ab.alpha_pnl(1, {'AAPL': pos}, set(), fills, [])
        assert r['realized'] == pytest.approx(0.0)
        assert r['unrealized'] == pytest.approx(50.0)     # broker's, or qty*(cur-avg)
        assert r['recon'] == 0


def test_broker_unrealized_pl_is_preferred_when_present():
    r = ab.alpha_pnl(1, {'A': _pos(10, 100, 105, upl=37.5)}, set(), [], [])
    assert r['unrealized'] == pytest.approx(37.5)


def test_crypto_key_normalisation_and_scope_exclusion():
    assert ab._pos_key('BTC/USD') == ab._pos_key('btcusd') == 'BTCUSD'
    fills = [_fill('BTC/USD', 'buy', 1, 60_000, 1), _fill('AAPL', 'buy', 5, 100, 2)]
    book = {'BTCUSD': _pos(1, 60_000, 61_000), 'AAPL': _pos(5, 100, 101)}
    r = ab.alpha_pnl(1, book, set(), fills, [])
    assert r['excluded'] == 1 and r['n_positions'] == 1     # BTC never leaks either leg
    assert r['alpha_pnl'] == pytest.approx(5.0) and r['recon'] == 0


def test_option_legs_excluded_from_both_legs_and_counted():
    occ = 'AAPL260918C00200000'
    fills = [_fill(occ, 'buy', 1, 5.0, 1), _fill(occ, 'sell', 1, 6.0, 2)]
    r = ab.alpha_pnl(1, {occ: _pos(1, 5.0, 6.0, upl=100)}, set(), fills, [])
    assert r['excluded'] == 1 and r['alpha_pnl'] == 0.0 and r['n_positions'] == 0


def test_recon_counts_qty_mismatches_incl_closed_positions_with_ledger_qty():
    lots = [{'ticker': 'AAPL', 'qty': 100, 'avg_entry_price': 100, 'side': 'long'},
            {'ticker': 'MSFT', 'qty': 10, 'avg_entry_price': 300, 'side': 'long'},
            {'ticker': 'OK', 'qty': 5, 'avg_entry_price': 10, 'side': 'long'}]
    book = {'AAPL': _pos(90, 100, 101), 'OK': _pos(5, 10, 10)}    # MSFT closed at broker
    r = ab.alpha_pnl(1, book, set(), [], lots)
    assert r['recon'] == 2
    line = ab.format_line('shadow', equity=1.0, bench_mv=0.0, alpha=1.0,
                          st={'peak': 1.0, 'dd': 0.0, 'daily': None,
                              'rule': 'none', 'breach': False},
                          open_equity=1.0, open_src='x', halted=False,
                          pnl=dict(r, hwm=0.0, dd_pnl=0.0))
    assert 'recon=2' in line


def test_losing_tick_of_the_epoch_race_falls_back_and_never_writes_a_second_epoch():
    class Race(FakeDB):
        def execute(self, sql, params=None):
            f = ' '.join(sql.split())
            if f.startswith('UPDATE account_breaker_state SET alpha_epoch_at'):
                self.calls.append((f, params))
                self.rowcount = 0                 # the other tick already stamped it
                return
            super().execute(sql, params)
    db = Race()
    conn = FakeConn(db)
    assert ab.compute_alpha_pnl_tick(db, conn, 1e5, BOOK, {'SPY'}) is None
    assert db.epoch_inserts == 0 and db.lots == [] and conn.committed == 0


def test_evaluate_skip_drawdown_ignores_dd_but_daily_still_fires():
    st = ab.evaluate(-0.39, 95_400, 95_500, skip_drawdown=True)
    assert st['breach'] is False and st['rule'] == 'none' and st['dd'] == 0.0
    st = ab.evaluate(-0.39, 90_000, 95_500, skip_drawdown=True)   # daily -5.8 %
    assert st['rule'] == 'daily_loss'


def test_armed_fallback_tick_skips_drawdown_and_posts_once_per_30_min(
        wired, caplog, monkeypatch, tmp_path):
    class Broken(FakeDB):
        def execute(self, sql, params=None):
            if 'alpha_epoch_at' in sql and sql.lstrip().startswith('SELECT'):
                raise RuntimeError('column does not exist')
            super().execute(sql, params)
    monkeypatch.setenv(ab.ARM_ENV, '1')
    monkeypatch.setenv(ab.FALLBACK_POST_PATH_ENV, str(tmp_path / 'fb.ts'))
    posts = []
    monkeypatch.setattr(rl, '_post_to_discord', lambda ch, msg: posts.append((ch, msg)) or True)
    # legacy dd_nav = 8.8k/14.5k - 1 = -0.39 would latch + flatten if it fed the rule
    book = {'AAPL': _pos(100, 100, 105), 'SPY': _pos(1, 1, 1, mv=86_600)}
    for _ in range(2):
        db = Broken(peak_nav=14_500.0, epoch_at=EPOCH)
        conn = wired['install'](db, 95_400.0, book)
        caplog.clear()
        with caplog.at_level('INFO', logger=ab.logger.name):
            assert ab.run_once(session_date=SESSION) == 0
        line = _last_line(caplog)
        assert 'rule=none' in line and 'breach=0' in line and 'halted=0' in line
        assert 'dd_pnl=n/a' in line and 'dd_nav' not in line
        assert not db.halted
    degraded = [p for p in posts if p[0] == 'trade-reports' and 'DRAWDOWN rule is' in p[1]]
    assert len(degraded) == 1                       # second tick throttled
    # after 30 minutes it posts again
    t0 = float((tmp_path / 'fb.ts').read_text())
    assert ab.post_fallback_notice(now=t0 + 31 * 60) is True
    assert ab.post_fallback_notice(now=t0 + 32 * 60) is False


# ── Task 2 / P1: persisted fill watermark + bounded pull ────────────────────

WM = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)


def _capture_fetch(monkeypatch, feed=()):
    seen = []

    def fake(after, **k):
        seen.append((after, k))
        return list(feed)
    monkeypatch.setattr(ar, 'fetch_fills_since', fake)
    return seen


def test_fetch_starts_at_watermark_minus_ten_minutes(monkeypatch):
    seen = _capture_fetch(monkeypatch)
    db = FakeDB(epoch_at=EPOCH)
    db.watermark = WM
    assert ab.sync_fills_since_epoch(db, EPOCH) is True
    assert seen[0][0] == '2026-09-30T14:50:00Z'


def test_fetch_never_starts_before_the_epoch(monkeypatch):
    seen = _capture_fetch(monkeypatch)
    db = FakeDB(epoch_at=EPOCH)
    db.watermark = EPOCH.replace(minute=35)             # wm - 10 min < epoch
    ab.sync_fills_since_epoch(db, EPOCH)
    assert seen[0][0] == '2026-09-29T13:30:00Z'
    seen.clear()
    db.watermark = None                                 # never synced
    ab.sync_fills_since_epoch(db, EPOCH)
    assert seen[0][0] == '2026-09-29T13:30:00Z'


def test_fetch_is_bounded_and_opts_into_raising(monkeypatch):
    seen = _capture_fetch(monkeypatch)
    ab.sync_fills_since_epoch(FakeDB(epoch_at=EPOCH), EPOCH)
    k = seen[0][1]
    assert k['max_pages'] == ab.FILL_MAX_PAGES == 20
    assert k['deadline_s'] == ab.FILL_BUDGET_S == 60 and k['raise_on_cap'] is True


def test_watermark_advances_to_max_filled_at_only_after_a_successful_upsert(monkeypatch):
    _capture_fetch(monkeypatch, [_act('w1', 'AAPL', 'buy', 1, 10, 5),
                                 _act('w2', 'AAPL', 'sell', 1, 11, 9)])
    db = FakeDB(epoch_at=EPOCH)
    assert ab.sync_fills_since_epoch(db, EPOCH) is True
    assert db.watermark == datetime(2026, 9, 29, 14, 9, tzinfo=timezone.utc)
    # a failing upsert leaves the watermark alone and reports failure
    class Bad(FakeDB):
        def execute(self, sql, params=None):
            if 'INSERT INTO broker_fills' in sql:
                raise RuntimeError('boom')
            super().execute(sql, params)
    bad = Bad(epoch_at=EPOCH)
    assert ab.sync_fills_since_epoch(bad, EPOCH) is False
    assert bad.watermark is None


def test_watermark_never_moves_backwards(monkeypatch):
    _capture_fetch(monkeypatch, [_act('w1', 'AAPL', 'buy', 1, 10, 5)])
    db = FakeDB(epoch_at=EPOCH)
    db.watermark = WM
    ab.sync_fills_since_epoch(db, EPOCH)
    assert db.watermark == WM
    assert any('GREATEST' in c[0] for c in db.sql('last_synced_filled_at'))


def test_ledger_still_reads_the_full_since_epoch_set_from_sql(monkeypatch):
    """The watermark narrows the BROKER fetch only: an old fill that is not in
    the narrow pull still counts, because the ledger reads broker_fills."""
    old = {'ticker': 'AAPL', 'side': 'buy', 'qty': 10, 'price': 100,
           'filled_at': datetime(2026, 9, 29, 14, 1, tzinfo=timezone.utc), 'activity_id': 'old'}
    db = FakeDB(epoch_at=EPOCH, fills=[old])
    db.watermark = WM
    _capture_fetch(monkeypatch, [_act('new', 'AAPL', 'sell', 10, 110, 30)])
    res = ab.compute_alpha_pnl_tick(db, FakeConn(db), 1e5, {'MSFT': _pos(1, 10, 10)}, {'SPY'})
    assert res['realized'] == pytest.approx(100.0)       # old buy + new sell
    full = db.sql('FROM broker_fills WHERE filled_at >= %s')
    assert full and full[0][1] == (EPOCH,)


def test_page_cap_hit_returns_false_and_no_ledger(monkeypatch):
    def capped(after, **k):
        raise ar.FillPagesTruncated('cap', [_act('c1', 'AAPL', 'buy', 1, 10, 5)])
    monkeypatch.setattr(ar, 'fetch_fills_since', capped)
    db = FakeDB(epoch_at=EPOCH)
    assert ab.sync_fills_since_epoch(db, EPOCH) is False
    assert ab.compute_alpha_pnl_tick(db, FakeConn(db), 1e5, BOOK, {'SPY'}) is None
    # the rows that did arrive are kept (append-only) and the watermark advanced
    # over them, so a long first catch-up converges instead of livelocking
    assert getattr(db, 'fill_inserts', 0) >= 1 and db.watermark is not None


def _cli(monkeypatch, pages):
    calls = []

    class P:
        def __init__(self, out):
            self.returncode, self.stdout, self.stderr = 0, out, ''

    def run(args, **k):
        calls.append(k.get('timeout'))
        return P(pages[min(len(calls) - 1, len(pages) - 1)])
    monkeypatch.setattr(ar.subprocess, 'run', run)
    return calls


def _page(n, start=0):
    import json
    return json.dumps([{'id': f'id{start + i}'} for i in range(n)])


def test_fetch_fill_pages_raises_on_cap_only_when_opted_in(monkeypatch):
    calls = _cli(monkeypatch, [_page(100)])              # every page is full
    with pytest.raises(ar.FillPagesTruncated) as ei:
        _REAL_FETCH_SINCE('2026-09-29T13:30:00Z', max_pages=3, raise_on_cap=True)
    assert len(calls) == 3 and len(ei.value.fills) == 300
    # default behaviour (fetch_fills_for_date and legacy callers): silent stop
    calls.clear()
    assert len(ar.fetch_fills_for_date('2026-09-29', max_pages=3)) == 300
    assert len(_REAL_FETCH_SINCE('2026-09-29T13:30:00Z', max_pages=3)) == 300


def test_fetch_fill_pages_short_page_is_complete_not_truncated(monkeypatch):
    _cli(monkeypatch, [_page(100), _page(7, 100)])
    out = _REAL_FETCH_SINCE('2026-09-29T13:30:00Z', max_pages=2, raise_on_cap=True)
    assert len(out) == 107                               # ended on a short page at the cap


def test_fetch_fill_pages_wall_clock_budget(monkeypatch):
    _cli(monkeypatch, [_page(100)])
    import types
    ticks = iter([0.0, 0.0])
    # first two reads (deadline + page 1) are inside the budget, then 61 s elapsed
    monkeypatch.setattr(ar, 'time', types.SimpleNamespace(
        monotonic=lambda: next(ticks, 61.0)))
    with pytest.raises(ar.FillPagesTruncated):
        _REAL_FETCH_SINCE('2026-09-29T13:30:00Z', raise_on_cap=True, deadline_s=60)


def test_migration_161_is_additive_only():
    sql = (ROOT / 'src' / 'database' / 'migrations' /
           '161_account_breaker_fill_watermark.sql').read_text()
    assert 'ADD COLUMN IF NOT EXISTS last_synced_filled_at TIMESTAMPTZ' in sql
    for bad in ('DROP ', 'DELETE ', 'TRUNCATE '):
        assert bad not in sql.upper()


# ── Task 2 / P2: recon > 0 on an ARMED tick == pnl is None ──────────────────

@pytest.fixture
def no_flatten(monkeypatch):
    calls = []
    monkeypatch.setattr(ab, 'flatten_alpha', lambda *a, **k: calls.append(1) or {
        'ok': 0, 'fail': 0, 'partial': 0, 'pending': False, 'aborted': False, 'tickers': []})
    return calls


def _recon_book():
    # ledger (epoch lot) says 100 AAPL, broker says 90 -> recon=1
    return {'AAPL': _pos(90, 100, 105), 'SPY': _pos(1, 1, 1, mv=1_000)}


def _recon_db():
    return FakeDB(peak_pnl=20_000.0, epoch_at=EPOCH,
                  lots=[{'ticker': 'AAPL', 'qty': 100, 'avg_entry_price': 100, 'side': 'long'}])


def test_armed_recon_mismatch_skips_drawdown_and_posts_degraded_notice(
        wired, caplog, monkeypatch, tmp_path, no_flatten):
    monkeypatch.setenv(ab.ARM_ENV, '1')
    posts = []
    monkeypatch.setattr(rl, '_post_to_discord', lambda ch, msg: posts.append((ch, msg)) or True)
    db = _recon_db()                  # dd_pnl would be (450-20000)/1e5 = -0.1955 -> breach
    wired['install'](db, 100_000.0, _recon_book())
    with caplog.at_level('INFO', logger=ab.logger.name):
        assert ab.run_once(session_date=SESSION) == 0
    line = _last_line(caplog)
    assert 'rule=none' in line and 'breach=0' in line and 'halted=0' in line
    assert 'dd_pnl=n/a' in line and 'recon=1' in line and not db.halted
    assert no_flatten == []
    assert any(c == 'trade-reports' and 'DRAWDOWN rule is' in m for c, m in posts)


def test_armed_recon_mismatch_still_runs_the_daily_loss_rule(wired, caplog, monkeypatch,
                                                             no_flatten):
    monkeypatch.setenv(ab.ARM_ENV, '1')
    db = _recon_db()
    db.open_eq = 100_000.0
    wired['install'](db, 90_000.0, _recon_book())          # daily -10 %
    with caplog.at_level('INFO', logger=ab.logger.name):
        ab.run_once(session_date=SESSION)
    assert 'rule=daily_loss' in _last_line(caplog)


def test_shadow_recon_mismatch_still_evaluates_the_drawdown(wired, caplog):
    db = _recon_db()
    wired['install'](db, 100_000.0, _recon_book())
    with caplog.at_level('INFO', logger=ab.logger.name):
        ab.run_once(session_date=SESSION)
    line = _last_line(caplog)
    assert 'rule=drawdown' in line and 'recon=1' in line and 'dd_pnl=-0.' in line


def test_armed_clean_recon_still_fires_the_drawdown(wired, caplog, monkeypatch, no_flatten):
    monkeypatch.setenv(ab.ARM_ENV, '1')
    db = FakeDB(peak_pnl=20_000.0, epoch_at=EPOCH,
                lots=[{'ticker': 'AAPL', 'qty': 100, 'avg_entry_price': 100, 'side': 'long'}])
    wired['install'](db, 100_000.0, {'AAPL': _pos(100, 100, 105), 'SPY': _pos(1, 1, 1, mv=1_000)})
    with caplog.at_level('INFO', logger=ab.logger.name):
        ab.run_once(session_date=SESSION)
    assert 'rule=drawdown' in _last_line(caplog) and 'recon=0' in _last_line(caplog)


# ── Task 2 / P3: asset-class scope ──────────────────────────────────────────

def test_load_broker_positions_carries_asset_class(monkeypatch):
    payload = [{'symbol': 'AAPL', 'qty': '1', 'side': 'long', 'market_value': '1',
                'asset_class': 'us_equity'},
               {'symbol': 'BTCUSD', 'qty': '1', 'side': 'long', 'market_value': '1',
                'asset_class': 'crypto'}]
    monkeypatch.setattr(rl, '_run_cli', lambda *a, **k: (True, payload, None))
    out = rl._load_broker_positions()
    assert out['AAPL']['asset_class'] == 'us_equity' and out['BTCUSD']['asset_class'] == 'crypto'


def _cls(p, cls):
    return dict(p, asset_class=cls)


def test_non_us_equity_class_is_excluded_from_both_legs_and_counted_by_class():
    # symbols look like plain equities: only the broker class marks them out
    book = {'AAPL': _cls(_pos(5, 100, 101), 'us_equity'),
            'XYZ': _cls(_pos(1, 50, 60, upl=10), 'crypto'),
            'ABC': _cls(_pos(1, 50, 60, upl=10), 'crypto')}
    fills = [_fill('XYZ', 'buy', 1, 50, 1), _fill('XYZ', 'sell', 1, 70, 2),
             _fill('AAPL', 'buy', 5, 100, 3)]
    r = ab.alpha_pnl(1, book, set(), fills, [])
    assert r['alpha_pnl'] == pytest.approx(5.0) and r['n_positions'] == 1
    assert r['realized'] == 0.0                         # XYZ's +20 never enters the ledger
    assert r['excluded'] == 2 and r['excluded_by_class'] == {'crypto': 2}


def test_us_equity_class_wins_over_symbol_shape():
    r = ab.alpha_pnl(1, {'BRK.B': _cls(_pos(1, 10, 12, upl=2), 'us_equity')}, set(), [], [])
    assert r['alpha_pnl'] == pytest.approx(2.0) and r['excluded'] == 0


def test_closed_position_fill_falls_back_to_the_shape_guess():
    fills = [_fill('ETH/USD', 'buy', 1, 100, 1), _fill('ETH/USD', 'sell', 1, 110, 2)]
    r = ab.alpha_pnl(1, {}, set(), fills, [])
    assert r['realized'] == 0.0 and r['excluded_by_class'] == {'crypto': 1}


def test_excluded_token_format():
    f = ab._excluded_token
    assert f({'excluded': 0}) == '0' and f({'excluded': 0, 'excluded_by_class': {}}) == '0'
    assert f({'excluded': 2, 'excluded_by_class': {'crypto': 2}}) == 'crypto:2'
    assert f({'excluded_by_class': {'us_option': 1, 'crypto': 2}}) == 'crypto:2,us_option:1'


def test_epoch_snapshot_excludes_non_us_equity_by_class():
    db = FakeDB()
    book = {'AAPL': _cls(_pos(100, 100, 105), 'us_equity'),
            'ABC': _cls(_pos(3, 10, 11), 'crypto')}
    assert ab.take_alpha_epoch(db, book, set()) is True
    assert [l['ticker'] for l in db.lots] == ['AAPL']


def test_flatten_scope_excludes_non_us_equity_by_class(monkeypatch):
    book = {'AAPL': _cls(_pos(10, 100, 101), 'us_equity'),
            'ABC': _cls(_pos(3, 10, 11), 'crypto')}
    r = ab.flatten_alpha(book, set(), cur=FakeDB(), live=False, rule='drawdown',
                         magnitude=-0.1, journal=False)
    assert r['tickers'] == ['AAPL']


# ── Task 2 / P4: legacy removal ─────────────────────────────────────────────

def test_shadow_tick_without_alpha_pnl_skips_drawdown_no_nav_fallback(wired, caplog):
    class Broken(FakeDB):
        def execute(self, sql, params=None):
            if 'alpha_epoch_at' in sql and sql.lstrip().startswith('SELECT'):
                raise RuntimeError('column does not exist')
            super().execute(sql, params)
    book = {'AAPL': _pos(100, 100, 105), 'SPY': _pos(1, 1, 1, mv=86_600)}
    wired['install'](Broken(peak_nav=14_500.0, epoch_at=EPOCH), 95_400.0, book)
    with caplog.at_level('INFO', logger=ab.logger.name):
        ab.run_once(session_date=SESSION)
    line = _last_line(caplog)
    assert 'rule=none' in line and 'dd_pnl=n/a' in line and 'dd_nav' not in line


def test_halted_and_rearm_paths_never_write_peak_alpha_nav(wired, monkeypatch):
    breached = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setenv(ab.REARM_ENV, breached.isoformat())
    db = FakeDB(peak_pnl=20_000.0, epoch_at=EPOCH, halted=True, breached_at=breached,
                lots=[{'ticker': 'AAPL', 'qty': 100, 'avg_entry_price': 100, 'side': 'long'}])
    wired['install'](db, 100_000.0, BOOK)
    ab.run_once(session_date=SESSION)
    assert db.peak_nav_writes == 0


# ── Task 2 fix round: MAJOR-1 HWM, MINOR-2 liveness, MINOR-3 pager ──────────

def _recon_db_low_peak(**kw):
    # alpha_pnl = 90 * 5 = 450 > stored peak 100, with recon=1 (ledger 100 vs broker 90)
    return FakeDB(peak_pnl=100.0, epoch_at=EPOCH,
                  lots=[{'ticker': 'AAPL', 'qty': 100, 'avg_entry_price': 100, 'side': 'long'}],
                  **kw)


def test_recon_mismatch_never_persists_a_hwm_shadow(wired, caplog):
    db = _recon_db_low_peak()
    wired['install'](db, 100_000.0, _recon_book())
    with caplog.at_level('INFO', logger=ab.logger.name):
        ab.run_once(session_date=SESSION)
    assert 'recon=1' in _last_line(caplog) and 'hwm=450.00' in _last_line(caplog)  # displayed
    assert db.sql('SET peak_alpha_pnl') == [] and db.peak_pnl == 100.0


def test_recon_mismatch_never_persists_a_hwm_armed(wired, monkeypatch, no_flatten):
    monkeypatch.setenv(ab.ARM_ENV, '1')
    db = _recon_db_low_peak()
    wired['install'](db, 100_000.0, _recon_book())
    ab.run_once(session_date=SESSION)
    assert db.sql('SET peak_alpha_pnl') == [] and db.peak_pnl == 100.0


def test_clean_recon_still_persists_the_hwm(wired):
    db = _recon_db_low_peak()
    wired['install'](db, 100_000.0, {'AAPL': _pos(100, 100, 105), 'SPY': _pos(1, 1, 1, mv=1_000)})
    ab.run_once(session_date=SESSION)
    assert db.peak_pnl == 500.0


BREACHED = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def notices(monkeypatch):
    got = []
    monkeypatch.setattr(ab, 'post_fallback_notice',
                        lambda now=None: got.append(1) or True)
    return got


def _rearm_updates(db):
    return db.sql('rearmed_at = NOW()')


def _untrusted_rearm_assertions(db, caplog, why):
    ups = _rearm_updates(db)
    assert len(ups) == 1 and 'peak_alpha_pnl = NULL' in ups[0][0] and not ups[0][1]
    assert db.halted is False and db.peak_pnl is None
    assert db.sql('SET peak_alpha_pnl') == []        # no separate HWM write, none persisted
    w = [r.getMessage() for r in caplog.records if r.levelname == 'WARNING'
         and 're-armed by operator token on an UNTRUSTED ledger' in r.getMessage()]
    assert len(w) == 1 and why in w[0] and 'hwm CLEARED' in w[0]
    assert not any('DEFERRED' in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize('armed', [False, True])
def test_rearm_on_a_recon_mismatch_tick_clears_latch_and_hwm(
        wired, monkeypatch, caplog, notices, no_flatten, armed):
    monkeypatch.setenv(ab.REARM_ENV, BREACHED.isoformat())
    if armed:
        monkeypatch.setenv(ab.ARM_ENV, '1')
    db = _recon_db_low_peak(halted=True, breached_at=BREACHED)
    wired['install'](db, 100_000.0, _recon_book())
    with caplog.at_level('INFO', logger=ab.logger.name):
        assert ab.run_once(session_date=SESSION) == 0
    _untrusted_rearm_assertions(db, caplog, 'recon=1')
    assert 'halted=0' in _last_line(caplog) and 'rule=none' in _last_line(caplog)
    assert no_flatten == []
    if armed:
        assert 'dd_pnl=n/a' in _last_line(caplog)     # drawdown skipped (P2)
    assert len(notices) == (1 if armed else 0)        # only the pre-existing armed P2 notice


@pytest.mark.parametrize('armed', [False, True])
def test_rearm_on_a_pnl_unavailable_tick_clears_latch_and_hwm(
        wired, monkeypatch, caplog, notices, no_flatten, armed):
    monkeypatch.setenv(ab.REARM_ENV, BREACHED.isoformat())
    if armed:
        monkeypatch.setenv(ab.ARM_ENV, '1')
    monkeypatch.setattr(ab, 'compute_alpha_pnl_tick', lambda *a, **k: None)
    db = _recon_db_low_peak(halted=True, breached_at=BREACHED)
    wired['install'](db, 100_000.0, BOOK)
    with caplog.at_level('INFO', logger=ab.logger.name):
        assert ab.run_once(session_date=SESSION) == 0
    _untrusted_rearm_assertions(db, caplog, 'pnl unavailable')
    assert 'halted=0' in _last_line(caplog) and 'dd_pnl=n/a' in _last_line(caplog)
    assert len(notices) == (1 if armed else 0)


@pytest.mark.parametrize('armed', [False, True])
def test_rearm_on_a_trusted_tick_sets_hwm_in_the_same_update(
        wired, monkeypatch, caplog, notices, armed):
    monkeypatch.setenv(ab.REARM_ENV, BREACHED.isoformat())
    if armed:
        monkeypatch.setenv(ab.ARM_ENV, '1')
    db = _recon_db_low_peak(halted=True, breached_at=BREACHED)
    wired['install'](db, 100_000.0,
                     {'AAPL': _pos(100, 100, 105), 'SPY': _pos(1, 1, 1, mv=1_000)})
    with caplog.at_level('INFO', logger=ab.logger.name):
        assert ab.run_once(session_date=SESSION) == 0
    ups = _rearm_updates(db)
    assert len(ups) == 1 and 'peak_alpha_pnl = %s' in ups[0][0] and ups[0][1] == (500.0,)
    assert db.halted is False and db.peak_pnl == 500.0
    assert any('alpha-P&L hwm reset to 500.00' in r.getMessage() for r in caplog.records)
    assert 'dd_pnl=0.0000' in _last_line(caplog) and notices == []


def test_distrusted_rearm_then_trusted_tick_seeds_hwm_and_does_not_relatch(
        wired, monkeypatch, caplog, notices, no_flatten):
    monkeypatch.setenv(ab.REARM_ENV, BREACHED.isoformat())
    monkeypatch.setenv(ab.ARM_ENV, '1')
    # pre-halt peak 20_000 is far above alpha_pnl 500: the old behaviour re-latched
    db = FakeDB(peak_pnl=20_000.0, epoch_at=EPOCH, halted=True, breached_at=BREACHED,
                lots=[{'ticker': 'AAPL', 'qty': 100, 'avg_entry_price': 100, 'side': 'long'}])
    wired['install'](db, 100_000.0, _recon_book())            # tick 1: recon=1
    with caplog.at_level('INFO', logger=ab.logger.name):
        ab.run_once(session_date=SESSION)
    assert db.halted is False and db.peak_pnl is None
    monkeypatch.delenv(ab.REARM_ENV)                           # operator removes the token
    wired['install'](db, 100_000.0,                            # tick 2: clean ledger
                     {'AAPL': _pos(100, 100, 105), 'SPY': _pos(1, 1, 1, mv=1_000)})
    with caplog.at_level('INFO', logger=ab.logger.name):
        ab.run_once(session_date=SESSION)
    assert db.peak_pnl == 500.0                                # seeded + persisted
    assert db.halted is False and no_flatten == []
    assert 'dd_pnl=0.0000' in _last_line(caplog) and 'breach=0' in _last_line(caplog)


@pytest.mark.parametrize('armed', [False, True])
def test_failed_clear_halt_write_keeps_the_latch_and_retries(
        wired, monkeypatch, caplog, notices, no_flatten, armed):
    monkeypatch.setenv(ab.REARM_ENV, BREACHED.isoformat())
    if armed:
        monkeypatch.setenv(ab.ARM_ENV, '1')
    db = _recon_db_low_peak(halted=True, breached_at=BREACHED)
    wired['install'](db, 100_000.0, _recon_book())
    real, fail = ab.clear_halt, [True]
    monkeypatch.setattr(ab, 'clear_halt',
                        lambda *a, **k: False if fail[0] else real(*a, **k))
    with caplog.at_level('INFO', logger=ab.logger.name):
        ab.run_once(session_date=SESSION)
    assert db.halted is True and db.peak_pnl == 100.0
    assert any('re-arm failed to persist' in r.getMessage() for r in caplog.records)
    assert 'halted=1' in _last_line(caplog)
    fail[0] = False                                            # next tick: write works
    wired['install'](db, 100_000.0, _recon_book())
    ab.run_once(session_date=SESSION)
    assert db.halted is False


def test_capped_tick_without_forward_progress_logs_an_error(monkeypatch, caplog):
    def capped(after, **k):
        raise ar.FillPagesTruncated('cap', [_act('c1', 'AAPL', 'buy', 1, 10, 5)])
    monkeypatch.setattr(ar, 'fetch_fills_since', capped)
    db = FakeDB(epoch_at=EPOCH)
    db.watermark = datetime(2026, 9, 29, 14, 12, tzinfo=timezone.utc)   # after = 14:02
    with caplog.at_level('ERROR', logger=ab.logger.name):
        assert ab.sync_fills_since_epoch(db, EPOCH) is False
    assert any('NO forward progress' in r.getMessage() for r in caplog.records)
    # a capped tick that DOES advance (new max filled_at 14:30 -> next start 14:20) is quiet
    caplog.clear()
    monkeypatch.setattr(ar, 'fetch_fills_since', lambda after, **k: (_ for _ in ()).throw(
        ar.FillPagesTruncated('cap', [_act('c2', 'AAPL', 'buy', 1, 10, 30)])))
    with caplog.at_level('ERROR', logger=ab.logger.name):
        ab.sync_fills_since_epoch(db, EPOCH)
    assert not any('NO forward progress' in r.getMessage() for r in caplog.records)


def test_non_list_or_empty_body_under_raise_on_cap_is_truncation_not_eof(monkeypatch):
    _cli(monkeypatch, [_page(100), '{"message": "rate limited"}'])
    with pytest.raises(ar.FillPagesTruncated) as ei:
        _REAL_FETCH_SINCE('2026-09-29T13:30:00Z', raise_on_cap=True)
    assert len(ei.value.fills) == 100
    _cli(monkeypatch, [_page(100), ''])
    with pytest.raises(ar.FillPagesTruncated):
        _REAL_FETCH_SINCE('2026-09-29T13:30:00Z', raise_on_cap=True)
    # default path unchanged: treated as end-of-data
    _cli(monkeypatch, [_page(100), ''])
    assert len(_REAL_FETCH_SINCE('2026-09-29T13:30:00Z')) == 100
