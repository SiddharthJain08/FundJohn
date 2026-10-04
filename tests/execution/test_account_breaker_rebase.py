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


def _ssp(aid, sym, day='2026-10-05', **kw):
    return {'id': aid, 'activity_type': 'SSP', 'symbol': sym, 'date': day,
            'qty': '80', 'net_amount': '0', **kw}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    """Hermetic broker + a controllable clock. `acts` is what the stubbed
    non-FILL activity lookup returns; `calls` records each lookup."""
    h = {'now': NOW, 'acts': [], 'calls': [], 'posts': [], 'raise': None}
    monkeypatch.setattr(ar, 'fetch_fills_since', lambda after, **k: [])
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
        self.watch = {}                 # ticker -> [first, last, notified, cleared]
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
        elif f.startswith('INSERT INTO account_breaker_recon_watch'):
            t, first, last, notified, cleared = params
            self.watch[t] = [first, last, notified, cleared]

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


def _tick(db, positions, conn=None, equity=100_000.0, bench=('SPY',)):
    return ab.compute_alpha_pnl_tick(db, conn or FakeConn(db), equity, positions, set(bench))


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
    # nothing mismatched => no activity lookup, no rebase, rebased=0
    db = FakeDB(lots=tagged, fills=fills)
    res = _tick(db, {'AAPL': _pos(85, 100.0, 105, upl=425), 'SPY': _pos(1, 1, 1)})
    assert res['rebased'] == 0 and res['recon'] == 0 and db.rebase_inserts == 0


# ── acceptance 2: split => auto-rebase, recon 0 the same tick, alpha continuous ─

def test_acc2_split_auto_rebase_recon_zero_alpha_continuous_hwm_untouched(env):
    db = _split_db()
    before = _tick(db, SPLIT_BOOK)                       # no SPLIT activity yet
    assert before['recon'] == 1 and before['rebased'] == 0
    assert before['alpha_pnl'] == pytest.approx(1_000.0)
    assert db.rebase_inserts == 0
    env['acts'] = [_ssp('act1', 'AAPL')]
    conn = FakeConn(db)
    after = _tick(db, SPLIT_BOOK, conn)
    assert after['rebased'] == 1 and after['recon'] == 0
    assert round(after['alpha_pnl'], 2) == round(before['alpha_pnl'], 2) == 1_000.00
    assert after['realized'] == pytest.approx(200.0)    # carried, not re-earned
    assert after['hwm'] == before['hwm'] == 1_500.0 and db.peak_pnl == 1_500.0
    (row,) = db.rebases()
    assert (row['ticker'], row['qty'], row['avg_entry_price'], row['side']) == \
        ('AAPL', 160.0, 50.0, 'long')
    assert row['realized_carry'] == pytest.approx(200.0)
    assert row['reason'] == 'auto:SSP' and row['activity_ref'] == 'act1'
    assert row['taken_at'] == NOW and conn.committed == 1
    # the epoch is never moved and the HWM never written by a rebase
    assert not db.sql('alpha_epoch_at =') and not db.sql('SET peak_alpha_pnl')
    assert not db.sql('UPDATE account_breaker_state')
    # the next tick reads the rebase row from the DB: still reconciled, no new lookup
    n_calls = len(env['calls'])
    again = _tick(db, SPLIT_BOOK)
    assert again['recon'] == 0 and again['rebased'] == 0 and len(env['calls']) == n_calls
    assert again['alpha_pnl'] == pytest.approx(1_000.0)


def test_acc2_activity_lookup_argument_and_single_fetch(env):
    db = FakeDB(lots=[_epoch_row('AAPL', 100, 100), _epoch_row('MSFT', 10, 300)], fills=[])
    env['acts'] = [_ssp('a1', 'AAPL'), _ssp('a2', 'MSFT')]
    book = {'AAPL': _pos(200, 50, 55, upl=1_000), 'MSFT': _pos(30, 100, 110, upl=300),
            'SPY': _pos(1, 1, 1)}
    res = _tick(db, book)
    assert res['rebased'] == 2 and res['recon'] == 0
    assert len(env['calls']) == 1                         # ONE lookup for both tickers
    after, types, kw = env['calls'][0]
    assert after == '2026-09-28' and types == ab.REBASE_ACTIVITY_TYPES      # day before the epoch (ET)
    assert kw['raise_on_cap'] is True and kw['max_pages'] == ab.REBASE_ACTIVITY_MAX_PAGES
    assert kw['deadline_s'] == ab.REBASE_ACTIVITY_BUDGET_S


# ── acceptance 3: symbol change ─────────────────────────────────────────────

