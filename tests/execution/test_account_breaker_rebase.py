"""Breaker alpha P&L Task 4 — per-ticker REBASE (spec C1 Amendment 2b).
Fake conn/cursor + monkeypatched broker only: no Postgres, no CLI, no network.
Tests are named after the spec's acceptance items 1-8."""
from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import account_breaker as ab            # noqa: E402
from execution import alpaca_reconcile as ar           # noqa: E402
from execution import stop_reattach as sr              # noqa: E402
from execution import alpaca_trader as at              # noqa: E402
from execution import regime_liquidator as rl          # noqa: E402

_REAL_FETCH_ACTIVITIES = ar.fetch_activities_since     # the autouse fixture stubs the module attr
_REAL_FETCH_SINCE = ar.fetch_fills_since
ESC = timedelta(seconds=ab.REBASE_ESCALATE_S)
UTC = timezone.utc
EPOCH = datetime(2026, 9, 29, 13, 30, tzinfo=UTC)
NOW = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)         # a Monday, 11:00 ET
SESSION = date(2026, 10, 5)


def _pos(qty, avg, cur, side='long', upl=None, **extra):
    p = {'qty': qty, 'side': side, 'avg_entry_price': avg, 'current_price': cur,
         'market_value': str(abs(qty) * cur)}
    if upl is not None:
        p['unrealized_pl'] = str(upl)
    p.update(extra)
    return p


def _epoch_row(t, qty, avg, side='long'):
    return {'kind': 'epoch', 'ticker': t, 'qty': qty, 'avg_entry_price': avg, 'side': side,
            'taken_at': EPOCH, 'realized_carry': 0, 'reason': None, 'activity_ref': None}


def _fill(t, side, q, px, when, aid):
    return {'ticker': t, 'side': side, 'qty': q, 'price': px, 'filled_at': when,
            'activity_id': aid}


def _split(aid, sym, day='2026-10-05', **kw):
    """A SPLIT activity; carries NO qty unless the test passes qty=... (an absent
    quantity is allowed for the auto types once the pair is stable)."""
    return {'id': aid, 'activity_type': 'SPLIT', 'symbol': sym, 'date': day,
            'net_amount': '0', **kw}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    """Hermetic broker + a controllable clock. `acts` is what the stubbed
    non-FILL activity lookup returns; `calls` records each lookup."""
    h = {'now': NOW, 'acts': [], 'calls': [], 'posts': [], 'raise': None, 'feed': []}
    monkeypatch.setattr(ar, 'fetch_fills_since', lambda after, **k: list(h['feed']))
    monkeypatch.setattr(ab, '_LAST_NEW_FILLS', [])
    monkeypatch.setattr(sr, 'fetch_recent_closed_orders', lambda *a, **k: (False, []))
    monkeypatch.setattr(ab, '_LAST_ACTIVITY_ID', None)
    monkeypatch.setattr(ab, '_utcnow', lambda: h['now'])

    def fake_acts(after, types, **k):
        h['calls'].append((after, tuple(types), k))
        if h['raise'] is not None:
            raise h['raise']
        return list(h['acts'])
    monkeypatch.setattr(ar, 'fetch_activities_since', fake_acts)
    monkeypatch.setattr(ab, '_post', lambda ch, msg: h['posts'].append((ch, msg)))
    return h


# ── fake DB ──────────────────────────────────────────────────────────────────

class FakeDB:
    """account_breaker_state / alpha_epoch (+162 columns) / broker_fills /
    recon_watch, keyed on SQL prefix. `missing_162` makes every migration-162
    statement raise (the savepoint guard must absorb it)."""

    def __init__(self, *, peak_pnl=1_500.0, epoch_at=EPOCH, lots=(), fills=(),
                 missing_162=False):
        self.peak_pnl, self.epoch_at = peak_pnl, epoch_at
        self.rows = [dict(r) for r in lots]
        self.fills = list(fills)
        self.missing_162 = missing_162
        self.watch = {}                 # ticker -> [first, last, notified, cleared, ledger_qty, broker_qty]
        self.calls, self._all = [], []
        self.rebase_inserts = 0
        self.rowcount = 1

    def execute(self, sql, params=None):
        f = ' '.join(sql.split())
        self.calls.append((f, params))
        self._all = []
        if f.startswith('SELECT peak_alpha_pnl, alpha_epoch_at'):
            self._all = [(self.peak_pnl, self.epoch_at)]
        elif f.startswith('SELECT last_synced_filled_at'):
            self._all = [(None,)]
        elif f.startswith('SELECT ticker, qty, avg_entry_price, side'):
            self._all = [(r['ticker'], r['qty'], r['avg_entry_price'], r['side'])
                         for r in self.rows]
        elif f.startswith('SELECT kind, ticker'):
            if self.missing_162:
                raise RuntimeError('column "kind" does not exist')
            self._all = [(r['kind'], r['ticker'], r['qty'], r['avg_entry_price'], r['side'],
                          r['taken_at'], r['realized_carry'], r['reason'], r['activity_ref'])
                         for r in sorted(self.rows, key=lambda r: r['taken_at'])]
        elif f.startswith('SELECT ticker, side, qty, price'):
            self._all = [(x['ticker'], x['side'], x['qty'], x['price'], x['filled_at'],
                          x['activity_id']) for x in self.fills
                         if x['filled_at'] >= params[0]]
        elif f.startswith('SELECT activity_id FROM broker_fills'):
            have = {x['activity_id'] for x in self.fills}
            self._all = [(i,) for i in params[0] if i in have]
        elif f.startswith('INSERT INTO account_breaker_alpha_epoch'):
            if self.missing_162:
                raise RuntimeError('column "kind" does not exist')
            t, q, avg, side, taken, kind, carry, reason, ref = params
            self.rebase_inserts += 1
            self.rows.append({'kind': kind, 'ticker': t, 'qty': q, 'avg_entry_price': avg,
                              'side': side, 'taken_at': taken, 'realized_carry': carry,
                              'reason': reason, 'activity_ref': ref})
        elif f.startswith('SELECT ticker, first_seen_at'):
            if self.missing_162:
                raise RuntimeError('relation "account_breaker_recon_watch" does not exist')
            self._all = [(t, *v) for t, v in self.watch.items()]
        elif f.startswith('INSERT INTO account_breaker_recon_watch') and 'late_fill_ref' in f:
            t, first, last, ref = params
            row = self.watch.setdefault(t, [first, last, None, None, None, None, None])
            row[6] = ref
        elif f.startswith('UPDATE account_breaker_recon_watch SET late_fill_ref = NULL'):
            self.watch[params[0]][6] = None
        elif f.startswith('INSERT INTO account_breaker_recon_watch'):
            self.watch_writes = getattr(self, 'watch_writes', 0) + 1
            t, first, last, notified, cleared, lq, bq = params
            old = self.watch.get(t)
            self.watch[t] = [first, last, notified, cleared, lq, bq, old[6] if old else None]
        elif f.startswith('INSERT INTO broker_fills'):
            if params[0] not in {x['activity_id'] for x in self.fills}:
                self.fills.append({'ticker': params[4], 'side': params[5], 'qty': params[8],
                                   'price': params[9], 'activity_id': params[0],
                                   'filled_at': datetime.fromisoformat(
                                       params[10].replace('Z', '+00:00'))})

    def fetchone(self):
        return self._all[0] if self._all else None

    def fetchall(self):
        return self._all

    def sql(self, needle):
        return [c for c in self.calls if needle in c[0]]

    def rebases(self):
        return [r for r in self.rows if r['kind'] == 'rebase']


class FakeConn:
    autocommit = False

    def __init__(self, cur):
        self._cur, self.committed, self.rolled_back = cur, 0, 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        pass


def _tick(db, positions, conn=None, equity=100_000.0, bench=('SPY',), t0=None):
    return ab.compute_alpha_pnl_tick(db, conn or FakeConn(db), equity, positions,
                                     set(bench), positions_at=t0)


def _two(db, book, env, gap=300, **kw):
    """Tick at the current clock (episode starts), advance `gap` s, tick again.
    Returns (first, second)."""
    r1 = _tick(db, book, **kw)
    env['now'] = env['now'] + timedelta(seconds=gap)
    return r1, _tick(db, book, **kw)


# Split scenario: epoch lot 100 AAPL @100; sold 20 @110 on 09-30 (realized +200);
# ledger 80 @100. The broker then splits 2-for-1: qty 160, avg 50, price 55 =>
# unrealized 800. alpha_pnl = 200 + 800 = 1000 before AND after a rebase.
SOLD = _fill('AAPL', 'sell', 20, 110, datetime(2026, 9, 30, 14, 0, tzinfo=UTC), 'f1')
SPLIT_BOOK = {'AAPL': _pos(160, 50, 55, upl=800), 'SPY': _pos(1, 1, 1)}


def _split_db(**kw):
    return FakeDB(lots=[_epoch_row('AAPL', 100, 100)], fills=[SOLD], **kw)


# ── acceptance 1: no rebase rows => ledger byte-identical ───────────────────

def test_acc1_no_rebase_rows_leaves_the_ledger_byte_identical():
    fills = [SOLD, _fill('AAPL', 'buy', 5, 90, datetime(2026, 10, 1, 14, 0, tzinfo=UTC), 'f2')]
    legacy = [{'ticker': 'AAPL', 'qty': 100, 'avg_entry_price': 100, 'side': 'long'}]
    tagged = [_epoch_row('AAPL', 100, 100)]
    book = {'AAPL': _pos(85, 100.0, 105, upl=425)}
    a = ab.alpha_pnl(1, book, set(), fills, legacy)
    b = ab.alpha_pnl(1, book, set(), fills, tagged)
    for k in ('alpha_pnl', 'realized', 'unrealized', 'unmatched', 'recon', 'excluded'):
        assert a[k] == b[k]
    assert a['recon'] == 0 and a['realized'] == pytest.approx(200.0)
    db = FakeDB(lots=tagged, fills=fills)
    res = _tick(db, {'AAPL': _pos(85, 100.0, 105, upl=425), 'SPY': _pos(1, 1, 1)})
    assert res['rebased'] == 0 and res['recon'] == 0 and db.rebase_inserts == 0


# ── acceptance 2: split => auto-rebase, recon 0 the same tick, alpha continuous ─

def test_acc2_split_auto_rebase_recon_zero_alpha_continuous_hwm_untouched(env):
    db = _split_db()
    env['acts'] = [_split('act1', 'AAPL', qty='80')]
    before = _tick(db, SPLIT_BOOK)                       # tick 1: episode starts
    assert before['recon'] == 1 and before['rebased'] == 0
    assert before['alpha_pnl'] == pytest.approx(1_000.0)
    assert db.rebase_inserts == 0 and env['calls'] == []  # pair not yet stable: no lookup
    env['now'] = NOW + timedelta(seconds=300)
    t0 = NOW + timedelta(seconds=299)
    conn = FakeConn(db)
    after = _tick(db, SPLIT_BOOK, conn, t0=t0)           # tick 2: stable, explained
    assert after['rebased'] == 1 and after['recon'] == 0
    assert round(after['alpha_pnl'], 2) == round(before['alpha_pnl'], 2) == 1_000.00
    assert after['realized'] == pytest.approx(200.0)    # carried, not re-earned
    assert after['hwm'] == before['hwm'] == 1_500.0 and db.peak_pnl == 1_500.0
    (row,) = db.rebases()
    assert (row['ticker'], row['qty'], row['avg_entry_price'], row['side']) == \
        ('AAPL', 160.0, 50.0, 'long')
    assert row['realized_carry'] == pytest.approx(200.0)
    assert row['reason'] == 'auto:SPLIT' and row['activity_ref'] == 'act1'
    assert row['taken_at'] == t0 and conn.committed == 1       # t0 = positions snapshot time
    assert not db.sql('alpha_epoch_at =') and not db.sql('SET peak_alpha_pnl')
    assert not db.sql('UPDATE account_breaker_state')
    n_calls = len(env['calls'])
    again = _tick(db, SPLIT_BOOK)
    assert again['recon'] == 0 and again['rebased'] == 0 and len(env['calls']) == n_calls
    assert again['alpha_pnl'] == pytest.approx(1_000.0)
    assert db.watch['AAPL'][3] is not None                # episode cleared, row kept


def test_acc2_two_tickers_one_lookup_argv_is_the_union_and_after_derives_from_first_seen(env):
    db = FakeDB(lots=[_epoch_row('AAPL', 100, 100), _epoch_row('MSFT', 10, 300)], fills=[])
    env['now'] = NOW + timedelta(days=10)                 # first_seen far from the epoch
    env['acts'] = [_split('a1', 'AAPL', day='2026-10-15'), _split('a2', 'MSFT', day='2026-10-15')]
    book = {'AAPL': _pos(200, 50, 55, upl=1_000), 'MSFT': _pos(30, 100, 110, upl=300),
            'SPY': _pos(1, 1, 1)}
    _r1, res = _two(db, book, env)
    assert res['rebased'] == 2 and res['recon'] == 0
    assert len(env['calls']) == 1                         # ONE lookup for both tickers
    after, types, kw = env['calls'][0]
    assert after == '2026-10-12'                          # ET date(first_seen - 3d), not the epoch
    assert types == ab.REBASE_AUTO_ACTIVITY_TYPES + ab.REBASE_OPERATOR_ACTIVITY_TYPES
    assert kw['raise_on_cap'] is True and kw['max_pages'] == ab.REBASE_ACTIVITY_MAX_PAGES
    assert kw['deadline_s'] == ab.REBASE_ACTIVITY_BUDGET_S


def test_activity_type_lists_are_the_live_verified_ones():
    assert ab.REBASE_AUTO_ACTIVITY_TYPES == ('SPLIT', 'SPIN', 'SC', 'NC', 'MA', 'REORG')
    assert ab.REBASE_OPERATOR_ACTIVITY_TYPES == ('OPASN', 'OPEXC', 'JNLS', 'ACATS')
    assert 'OPEXP' not in ab.REBASE_ACTIVITY_TYPES                 # expiry never moves shares
    assert {'SSP', 'SSO', 'OPXRC'}.isdisjoint(ab.REBASE_ACTIVITY_TYPES)   # HTTP 400 live