def test_acc3_symbol_change_old_key_to_zero_new_key_to_broker_lot(env):
    # OLDT: 60 @20 at the epoch, sold 10 @25 (+50) => ledger 50, then renamed.
    db = FakeDB(lots=[_epoch_row('OLDT', 60, 20)],
                fills=[_fill('OLDT', 'sell', 10, 25, datetime(2026, 9, 30, 14, 0, tzinfo=UTC), 'g1')])
    book = {'NEWT': _pos(50, 20, 22, upl=100), 'SPY': _pos(1, 1, 1)}
    pre = _tick(db, book)
    assert pre['recon'] == 2                              # OLDT ledger 50 vs 0, NEWT 0 vs 50
    assert pre['alpha_pnl'] == pytest.approx(150.0)
    env['acts'] = [{'id': 'sc1', 'activity_type': 'SC', 'symbol': 'NEWT', 'date': '2026-10-05',
                    'description': 'SYMBOL CHANGE OLDT TO NEWT'}]
    res = _tick(db, book)
    assert res['rebased'] == 2 and res['recon'] == 0
    assert round(res['alpha_pnl'], 2) == 150.00
    rows = {r['ticker']: r for r in db.rebases()}
    assert (rows['OLDT']['qty'], rows['OLDT']['avg_entry_price'], rows['OLDT']['side']) == (0.0, None, None)
    assert rows['OLDT']['realized_carry'] == pytest.approx(50.0)     # old key's realized kept
    assert (rows['NEWT']['qty'], rows['NEWT']['avg_entry_price']) == (50.0, 20.0)
    assert rows['NEWT']['realized_carry'] == 0.0
    assert rows['OLDT']['reason'] == 'auto:SC'


def test_activity_matching_is_conservative():
    m = ab._activity_matches
    assert m({'activity_type': 'SSP', 'symbol': 'AAPL'}, 'AAPL')
    assert not m({'activity_type': 'SSP', 'symbol': 'MSFT'}, 'AAPL')
    assert not m({'activity_type': 'DIV', 'symbol': 'AAPL'}, 'AAPL')            # not in the list
    assert m({'activity_type': 'OPASN', 'symbol': 'AAPL261016C00100000'}, 'AAPL')
    assert not m({'activity_type': 'SSP', 'symbol': 'AAPL261016C00100000'}, 'AAPL')
    assert m({'activity_type': 'MA', 'symbol': 'XYZ', 'description': 'MERGER OLDT INTO XYZ'}, 'OLDT')
    assert not m({'activity_type': 'SSP', 'description': 'OLDT'}, 'OLDT')       # description: SC-like types only
    assert not m({'activity_type': 'MA', 'symbol': 'XYZ', 'description': 'MERGER OLDTX'}, 'OLDT')
    assert not m({'activity_type': 'MA', 'symbol': 'XYZ', 'description': 'MERGER ON ABC'}, 'ON')  # <3 chars


def test_one_activity_never_explains_a_later_mismatch_on_the_same_ticker(env):
    db = _split_db()
    env['acts'] = [_ssp('act1', 'AAPL')]
    _tick(db, SPLIT_BOOK)
    assert db.rebase_inserts == 1
    # a missing fill later: qty drifts. act1 is already this ticker's activity_ref.
    drift = {'AAPL': _pos(170, 50, 55, upl=850), 'SPY': _pos(1, 1, 1)}
    env['now'] = NOW + timedelta(hours=1)
    res = _tick(db, drift)
    assert res['rebased'] == 0 and res['recon'] == 1 and db.rebase_inserts == 1


# ── acceptance 4: unexplained mismatch ──────────────────────────────────────