# ── acceptance 3: symbol change ─────────────────────────────────────────────

def test_acc3_symbol_change_old_key_to_zero_new_key_to_broker_lot(env):
    db = FakeDB(lots=[_epoch_row('OLDT', 60, 20)],
                fills=[_fill('OLDT', 'sell', 10, 25, datetime(2026, 9, 30, 14, 0, tzinfo=UTC), 'g1')])
    book = {'NEWT': _pos(50, 20, 22, upl=100), 'SPY': _pos(1, 1, 1)}
    env['acts'] = [{'id': 'sc1', 'activity_type': 'SC', 'symbol': 'NEWT', 'old_symbol': 'OLDT',
                    'qty': '50', 'date': '2026-10-05'}]
    pre, res = _two(db, book, env)
    assert pre['recon'] == 2 and pre['alpha_pnl'] == pytest.approx(150.0)
    assert res['rebased'] == 2 and res['recon'] == 0
    assert round(res['alpha_pnl'], 2) == 150.00
    rows = {r['ticker']: r for r in db.rebases()}
    assert (rows['OLDT']['qty'], rows['OLDT']['avg_entry_price'], rows['OLDT']['side']) == (0.0, None, None)
    assert rows['OLDT']['realized_carry'] == pytest.approx(50.0)
    assert (rows['NEWT']['qty'], rows['NEWT']['avg_entry_price']) == (50.0, 20.0)
    assert rows['NEWT']['realized_carry'] == 0.0 and rows['OLDT']['reason'] == 'auto:SC'


def test_activity_classification_is_conservative():
    c = ab._classify_activity
    assert c({'activity_type': 'SPLIT', 'symbol': 'AAPL'}, 'AAPL') == ('structured', 'symbol')
    assert c({'activity_type': 'SPLIT', 'symbol': 'MSFT'}, 'AAPL') is None
    assert c({'activity_type': 'DIV', 'symbol': 'AAPL'}, 'AAPL') is None
    assert c({'activity_type': 'SSP', 'symbol': 'AAPL'}, 'AAPL') is None          # not a real code
    assert c({'activity_type': 'OPASN', 'symbol': 'AAPL261016C00100000'}, 'AAPL') == ('structured', 'occ')
    assert c({'activity_type': 'SPLIT', 'symbol': 'AAPL261016C00100000'}, 'AAPL') is None
    assert c({'activity_type': 'SC', 'symbol': 'N', 'old_symbol': 'OLDT'}, 'OLDT') == ('structured', 'related_old')
    assert c({'activity_type': 'SC', 'symbol': 'N', 'new_symbol': 'NEWT'}, 'NEWT') == ('structured', 'related_new')
    assert c({'activity_type': 'MA', 'symbol': 'XYZ', 'description': 'MERGER OLDT INTO XYZ'}, 'OLDT') \
        == ('description', 'description')
    assert c({'activity_type': 'SPLIT', 'description': 'OLDT'}, 'OLDT') is None     # SC-like types only
    assert c({'activity_type': 'MA', 'symbol': 'XYZ', 'description': 'MERGER OLDTX'}, 'OLDT') is None
    assert c({'activity_type': 'MA', 'symbol': 'XYZ', 'description': 'MERGER ON ABC'}, 'ON') is None


def test_quantity_consistency_rule():
    q = ab._quantity_consistent
    assert q({'qty': '80'}, 'symbol', 'SPLIT', 80, 160)              # == delta
    assert q({'qty': '160'}, 'symbol', 'SPLIT', 80, 160)             # == resulting position, ratio 2:1
    assert not q({'qty': '1000'}, 'symbol', 'SPLIT', 80, 160)
    assert not q({'qty': '-80'}, 'symbol', 'SPLIT', 80, 160)         # sign flip only for SC-like
    assert q({'qty': '-80'}, 'symbol', 'MA', 80, 160)
    assert q({}, 'symbol', 'SPLIT', 80, 160) and q({'qty': 'x'}, 'symbol', 'SPLIT', 80, 160)
    assert q({'qty': '0'}, 'symbol', 'SPLIT', 80, 160)               # absent + plausible ratio => allowed
    assert q({'qty': '999'}, 'related_new', 'SC', 0, 50)             # other symbol's qty: not tested
    assert q({'qty': '999'}, 'related_old', 'SC', 50, 0)
    assert not q({}, 'related_old', 'SC', 50, 10)                    # old key must be gone


def test_one_activity_never_explains_a_later_mismatch_on_the_same_ticker(env):
    db = _split_db()
    env['acts'] = [_split('act1', 'AAPL')]
    _two(db, SPLIT_BOOK, env)
    assert db.rebase_inserts == 1
    drift = {'AAPL': _pos(170, 50, 55, upl=850), 'SPY': _pos(1, 1, 1)}
    env['now'] += timedelta(hours=1)
    _r, res = _two(db, drift, env)
    assert res['rebased'] == 0 and res['recon'] == 1 and db.rebase_inserts == 1


# ── acceptance 4: unexplained mismatch ──────────────────────────────────────

def test_acc4_unexplained_mismatch_notice_after_window_and_operator_command_repairs(env):
    db = _split_db()
    res = _tick(db, SPLIT_BOOK)
    assert res['rebased'] == 0 and res['recon'] == 1 and db.rebase_inserts == 0
    assert db.watch['AAPL'] == [NOW, NOW, None, None, 80.0, 160.0, None]
    assert env['posts'] == []
    env['now'] = NOW + timedelta(seconds=ab.REBASE_ESCALATE_S - 60)
    _tick(db, SPLIT_BOOK)
    assert env['posts'] == [] and db.watch['AAPL'][0] == NOW         # first_seen kept
    assert db.watch['AAPL'][1] == env['now']                         # last_seen advanced
    env['now'] = NOW + timedelta(seconds=ab.REBASE_ESCALATE_S + 1)
    conn = FakeConn(db)
    _tick(db, SPLIT_BOOK, conn)
    (ch, msg), = env['posts']
    assert ch == 'trade-reports' and 'AAPL' in msg
    assert 'ledger 80' in msg and 'broker 160' in msg
    assert 'No auto-rebasable corporate-action activity explains it' in msg
    assert 'account_breaker.py --rebase AAPL' in msg and '--apply' in msg
    assert db.watch['AAPL'][2] == env['now'] and conn.committed == 1
    env['now'] += timedelta(minutes=5)
    _tick(db, SPLIT_BOOK)
    assert len(env['posts']) == 1
    env['now'] += timedelta(seconds=ab.REBASE_ESCALATE_S)
    _tick(db, SPLIT_BOOK)
    assert len(env['posts']) == 2
    out = []
    rc = ab.rebase_cli(db, FakeConn(db), SPLIT_BOOK, {'SPY'}, ['AAPL'], 'confirmed split', True,
                       now=env['now'], out=out.append)
    assert rc == 0 and db.rebase_inserts == 1
    assert any('watch episode first seen' in o and 'age' in o for o in out)
    assert db.rebases()[0]['reason'] == 'operator:confirmed split'
    env['now'] += timedelta(minutes=5)
    res = _tick(db, SPLIT_BOOK)
    assert res['recon'] == 0 and res['alpha_pnl'] == pytest.approx(1_000.0)
    assert db.watch['AAPL'][3] == env['now']                         # cleared, row kept
    env['now'] += timedelta(hours=2)
    _tick(db, {'AAPL': _pos(170, 50, 55, upl=850), 'SPY': _pos(1, 1, 1)})
    assert db.watch['AAPL'] == [env['now'], env['now'], None, None, 160.0, 170.0, None]


# ── MAJOR-1: lagged fills / stable pair ─────────────────────────────────────

def test_lagged_closing_fill_not_yet_ingested_blocks_the_first_tick_then_restarts(env):
    db = _split_db()
    env['acts'] = [_split('act1', 'AAPL')]
    r1 = _tick(db, SPLIT_BOOK)                           # pair (80, 160) first seen
    assert r1['rebased'] == 0 and env['calls'] == []
    # the lagged fill (filled long ago, ingested only now) lands: ledger 80 -> 60
    db.fills.append(_fill('AAPL', 'sell', 20, 110, NOW - timedelta(minutes=10), 'lag1'))
    env['now'] = NOW + timedelta(seconds=300)
    r2 = _tick(db, SPLIT_BOOK)
    assert r2['rebased'] == 0 and db.rebase_inserts == 0 and env['calls'] == []
    assert db.watch['AAPL'][0] == env['now'] and db.watch['AAPL'][4] == 60.0   # episode restarted
    env['now'] += timedelta(seconds=300)
    r3 = _tick(db, SPLIT_BOOK)                           # stable pair (60, 160) — but 8:3 is not a
    # split ratio (R2 whitelist), so the no-qty SPLIT activity does NOT explain it: a mismatch that
    # absorbed a lagged fill is left to the operator rather than auto-rebased.
    assert r3['rebased'] == 0 and r3['recon'] == 1 and db.rebase_inserts == 0


def test_fill_landing_between_two_ticks_changes_the_pair_and_restarts(env):
    db = _split_db()
    env['acts'] = [_split('act1', 'AAPL')]
    _tick(db, SPLIT_BOOK)
    first = db.watch['AAPL'][0]
    db.fills.append(_fill('AAPL', 'buy', 5, 100, NOW + timedelta(seconds=10), 'mid'))
    env['now'] = NOW + timedelta(seconds=400)
    res = _tick(db, SPLIT_BOOK)
    assert res['rebased'] == 0 and db.watch['AAPL'][0] == env['now'] != first
    assert db.watch['AAPL'][2] is None                        # notified_at reset


def test_acc5_mismatch_inside_the_ingested_fill_quiet_window_is_not_rebased(env):
    db = _split_db()
    env['acts'] = [_split('act1', 'AAPL')]
    _tick(db, SPLIT_BOOK)                                 # episode starts at NOW
    # a net-zero pair of fills 20 s before the next tick: pair unchanged, window not quiet
    t = NOW + timedelta(seconds=280)
    db.fills += [_fill('AAPL', 'buy', 1, 100, t, 'n1'), _fill('AAPL', 'sell', 1, 100, t, 'n2')]
    env['now'] = NOW + timedelta(seconds=300)
    res = _tick(db, SPLIT_BOOK)
    assert res['rebased'] == 0 and res['recon'] == 1 and env['calls'] == []
    env['now'] = NOW + timedelta(seconds=420)
    assert _tick(db, SPLIT_BOOK)['rebased'] == 1 and len(env['calls']) == 1


def test_activity_lookup_not_issued_when_nothing_is_mismatched(env):
    db = FakeDB(lots=[_epoch_row('AAPL', 80, 100)], fills=[])
    res = _tick(db, {'AAPL': _pos(80, 100, 105, upl=400), 'SPY': _pos(1, 1, 1)})
    assert res['recon'] == 0 and env['calls'] == []


# ── MAJOR-2: how loosely an activity may explain ────────────────────────────

def test_old_activity_before_first_seen_minus_3_days_does_not_explain(env):
    db = _split_db()
    env['acts'] = [_split('old1', 'AAPL', day='2026-09-20')]
    _r, res = _two(db, SPLIT_BOOK, env)
    assert len(env['calls']) == 1 and res['rebased'] == 0 and res['recon'] == 1


def test_description_only_match_never_auto_rebases_but_is_named_in_the_notice(env):
    db = FakeDB(lots=[_epoch_row('ALL', 10, 50)], fills=[])
    book = {'ALL': _pos(20, 25, 26, upl=20), 'SPY': _pos(1, 1, 1)}
    env['acts'] = [{'id': 'm1', 'activity_type': 'MA', 'symbol': 'XYZ', 'date': '2026-10-05',
                    'description': 'MERGER OF BALL CORP INTO XYZ - ALL SHARES CONVERTED'}]
    _tick(db, book)
    env['now'] = NOW + ESC + timedelta(seconds=1)
    res = _tick(db, book)
    assert res['rebased'] == 0 and db.rebase_inserts == 0
    (_ch, msg), = env['posts']
    assert 'ALL' in msg and 'possible cause MA 2026-10-05 (description mention only)' in msg


def test_quantity_inconsistent_split_is_not_explained(env):
    db = _split_db()
    env['acts'] = [_split('act1', 'AAPL', qty='1000')]
    _tick(db, SPLIT_BOOK)
    env['now'] = NOW + ESC + timedelta(seconds=1)
    res = _tick(db, SPLIT_BOOK)
    assert res['rebased'] == 0 and res['recon'] == 1
    assert 'SPLIT 2026-10-05 (quantity inconsistent)' in env['posts'][0][1]


def test_operator_types_never_auto_rebase_and_are_named_in_the_notice(env):
    db = FakeDB(lots=[_epoch_row('AAPL', 100, 100), _epoch_row('MSFT', 10, 300)], fills=[])
    book = {'AAPL': _pos(0, 100, 105), 'MSFT': _pos(30, 100, 110, upl=300), 'SPY': _pos(1, 1, 1)}
    env['acts'] = [{'id': 'o1', 'activity_type': 'OPASN', 'symbol': 'AAPL261016C00100000',
                    'date': '2026-10-05'},
                   {'id': 'j1', 'activity_type': 'JNLS', 'symbol': 'MSFT', 'date': '2026-10-05',
                    'qty': '20'}]
    _tick(db, book)
    env['now'] = NOW + ESC + timedelta(seconds=1)
    res = _tick(db, book)
    assert res['rebased'] == 0 and db.rebase_inserts == 0 and res['recon'] == 2
    (_ch, msg), = env['posts']
    assert 'AAPL: possible cause OPASN 2026-10-05' in msg
    assert 'MSFT: possible cause JNLS 2026-10-05' in msg