def test_acc4_unexplained_mismatch_notice_after_window_and_operator_command_repairs(env):
    db = _split_db()
    res = _tick(db, SPLIT_BOOK)
    assert res['rebased'] == 0 and res['recon'] == 1 and db.rebase_inserts == 0
    assert db.watch['AAPL'] == [NOW, NOW, None, None]
    assert env['posts'] == []
    # still inside the escalation window
    env['now'] = NOW + timedelta(seconds=ab.REBASE_ESCALATE_S - 60)
    _tick(db, SPLIT_BOOK)
    assert env['posts'] == [] and db.watch['AAPL'][0] == NOW         # first_seen kept
    assert db.watch['AAPL'][1] == env['now']                         # last_seen advanced
    # window elapsed: exactly ONE notice naming the ticker, both qtys, the command
    env['now'] = NOW + timedelta(seconds=ab.REBASE_ESCALATE_S + 1)
    conn = FakeConn(db)
    _tick(db, SPLIT_BOOK, conn)
    (ch, msg), = env['posts']
    assert ch == 'trade-reports' and 'AAPL' in msg
    assert 'ledger 80' in msg and 'broker 160' in msg
    assert 'account_breaker.py --rebase AAPL' in msg and '--apply' in msg
    assert db.watch['AAPL'][2] == env['now'] and conn.committed == 1  # notified_at durable first
    # throttled by notified_at, not the fallback file
    env['now'] += timedelta(minutes=5)
    _tick(db, SPLIT_BOOK)
    assert len(env['posts']) == 1
    env['now'] += timedelta(seconds=ab.REBASE_ESCALATE_S)
    _tick(db, SPLIT_BOOK)
    assert len(env['posts']) == 2
    # operator repairs it
    out = []
    conn = FakeConn(db)
    rc = ab.rebase_cli(db, conn, SPLIT_BOOK, {'SPY'}, ['AAPL'], 'confirmed split', True,
                       now=env['now'], out=out.append)
    assert rc == 0 and db.rebase_inserts == 1
    assert db.rebases()[0]['reason'] == 'operator:confirmed split'
    env['now'] += timedelta(minutes=5)
    res = _tick(db, SPLIT_BOOK)
    assert res['recon'] == 0 and res['alpha_pnl'] == pytest.approx(1_000.0)
    assert db.watch['AAPL'][3] == env['now']                         # cleared, row kept
    # a later mismatch is a NEW episode: first_seen reset, notified/cleared reset
    env['now'] += timedelta(hours=2)
    _tick(db, {'AAPL': _pos(170, 50, 55, upl=850), 'SPY': _pos(1, 1, 1)})
    assert db.watch['AAPL'] == [env['now'], env['now'], None, None]


# ── acceptance 5: fill-quiet window ─────────────────────────────────────────

def test_acc5_mismatch_inside_the_fill_quiet_window_is_not_rebased(env):
    recent = _fill('AAPL', 'sell', 1, 110, NOW - timedelta(seconds=ab.REBASE_FILL_QUIET_S - 20), 'r1')
    db = FakeDB(lots=[_epoch_row('AAPL', 100, 100)], fills=[SOLD, recent])
    env['acts'] = [_ssp('act1', 'AAPL')]
    res = _tick(db, {'AAPL': _pos(160, 50, 55, upl=800), 'SPY': _pos(1, 1, 1)})
    assert res['rebased'] == 0 and res['recon'] == 1 and db.rebase_inserts == 0
    assert env['calls'] == []                      # no lookup when nothing is eligible
    # just past the window it IS eligible
    env['now'] = NOW + timedelta(seconds=60)
    res = _tick(db, {'AAPL': _pos(160, 50, 55, upl=800), 'SPY': _pos(1, 1, 1)})
    assert res['rebased'] == 1 and len(env['calls']) == 1


def test_activity_lookup_not_issued_when_nothing_is_mismatched(env):
    db = FakeDB(lots=[_epoch_row('AAPL', 80, 100)], fills=[])
    res = _tick(db, {'AAPL': _pos(80, 100, 105, upl=400), 'SPY': _pos(1, 1, 1)})
    assert res['recon'] == 0 and env['calls'] == []


# ── acceptance 6: lookup failure / cap / timeout => no rebase, no crash ─────

@pytest.mark.parametrize('exc', [RuntimeError('cli down'),
                                 ar.FillPagesTruncated('hit max_pages=5', []),
                                 TimeoutError('deadline')])
def test_acc6_lookup_failure_cap_or_timeout_means_no_rebase_no_crash(env, exc):
    db = _split_db()
    env['acts'] = [_ssp('act1', 'AAPL')]
    env['raise'] = exc
    res = _tick(db, SPLIT_BOOK)
    assert res is not None and res['rebased'] == 0 and res['recon'] == 1
    assert db.rebase_inserts == 0
    env['raise'] = None                                  # retried on the next tick
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
    assert a[a.index('--activity-types') + 1] == 'SSP,SSO,SC,NC,MA,REORG,OPASN,OPXRC,OPEXP,JNLS,ACATS'
    assert a[a.index('--after') + 1] == '2026-10-04' and a[a.index('--direction') + 1] == 'asc'
    assert a[a.index('--page-size') + 1] == '2' and '--page-token' not in a
    assert '--page-token' in seen[1]
    # existing FILL callers are byte-identical
    seen.clear()
    monkeypatch.setattr(ar.subprocess, 'run',
                        lambda args, **k: seen.append(args) or Proc('[]'))
    _REAL_FETCH_SINCE('2026-10-04T00:00:00Z')
    ar.fetch_fills_for_date('2026-10-04')
    assert [a[a.index('--activity-types') + 1] for a in seen] == ['FILL', 'FILL']