def test_lookup_failure_notice_says_the_lookup_is_failing(env):
    db = _split_db()
    env['raise'] = RuntimeError('alpaca activity list failed: HTTP 400 invalid activity type')
    _tick(db, SPLIT_BOOK)
    env['now'] = NOW + ESC + timedelta(seconds=1)
    res = _tick(db, SPLIT_BOOK)
    assert res['rebased'] == 0
    (_ch, msg), = env['posts']
    assert 'activity lookup is failing (' in msg and 'HTTP 400' in msg
    assert 'No auto-rebasable' not in msg


# ── acceptance 6: lookup failure / cap / timeout => no rebase, no crash ─────

@pytest.mark.parametrize('exc', [RuntimeError('cli down'),
                                 ar.FillPagesTruncated('hit max_pages=5', []),
                                 TimeoutError('deadline')])
def test_acc6_lookup_failure_cap_or_timeout_means_no_rebase_no_crash(env, exc):
    db = _split_db()
    env['acts'] = [_split('act1', 'AAPL')]
    env['raise'] = exc
    _r, res = _two(db, SPLIT_BOOK, env)
    assert res is not None and res['rebased'] == 0 and res['recon'] == 1
    assert db.rebase_inserts == 0
    env['raise'] = None                                  # retried on the next tick
    env['now'] += timedelta(seconds=300)
    assert _tick(db, SPLIT_BOOK)['rebased'] == 1


def test_acc6_real_pager_cap_and_argv(monkeypatch):
    seen = []

    class Proc:
        returncode, stderr = 0, ''

        def __init__(self, out):
            self.stdout = out

    def fake_run(args, **k):
        seen.append(args)
        return Proc('[' + ','.join('{"id": "x%d"}' % (len(seen) * 10 + i) for i in range(2)) + ']')
    monkeypatch.setattr(ar.subprocess, 'run', fake_run)
    with pytest.raises(ar.FillPagesTruncated):             # full pages forever => cap
        _REAL_FETCH_ACTIVITIES('2026-10-04', ab.REBASE_ACTIVITY_TYPES, page_size=2, max_pages=3)
    a = seen[0]
    assert a[1:4] == ['account', 'activity', 'list']
    assert a[a.index('--activity-types') + 1] == 'SPLIT,SPIN,SC,NC,MA,REORG,OPASN,OPEXC,JNLS,ACATS'
    assert a[a.index('--after') + 1] == '2026-10-04' and a[a.index('--direction') + 1] == 'asc'
    assert a[a.index('--page-size') + 1] == '2' and '--page-token' not in a
    assert '--page-token' in seen[1]
    seen.clear()
    monkeypatch.setattr(ar.subprocess, 'run',
                        lambda args, **k: seen.append(args) or Proc('[]'))
    _REAL_FETCH_SINCE('2026-10-04T00:00:00Z')
    ar.fetch_fills_for_date('2026-10-04')
    assert [a[a.index('--activity-types') + 1] for a in seen] == ['FILL', 'FILL']


def test_fetch_rebase_activities_returns_reason_on_failure_and_drops_fills(env):
    env['raise'] = RuntimeError('boom')
    assert ab.fetch_rebase_activities(EPOCH) == (None, 'boom')
    env['raise'] = ar.FillPagesTruncated('cap', [])
    assert ab.fetch_rebase_activities(EPOCH) == (None, 'page cap / budget hit')
    env['raise'] = None
    env['acts'] = [{'id': 'f', 'activity_type': 'FILL'}, _split('s', 'AAPL'), 'junk']
    acts, err = ab.fetch_rebase_activities(EPOCH)
    assert err is None and [a['id'] for a in acts] == ['s']


# ── acceptance 7: rebase after the position is closed ───────────────────────

def test_acc7_rebase_after_the_position_is_closed(env):
    # ledger AAPL 80; the broker holds none (cash acquisition, no FILL).
    db = _split_db()
    book = {'MSFT': _pos(5, 10, 11, upl=5), 'SPY': _pos(1, 1, 1)}
    env['acts'] = [{'id': 'ma1', 'activity_type': 'MA', 'symbol': 'AAPL', 'qty': '-80',
                    'date': '2026-10-05'}]
    pre, res = _two(db, book, env)
    assert pre['recon'] == 2 and pre['alpha_pnl'] == pytest.approx(205.0)   # AAPL + MSFT (no lot row)
    assert res['rebased'] == 1 and res['recon'] == 1      # MSFT has no lot row: still flagged
    (row,) = db.rebases()
    assert (row['ticker'], row['qty'], row['avg_entry_price'], row['side']) == ('AAPL', 0.0, None, None)
    assert row['realized_carry'] == pytest.approx(200.0) and row['reason'] == 'auto:MA'
    assert round(res['alpha_pnl'], 2) == round(pre['alpha_pnl'], 2)
    assert 'AAPL' not in res['mismatch']


# ── acceptance 8: migration 162 strictly additive ───────────────────────────

def test_acc8_migration_162_is_strictly_additive():
    sql = (ROOT / 'src/database/migrations/162_account_breaker_rebase.sql').read_text()
    code = '\n'.join(re.sub(r'--.*$', '', ln) for ln in sql.splitlines())
    assert not re.search(r'\b(DELETE|DROP|TRUNCATE|RENAME|UPDATE)\b', code, re.I)
    assert len(re.findall(r'ADD COLUMN IF NOT EXISTS', code)) == 4
    for col in ('kind', 'realized_carry', 'reason', 'activity_ref'):
        assert re.search(rf'ADD COLUMN IF NOT EXISTS {col}\b', code)
    norm = re.sub(r'\s+', ' ', code)
    assert "kind TEXT NOT NULL DEFAULT 'epoch'" in norm
    assert 'realized_carry NUMERIC NOT NULL DEFAULT 0' in norm
    assert 'CREATE TABLE IF NOT EXISTS account_breaker_recon_watch' in norm
    for col in ('ticker TEXT PRIMARY KEY', 'first_seen_at TIMESTAMPTZ NOT NULL',
                'last_seen_at TIMESTAMPTZ NOT NULL', 'notified_at TIMESTAMPTZ',
                'cleared_at TIMESTAMPTZ', 'ledger_qty NUMERIC,', 'broker_qty NUMERIC,',
                'late_fill_ref TEXT'):
        assert col in norm
    stmts = [s.strip() for s in code.split(';') if s.strip()]
    assert all(s.startswith(('ALTER TABLE account_breaker_alpha_epoch', 'CREATE TABLE IF NOT EXISTS'))
               for s in stmts)


# ── per-ticker fill cutoff, realized carry ──────────────────────────────────

def test_per_ticker_cutoff_ignores_earlier_fills_but_not_another_tickers_same_time_fill():
    t = datetime(2026, 10, 5, 14, 30, tzinfo=UTC)
    rows = [_epoch_row('AAPL', 100, 100), _epoch_row('MSFT', 0, None),
            {'kind': 'rebase', 'ticker': 'AAPL', 'qty': 10, 'avg_entry_price': 50.0,
             'side': 'long', 'taken_at': t, 'realized_carry': 5.0, 'reason': 'auto:SPLIT',
             'activity_ref': 'a'}]
    fills = [_fill('AAPL', 'buy', 5, 40, t - timedelta(minutes=1), 'x1'),
             _fill('AAPL', 'buy', 7, 41, t, 'x2'),
             _fill('MSFT', 'buy', 3, 10, t, 'x3'),
             _fill('AAPL', 'sell', 4, 60, t + timedelta(minutes=1), 'x4')]
    book = {'AAPL': _pos(6, 50, 60, upl=60), 'MSFT': _pos(3, 10, 10, upl=0)}
    r = ab.alpha_pnl(1, book, set(), fills, rows)
    assert r['recon'] == 0 and r['mismatch'] == {}
    assert r['realized_by_key']['AAPL'] == pytest.approx(5.0 + 40.0)
    assert r['realized'] == pytest.approx(45.0) and r['unmatched'] == 0
    assert ab.alpha_pnl(1, {'AAPL': _pos(110, 100, 100)}, set(), [], rows)['recon'] == 1


def test_two_successive_rebases_carry_realized_correctly(env):
    db = _split_db()
    env['acts'] = [_split('act1', 'AAPL')]
    _r, r1 = _two(db, SPLIT_BOOK, env)
    assert r1['rebased'] == 1 and db.rebases()[0]['realized_carry'] == pytest.approx(200.0)
    env['now'] = NOW + timedelta(days=1)
    db.fills.append(_fill('AAPL', 'sell', 10, 70, NOW + timedelta(hours=1), 'f9'))
    env['acts'] = [_split('act1', 'AAPL'), _split('act2', 'AAPL', day='2026-10-06')]
    book = {'AAPL': _pos(450, 50 / 3, 25, upl=450 * (25 - 50 / 3)), 'SPY': _pos(1, 1, 1)}
    pre = ab.alpha_pnl(0, book, {'SPY'}, db.fills, [r for r in db.rows])
    assert pre['recon'] == 1
    _r, r2 = _two(db, book, env)
    assert r2['rebased'] == 1 and r2['recon'] == 0
    row2 = db.rebases()[1]
    assert row2['realized_carry'] == pytest.approx(400.0)
    assert row2['activity_ref'] == 'act2'
    assert round(r2['alpha_pnl'], 2) == round(pre['alpha_pnl'], 2)
    assert r2['realized'] == pytest.approx(400.0)


# ── missing migration 162 => behave exactly as today ────────────────────────

def test_missing_migration_162_skips_the_repair_entirely(env):
    db = _split_db(missing_162=True)
    env['acts'] = [_split('act1', 'AAPL')]
    r1, res = _two(db, SPLIT_BOOK, env)
    assert res is not None and res['rebased'] == 0 and res['recon'] == 1
    assert res['alpha_pnl'] == pytest.approx(1_000.0)
    assert db.rebase_inserts == 0 and env['calls'] == []        # no activity fetch
    assert not db.sql('account_breaker_recon_watch') and db.watch == {}   # no watch read/write
    lots, fills = ab.load_alpha_inputs(db, EPOCH)
    assert lots == [{'ticker': 'AAPL', 'qty': 100, 'avg_entry_price': 100, 'side': 'long'}]
    assert ab.load_alpha_inputs(db, EPOCH).has_162 is False
    db2 = FakeDB(lots=[_epoch_row('AAPL', 100, 100), _epoch_row('AAPL', 160, 50)],
                 missing_162=True)
    assert ab.load_alpha_inputs(db2, EPOCH) is None
    assert _tick(db2, SPLIT_BOOK) is None


# ── operator CLI ────────────────────────────────────────────────────────────

def test_cli_dry_run_writes_nothing_and_prints_both_quantities_and_pnl(env):
    db = _split_db()
    conn = FakeConn(db)
    out = []
    rc = ab.rebase_cli(db, conn, SPLIT_BOOK, {'SPY'}, ['AAPL'], 'split', False,
                       now=NOW, out=out.append)
    text = '\n'.join(out)
    assert rc == 0 and db.rebase_inserts == 0 and conn.committed == 0 and conn.rolled_back >= 1
    assert 'ledger qty 80' in text and 'broker qty 160' in text
    assert 'qty=160' in text and 'avg=50.0' in text and "reason='operator:split'" in text
    assert 'before 1000.00 | after 1000.00' in text and 'DRY RUN' in text
    assert not db.sql('alpha_epoch_at =') and not db.sql('SET peak_alpha_pnl')


def test_cli_apply_writes_exactly_one_row_per_ticker_taken_at_t0(env):
    db = FakeDB(lots=[_epoch_row('AAPL', 100, 100), _epoch_row('MSFT', 10, 300)], fills=[])
    book = {'AAPL': _pos(200, 50, 55, upl=1_000), 'MSFT': _pos(30, 100, 110, upl=300),
            'SPY': _pos(1, 1, 1)}
    conn = FakeConn(db)
    t0 = NOW - timedelta(seconds=7)
    rc = ab.rebase_cli(db, conn, book, {'SPY'}, ['AAPL', 'MSFT'], 'two splits', True,
                       now=NOW, positions_at=t0, out=lambda s: None)
    assert rc == 0 and db.rebase_inserts == 2 and conn.committed == 1
    assert {r['ticker'] for r in db.rebases()} == {'AAPL', 'MSFT'}
    assert all(r['reason'] == 'operator:two splits' and r['kind'] == 'rebase'
               and r['taken_at'] == t0 for r in db.rebases())
    assert db.epoch_at == EPOCH and db.peak_pnl == 1_500.0


@pytest.mark.parametrize('ticker, book_extra, fills_extra, why', [
    ('MSFT', {'MSFT': _pos(10, 300, 310, upl=100)}, [], 'reconciles'),
    ('SPY', {}, [], 'benchmark'),
    ('BTCUSD', {'BTCUSD': _pos(1, 10, 11, asset_class='crypto')}, [], 'us_equity'),
    ('AAPL', {}, [_fill('AAPL', 'sell', 1, 110, NOW - timedelta(seconds=30), 'q1')], 'quiet'),
])
def test_cli_refusals_write_nothing(env, ticker, book_extra, fills_extra, why):
    db = FakeDB(lots=[_epoch_row('AAPL', 100, 100), _epoch_row('MSFT', 10, 300)],
                fills=[SOLD] + fills_extra)
    book = {**SPLIT_BOOK, **book_extra}
    out = []
    rc = ab.rebase_cli(db, FakeConn(db), book, {'SPY'}, [ticker], 'x', True,
                       now=NOW, out=out.append)
    assert rc == 2 and db.rebase_inserts == 0
    assert why in '\n'.join(out)


def test_cli_one_refused_ticker_refuses_the_whole_command(env):
    db = FakeDB(lots=[_epoch_row('AAPL', 100, 100), _epoch_row('MSFT', 10, 300)], fills=[SOLD])
    book = {**SPLIT_BOOK, 'MSFT': _pos(10, 300, 310, upl=100)}
    rc = ab.rebase_cli(db, FakeConn(db), book, {'SPY'}, ['AAPL', 'MSFT'], 'x', True,
                       now=NOW, out=lambda s: None)
    assert rc == 2 and db.rebase_inserts == 0