def test_fetch_rebase_activities_returns_none_on_failure_and_drops_fills(env):
    env['raise'] = RuntimeError('boom')
    assert ab.fetch_rebase_activities(EPOCH) is None
    env['raise'] = None
    env['acts'] = [{'id': 'f', 'activity_type': 'FILL'}, _ssp('s', 'AAPL'), 'junk']
    assert [a['id'] for a in ab.fetch_rebase_activities(EPOCH)] == ['s']


# ── acceptance 7: rebase after the position is closed ───────────────────────

def test_acc7_rebase_after_the_position_is_closed(env):
    # ledger AAPL 80 (after the +200 sale); broker holds none (assignment delivered
    # the shares away, no FILL). An OPASN on an AAPL contract explains it.
    db = _split_db()
    book = {'MSFT': _pos(5, 10, 11, upl=5), 'SPY': _pos(1, 1, 1)}
    pre = _tick(db, book)
    assert pre['recon'] == 2 and pre['alpha_pnl'] == pytest.approx(205.0)   # AAPL + MSFT (no lot row)
    env['acts'] = [{'id': 'as1', 'activity_type': 'OPASN',
                    'symbol': 'AAPL261016C00100000', 'date': '2026-10-05'}]
    res = _tick(db, book)
    assert res['rebased'] == 1 and res['recon'] == 1      # MSFT has no lot row: still flagged
    (row,) = db.rebases()
    assert (row['ticker'], row['qty'], row['avg_entry_price'], row['side']) == ('AAPL', 0.0, None, None)
    assert row['realized_carry'] == pytest.approx(200.0) and row['reason'] == 'auto:OPASN'
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
                'cleared_at TIMESTAMPTZ'):
        assert col in norm
    # every statement is an ALTER ... ADD COLUMN IF NOT EXISTS or CREATE ... IF NOT EXISTS
    stmts = [s.strip() for s in code.split(';') if s.strip()]
    assert all(s.startswith(('ALTER TABLE account_breaker_alpha_epoch', 'CREATE TABLE IF NOT EXISTS'))
               for s in stmts)


# ── per-ticker fill cutoff, realized carry ──────────────────────────────────

def test_per_ticker_cutoff_ignores_earlier_fills_but_not_another_tickers_same_time_fill():
    t = datetime(2026, 10, 5, 14, 30, tzinfo=UTC)
    rows = [_epoch_row('AAPL', 100, 100), _epoch_row('MSFT', 0, None),
            {'kind': 'rebase', 'ticker': 'AAPL', 'qty': 10, 'avg_entry_price': 50.0,
             'side': 'long', 'taken_at': t, 'realized_carry': 5.0, 'reason': 'auto:SSP',
             'activity_ref': 'a'}]
    fills = [_fill('AAPL', 'buy', 5, 40, t - timedelta(minutes=1), 'x1'),    # before the rebase: ignored
             _fill('AAPL', 'buy', 7, 41, t, 'x2'),                           # same instant: ignored (strict >)
             _fill('MSFT', 'buy', 3, 10, t, 'x3'),                           # other ticker, same instant: applies
             _fill('AAPL', 'sell', 4, 60, t + timedelta(minutes=1), 'x4')]   # after: applies
    book = {'AAPL': _pos(6, 50, 60, upl=60), 'MSFT': _pos(3, 10, 10, upl=0)}
    r = ab.alpha_pnl(1, book, set(), fills, rows)
    assert r['recon'] == 0 and r['mismatch'] == {}
    assert r['realized_by_key']['AAPL'] == pytest.approx(5.0 + 40.0)   # carry + 4*(60-50)
    assert r['realized'] == pytest.approx(45.0) and r['unmatched'] == 0
    # the epoch row of a rebased key is NOT applied on top of its rebase row
    assert ab.alpha_pnl(1, {'AAPL': _pos(110, 100, 100)}, set(), [], rows)['recon'] == 1