def test_cli_refuses_when_positions_unavailable_or_fill_sync_fails(env, monkeypatch):
    db = _split_db()
    assert ab.rebase_cli(db, FakeConn(db), {}, {'SPY'}, ['AAPL'], 'x', True,
                         now=NOW, out=lambda s: None) == 2
    monkeypatch.setattr(ar, 'fetch_fills_since',
                        lambda after, **k: (_ for _ in ()).throw(RuntimeError('down')))
    assert ab.rebase_cli(db, FakeConn(db), SPLIT_BOOK, {'SPY'}, ['AAPL'], 'x', True,
                         now=NOW, out=lambda s: None) == 2
    assert db.rebase_inserts == 0


def test_main_routes_rebase_without_running_a_breaker_tick(monkeypatch):
    ticks, cli = [], []
    monkeypatch.setattr(ab, 'run_once', lambda *a, **k: ticks.append(1) or 0)
    monkeypatch.setattr(ab, '_run_rebase_cli', lambda t, r, a, adj=0.0: cli.append((t, r, a, adj)) or 0)
    assert ab.main([]) == 0 and ticks == [1] and cli == []
    ticks.clear()
    assert ab.main(['--rebase', 'aapl, msft', '--reason', 'why']) == 0
    assert cli == [(['AAPL', 'MSFT'], 'why', False, 0.0)] and ticks == []
    assert ab.main(['--rebase', 'AAPL', '--apply']) == 0 and cli[-1] == (['AAPL'], None, True, 0.0)
    assert ab.main(['--rebase', 'AAPL', '--realized-adjust', '12.5']) == 0 and cli[-1][3] == 12.5
    with pytest.raises(SystemExit):
        ab.main(['--apply'])


# ── shadow line token + run_once, both modes ────────────────────────────────

def test_rebased_token_in_format_line():
    st = {'peak': None, 'dd': 0.0, 'daily': 0.0, 'rule': 'none', 'breach': False}
    kw = dict(equity=1.0, bench_mv=0.0, alpha=1.0, st=st, open_equity=1.0,
              open_src='x', halted=False)
    base = {'alpha_pnl': 1.0, 'realized': 0.0, 'unrealized': 1.0, 'hwm': 1.0,
            'dd_pnl': 0.0, 'unmatched': 0, 'recon': 0, 'excluded': 0}
    assert ab.format_line('shadow', pnl=base, **kw).endswith('excluded=0 rebased=0')
    assert ab.format_line('shadow', pnl={**base, 'rebased': 2}, **kw).endswith('excluded=0 rebased=2')


@pytest.mark.parametrize('armed', [False, True])
def test_run_once_rebases_in_both_modes_threads_t0_and_armed_drawdown_is_not_skipped(
        env, monkeypatch, tmp_path, caplog, armed):
    monkeypatch.setenv('POSTGRES_URI', 'postgres://stub')
    monkeypatch.setenv(ab.NAV_OHLC_PATH_ENV, str(tmp_path / 'missing.json'))
    monkeypatch.setenv(ab.FALLBACK_POST_PATH_ENV, str(tmp_path / 'fb.ts'))
    monkeypatch.delenv(ab.REARM_ENV, raising=False)
    if armed:
        monkeypatch.setenv(ab.ARM_ENV, '1')
    else:
        monkeypatch.delenv(ab.ARM_ENV, raising=False)
    monkeypatch.setattr(ab, '_SETTLE_S', 0)
    monkeypatch.setattr(rl, '_market_is_open', lambda: True)
    monkeypatch.setattr(rl, '_load_open_orders', lambda: [])
    monkeypatch.setattr(at, '_alpaca_session', lambda: object())
    monkeypatch.setattr(ab, 'bench_tickers', lambda cur: {'SPY'})
    monkeypatch.setattr(at, '_fetch_account_state', lambda s: {'equity': 100_000.0})
    monkeypatch.setattr(rl, '_load_broker_positions', lambda: dict(SPLIT_BOOK))
    monkeypatch.setattr(ab, 'opening_equity', lambda cur, sd, eq, path=None: (100_000.0, 'stored'))
    monkeypatch.setattr(ab, 'load_state', lambda cur: dict(ab._EMPTY_STATE))
    monkeypatch.setattr(ab, 'save_state', lambda *a, **k: True)
    db = _split_db(peak_pnl=1_000.0)
    monkeypatch.setattr(ab.psycopg2, 'connect', lambda *_a, **_k: FakeConn(db))
    env['acts'] = [_split('act1', 'AAPL')]
    assert ab.run_once(session_date=SESSION) == 0          # tick 1: episode starts
    assert db.rebase_inserts == 0
    env['now'] = NOW + timedelta(seconds=300)
    caplog.clear()
    with caplog.at_level('INFO', logger=ab.logger.name):
        assert ab.run_once(session_date=SESSION) == 0
    line = [m for m in (r.getMessage() for r in caplog.records)
            if m.startswith('[account_breaker] ')][-1]
    assert line.startswith('[account_breaker] armed' if armed else '[account_breaker] shadow')
    assert 'rebased=1' in line and 'recon=0' in line
    assert 'alpha_pnl=1000.00' in line and 'dd_pnl=0.0000' in line     # not skipped, even armed
    (row,) = db.rebases()
    assert row['taken_at'] == env['now']                   # t0 captured in run_once (frozen clock)


def test_repair_failure_never_raises_out_of_the_tick(env, monkeypatch):
    db = _split_db()
    env['acts'] = [_split('act1', 'AAPL')]
    monkeypatch.setattr(ab, 'evaluate_activities',
                        lambda *a, **k: (_ for _ in ()).throw(ValueError('bug')))
    _r, res = _two(db, SPLIT_BOOK, env)
    assert res is not None and res['rebased'] == 0 and res['recon'] == 1


# ── R2: ratio plausibility on the weak paths ────────────────────────────────

@pytest.mark.parametrize('ledger, broker', [(80, 160), (100, 150), (100, 10), (7, 21)])
def test_r2_plausible_split_ratios_pass_on_broker_qty_and_no_qty_paths(ledger, broker):
    q = ab._quantity_consistent
    assert q({'qty': str(broker)}, 'symbol', 'SPLIT', ledger, broker)    # qty == broker_qty path
    assert q({}, 'symbol', 'SPLIT', ledger, broker)                       # no usable quantity


@pytest.mark.parametrize('ledger, broker', [(100, 137), (100, 0), (0, 50), (100, -100),
                                            (100, 95), (100, 90), (100, 85), (50, 45)])
def test_r2_implausible_ratio_is_not_explained(ledger, broker):
    q = ab._quantity_consistent
    assert not q({}, 'symbol', 'SPLIT', ledger, broker)
    if ledger and broker:       # (qty == broker_qty is also the delta when ledger==0; qty 0 == absent)
        assert not q({'qty': str(broker)}, 'symbol', 'SPLIT', ledger, broker)
    # the qty == delta path is unchanged (needs no ratio)
    assert q({'qty': str(broker - ledger)}, 'symbol', 'SPLIT', ledger, broker)