def test_two_successive_rebases_carry_realized_correctly(env):
    db = _split_db()
    env['acts'] = [_ssp('act1', 'AAPL')]
    r1 = _tick(db, SPLIT_BOOK)
    assert r1['rebased'] == 1 and db.rebases()[0]['realized_carry'] == pytest.approx(200.0)
    # a day later: sell 10 @70 against avg 50 (+200), then a 3-for-1 split of the remaining 150
    env['now'] = NOW + timedelta(days=1)
    db.fills.append(_fill('AAPL', 'sell', 10, 70, NOW + timedelta(hours=1), 'f9'))
    env['acts'] = [_ssp('act1', 'AAPL'), _ssp('act2', 'AAPL', day='2026-10-06')]
    book = {'AAPL': _pos(450, 50 / 3, 25, upl=450 * (25 - 50 / 3)), 'SPY': _pos(1, 1, 1)}
    pre = ab.alpha_pnl(0, book, {'SPY'}, db.fills, [r for r in db.rows])
    assert pre['recon'] == 1
    r2 = _tick(db, book)
    assert r2['rebased'] == 1 and r2['recon'] == 0
    row2 = db.rebases()[1]
    assert row2['realized_carry'] == pytest.approx(400.0)     # 200 carried + 200 since
    assert row2['activity_ref'] == 'act2'                     # act1 already spent
    assert round(r2['alpha_pnl'], 2) == round(pre['alpha_pnl'], 2)
    assert r2['realized'] == pytest.approx(400.0)


# ── missing migration 162 => behave exactly as today ────────────────────────

def test_missing_migration_162_degrades_to_todays_behaviour(env, caplog):
    db = _split_db(missing_162=True)
    env['acts'] = [_ssp('act1', 'AAPL')]
    res = _tick(db, SPLIT_BOOK)
    assert res is not None
    assert res['rebased'] == 0 and res['recon'] == 1          # the write failed: not counted
    assert res['alpha_pnl'] == pytest.approx(1_000.0)
    assert db.rebase_inserts == 0
    # reads fall back to the legacy lots
    lots, fills = ab.load_alpha_inputs(db, EPOCH)
    assert lots == [{'ticker': 'AAPL', 'qty': 100, 'avg_entry_price': 100, 'side': 'long'}]
    # ...but a legacy table that already holds >1 row per ticker (rebase exists,
    # columns unreadable) must NOT be double-counted
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


def test_cli_apply_writes_exactly_one_row_per_ticker(env):
    db = FakeDB(lots=[_epoch_row('AAPL', 100, 100), _epoch_row('MSFT', 10, 300)], fills=[])
    book = {'AAPL': _pos(200, 50, 55, upl=1_000), 'MSFT': _pos(30, 100, 110, upl=300),
            'SPY': _pos(1, 1, 1)}
    conn = FakeConn(db)
    out = []
    rc = ab.rebase_cli(db, conn, book, {'SPY'}, ['AAPL', 'MSFT'], 'two splits', True,
                       now=NOW, out=out.append)
    assert rc == 0 and db.rebase_inserts == 2 and conn.committed == 1
    assert {r['ticker'] for r in db.rebases()} == {'AAPL', 'MSFT'}
    assert all(r['reason'] == 'operator:two splits' and r['kind'] == 'rebase' for r in db.rebases())
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
    monkeypatch.setattr(ab, '_run_rebase_cli', lambda t, r, a: cli.append((t, r, a)) or 0)
    assert ab.main([]) == 0 and ticks == [1] and cli == []
    ticks.clear()
    assert ab.main(['--rebase', 'aapl, msft', '--reason', 'why']) == 0
    assert cli == [(['AAPL', 'MSFT'], 'why', False)] and ticks == []
    assert ab.main(['--rebase', 'AAPL', '--apply']) == 0 and cli[-1] == (['AAPL'], None, True)
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
def test_run_once_rebases_in_both_modes_and_armed_drawdown_is_not_skipped(
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
    env['acts'] = [_ssp('act1', 'AAPL')]
    with caplog.at_level('INFO', logger=ab.logger.name):
        assert ab.run_once(session_date=SESSION) == 0
    line = [m for m in (r.getMessage() for r in caplog.records)
            if m.startswith('[account_breaker] ')][-1]
    assert line.startswith('[account_breaker] armed' if armed else '[account_breaker] shadow')
    assert 'rebased=1' in line and 'recon=0' in line
    assert 'alpha_pnl=1000.00' in line and 'dd_pnl=0.0000' in line     # not skipped, even armed
    assert db.rebase_inserts == 1


def test_repair_failure_never_raises_out_of_the_tick(env, monkeypatch):
    db = _split_db()
    env['acts'] = [_ssp('act1', 'AAPL')]
    monkeypatch.setattr(ab, 'find_explaining_activity',
                        lambda *a, **k: (_ for _ in ()).throw(ValueError('bug')))
    res = _tick(db, SPLIT_BOOK)
    assert res is not None and res['rebased'] == 0 and res['recon'] == 1