def test_r2_end_to_end_implausible_ratio_stays_distrusted(env):
    db = FakeDB(lots=[_epoch_row('AAPL', 100, 100)], fills=[])
    env['acts'] = [_split('s1', 'AAPL')]
    book = {'AAPL': _pos(137, 70, 75, upl=685), 'SPY': _pos(1, 1, 1)}
    _r, res = _two(db, book, env)
    assert res['rebased'] == 0 and res['recon'] == 1 and db.rebase_inserts == 0


# ── R3: new-symbol keys rebase only together with the old key ───────────────

def test_r3_related_new_alone_never_auto_rebases_and_is_named_in_the_notice(env):
    db = FakeDB(lots=[], fills=[])                       # OLDT is not mismatched / not held
    book = {'NEWT': _pos(50, 20, 22, upl=100), 'SPY': _pos(1, 1, 1)}
    env['acts'] = [{'id': 'sc1', 'activity_type': 'SC', 'symbol': 'OLDT', 'new_symbol': 'NEWT',
                    'date': '2026-10-05'}]
    _tick(db, book)
    env['now'] = NOW + ESC + timedelta(seconds=1)
    res = _tick(db, book)
    assert res['rebased'] == 0 and db.rebase_inserts == 0 and res['recon'] == 1
    (_ch, msg), = env['posts']
    assert 'NEWT' in msg and 'new symbol only' in msg


def test_r3_old_and_new_together_are_both_rebased_old_first(env):
    db = FakeDB(lots=[_epoch_row('OLDT', 50, 20)], fills=[])
    book = {'NEWT': _pos(50, 20, 22, upl=100), 'SPY': _pos(1, 1, 1)}
    env['acts'] = [{'id': 'sc1', 'activity_type': 'SC', 'symbol': 'OLDT', 'new_symbol': 'NEWT',
                    'qty': '50', 'date': '2026-10-05'}]
    _r, res = _two(db, book, env)
    assert res['rebased'] == 2 and res['recon'] == 0
    assert [r['ticker'] for r in db.rebases()] == ['OLDT', 'NEWT']      # old first


# ── R1: late fill after a rebase ────────────────────────────────────────────

def _rebased_split(env):
    db = _split_db()
    env['acts'] = [_split('act1', 'AAPL', qty='80')]
    _two(db, SPLIT_BOOK, env)
    assert db.rebase_inserts == 1
    return db


def test_r1_late_fill_after_a_rebase_is_detected_distrusted_every_tick_never_auto_rebased(
        env, caplog):
    db = _rebased_split(env)
    cutoff = db.rebases()[0]['taken_at']
    # a closing fill from BEFORE the rebase cutoff is ingested only now (posting lag)
    late = {'id': 'late1', 'order_id': 'olate1', 'symbol': 'AAPL', 'side': 'sell', 'qty': '10',
            'price': '120', 'transaction_time': (cutoff - timedelta(hours=1)).isoformat()}
    env['feed'] = [late]
    env['now'] += timedelta(seconds=300)
    with caplog.at_level('ERROR', logger=ab.logger.name):
        res = _tick(db, SPLIT_BOOK)
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith('[account_breaker] LATE FILL late1 AAPL filled_at=')
               and '<= rebase cutoff' in m
               and 'realized P&L of this fill is NOT in the ledger' in m for m in msgs)
    assert db.watch['AAPL'][6] and db.watch['AAPL'][6].startswith('late1@')
    assert res['recon'] == 1 and 'AAPL' in res['mismatch']      # ledger==broker, still distrusted
    # it is skipped by the cutoff: alpha unchanged by the late fill
    assert res['alpha_pnl'] == pytest.approx(1_000.0)
    # distrusted on every later tick, never auto-rebased even with a fresh SPLIT activity
    env['acts'].append(_split('act2', 'AAPL', day='2026-10-05'))
    for _ in range(3):
        env['now'] += timedelta(seconds=300)
        r = _tick(db, SPLIT_BOOK)
        assert r['recon'] == 1 and r['rebased'] == 0 and 'AAPL' in r['mismatch']
    assert db.rebase_inserts == 1
    # existing notice path, with the late-fill text and the command
    env['now'] += ESC
    _tick(db, SPLIT_BOOK)
    msg = env['posts'][-1][1]
    assert 'late fill after a rebase — operator review required' in msg
    assert 'account_breaker.py --rebase AAPL' in msg and '--realized-adjust' in msg


def test_r1_operator_rebase_with_realized_adjust_clears_the_marker_and_moves_alpha_by_exactly_it(env):
    db = _rebased_split(env)
    cutoff = db.rebases()[0]['taken_at']
    env['feed'] = [{'id': 'late1', 'order_id': 'o', 'symbol': 'AAPL', 'side': 'sell', 'qty': '10',
                    'price': '120', 'transaction_time': (cutoff - timedelta(hours=1)).isoformat()}]
    env['now'] += timedelta(seconds=300)
    before = _tick(db, SPLIT_BOOK)
    assert before['recon'] == 1
    out = []
    conn = FakeConn(db)
    rc = ab.rebase_cli(db, conn, SPLIT_BOOK, {'SPY'}, ['AAPL'], 'late fill', False,
                       now=env['now'], realized_adjust=100.0, out=out.append)
    text = '\n'.join(out)
    assert rc == 0 and db.rebase_inserts == 1 and 'LATE FILL' in text and 'late1@' in text
    assert 'before 1000.00 | after 1100.00' in text and db.watch['AAPL'][6]      # dry-run: marker stays
    rc = ab.rebase_cli(db, conn, SPLIT_BOOK, {'SPY'}, ['AAPL', 'MSFT'], 'x', True,
                       now=env['now'], realized_adjust=100.0, out=out.append)
    assert rc == 2                                              # adjust needs exactly one ticker
    rc = ab.rebase_cli(db, conn, SPLIT_BOOK, {'SPY'}, ['AAPL'], 'late fill', True,
                       now=env['now'], realized_adjust=100.0, out=out.append)
    assert rc == 0 and db.rebase_inserts == 2
    row = db.rebases()[-1]
    assert row['realized_carry'] == pytest.approx(200.0 + 100.0)
    assert 'realized-adjust=+100.00' in row['reason'] and row['reason'].startswith('operator:late fill')
    assert db.watch['AAPL'][6] is None                          # marker cleared (row kept)
    env['now'] += timedelta(seconds=300)
    after = _tick(db, SPLIT_BOOK)
    assert after['recon'] == 0 and after['rebased'] == 0
    assert after['alpha_pnl'] - before['alpha_pnl'] == pytest.approx(100.0)   # exactly the adjustment



@pytest.mark.parametrize('bad', [float('nan'), float('inf'), float('-inf')])
def test_operator_rebase_refuses_a_non_finite_realized_adjust(env, bad):
    """A NaN carry would make alpha_pnl/dd NaN and silently kill the drawdown rule."""
    db = FakeDB(lots=[_epoch_row('AAPL', 100, 100)], fills=[])
    conn, out = FakeConn(db), []
    rc = ab.rebase_cli(db, conn, SPLIT_BOOK, {'SPY'}, ['AAPL'], 'x', True,
                       now=env['now'], realized_adjust=bad, out=out.append)
    assert rc == 2 and db.rebase_inserts == 0
    assert 'finite' in '\n'.join(out)
