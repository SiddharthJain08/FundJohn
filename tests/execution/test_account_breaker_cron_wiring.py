"""C1 entry point + cron wiring.

run_once() is driven entirely through patched surfaces — no CLI, no Postgres,
no Discord. The cron assertion is a source read, so it needs no node runtime.

Deviations from the brief's literal Step 1 draft (recorded in
task-7-report.md, mapped against the brief supplement's binding items):

  1. `wired` additionally patches `regime_liquidator._load_open_orders` and
     zeroes `account_breaker._SETTLE_S` — every armed-mode scenario here
     exercises flatten_alpha's LIVE branch, which calls `_load_open_orders()`
     unconditionally; leaving it unpatched would attempt the real alpaca CLI
     (forbidden — hard constraint) and pay the real settle sleep.
  2. `_load_broker_positions` is a call-counted double (full book on the
     FIRST read, a caller-supplied `reread` book on every read after) instead
     of a flat constant — flatten_alpha's live path re-reads the broker
     AFTER submitting closes to confirm they landed; a constant double makes
     every submitted close read back as "still open" (pending=True forever),
     which is inconsistent with flatten_alpha's actual, already-reviewed
     behaviour (Task 5) and would make `pending=0` unreachable in any armed
     test.
  3. `test_armed_breach_flattens_halts_and_posts` asserts `rule ==
     'drawdown+daily_loss'`, not `'drawdown'`: at equity=100_000 vs
     open_equity=205_000 the daily rule (-51.2%) breaches alongside the
     drawdown rule (-70.5%) given this fixture's numbers — both rules fire,
     so `evaluate()` legitimately returns the combined rule string. This is
     the actual arithmetic of the given fixture, not a bug to route around.
  4. Two commits, not one: supplement item 1 (CRITICAL ordering, binding,
     overrides the plan's draft `main()`) requires the halt latch to be
     persisted and committed *before* any broker action, then persisted
     again after — so a NEW breach → ARMED transition writes
     `UPDATE account_breaker_state` twice in one tick. Tests that hit this
     transition assert `len(updates) == 2` and inspect the LAST one (the
     final, post-flatten state) rather than unpacking a single match.
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import account_breaker as ab            # noqa: E402
from execution import alpaca_trader as at              # noqa: E402
from execution import regime_liquidator as rl          # noqa: E402

CRON = ROOT / 'src' / 'engine' / 'cron-schedule.js'
SESSION = date(2026, 9, 16)

POSITIONS = {
    'SPY':  {'qty': 200.0, 'side': 'long', 'market_value': '41000'},
    'AAPL': {'qty': 100.0, 'side': 'long', 'market_value': '22000'},
}


class FakeCursor:
    def __init__(self, state_row=None, open_row=None):
        self._queue = [state_row, open_row]
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((' '.join(sql.split()), params))

    def fetchone(self):
        return self._queue.pop(0) if self._queue else None

    def fetchall(self):
        return []

    def sql_matching(self, needle):
        return [c for c in self.calls if needle in c[0]]

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class FailFirstUpdateCursor(FakeCursor):
    """SAVEPOINT/ROLLBACK/RELEASE succeed; the FIRST UPDATE against
    account_breaker_state raises — exercises supplement item 1's critical
    ordering gate: no broker action may happen once persistence of the halt
    latch itself has failed."""

    def execute(self, sql, params=None):
        flat = ' '.join(sql.split())
        if flat.startswith('UPDATE account_breaker_state'):
            self.calls.append((flat, params))
            raise RuntimeError('db down')
        super().execute(sql, params)


class FakeConn:
    # Explicit, not absent (fix round 1 item 3): run_once() now reads
    # `conn.autocommit` directly (no getattr default) inside its own
    # try/except, so a double lacking the attribute entirely would still
    # "work" by falling into that except and returning 1 — but that masks
    # the very check this attribute exists to exercise, so every FakeConn
    # here declares it like a real psycopg2 connection would.
    autocommit = False

    def __init__(self, cur):
        self._cur = cur
        self.committed = 0
        self.closed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed += 1

    def close(self):
        self.closed = True


class RaisingCommitConn(FakeConn):
    """commit() raises — the other half of supplement item 1's persistence
    gate: save_state's UPDATE can land cleanly while the commit that makes
    it durable still fails. _commit() catches this internally and returns
    False, which must gate broker action exactly like a raising UPDATE does
    (fix round 1 item 4, required test 1)."""

    def commit(self):
        self.committed += 1
        raise RuntimeError('commit failed: connection reset')


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Everything run_once() touches, patched. Returns the FakeCursor."""
    monkeypatch.setenv('POSTGRES_URI', 'postgres://stub')
    monkeypatch.setenv(ab.NAV_OHLC_PATH_ENV, str(tmp_path / 'missing.json'))
    monkeypatch.setenv(ab.FALLBACK_POST_PATH_ENV, str(tmp_path / 'fallback_post.ts'))
    monkeypatch.delenv(ab.ARM_ENV, raising=False)
    monkeypatch.delenv(ab.REARM_ENV, raising=False)
    monkeypatch.setattr(ab, '_SETTLE_S', 0)
    monkeypatch.setattr(rl, '_market_is_open', lambda: True)
    monkeypatch.setattr(rl, '_load_open_orders', lambda: [])
    monkeypatch.setattr(at, '_alpaca_session', lambda: object())
    monkeypatch.setattr(ab, 'bench_tickers', lambda conn: {'SPY'})

    posts = []
    monkeypatch.setattr(rl, '_post_to_discord',
                        lambda ch, msg: posts.append((ch, msg)) or True)

    holder = {'posts': posts, 'reread': {'SPY': dict(POSITIONS['SPY'])}}

    calls = {'n': 0}

    def _positions():
        calls['n'] += 1
        if calls['n'] == 1:
            return dict(POSITIONS)
        return dict(holder['reread'])

    monkeypatch.setattr(rl, '_load_broker_positions', _positions)

    def _install(cur, equity):
        conn = FakeConn(cur)
        monkeypatch.setattr(ab.psycopg2, 'connect', lambda *_a, **_k: conn)
        monkeypatch.setattr(at, '_fetch_account_state', lambda sess: {'equity': equity})
        holder['conn'] = conn
        return conn

    holder['install'] = _install
    return holder


def _state(halted=False, peak=None, breached_at=None, reason=None, pending=False):
    return (halted, reason, breached_at, peak, None, None, pending)


# ── guards ──────────────────────────────────────────────────────────────────

def test_market_closed_skips_without_touching_the_db(monkeypatch, wired):
    monkeypatch.setattr(rl, '_market_is_open', lambda: False)
    monkeypatch.setattr(ab.psycopg2, 'connect',
                        lambda *_a, **_k: pytest.fail('must not connect'))
    assert ab.run_once(session_date=SESSION) == 0


def test_bench_lookup_failure_blocks_the_whole_tick(monkeypatch, wired, caplog):
    cur = FakeCursor(_state(peak=200_000.0), None)
    wired['install'](cur, 100_000.0)
    monkeypatch.setattr(ab, 'bench_tickers', lambda conn: None)
    assert ab.run_once(session_date=SESSION) == 1
    assert cur.sql_matching('UPDATE account_breaker_state') == []
    assert cur.sql_matching('INSERT INTO circuit_breaker_fires') == []


def test_zero_equity_is_a_soft_failure(monkeypatch, wired):
    cur = FakeCursor(_state(), None)
    wired['install'](cur, 0.0)
    assert ab.run_once(session_date=SESSION) == 1


def test_bench_tickers_is_called_with_the_cursor_not_the_connection(monkeypatch, wired):
    """Supplement item 8 (contract change from Task 5): bench_tickers(cur)
    takes the caller's CURSOR, opened before it is called — not the bare
    connection."""
    seen = {}

    def _spy(cur_arg):
        seen['arg'] = cur_arg
        return {'SPY'}

    monkeypatch.setattr(ab, 'bench_tickers', _spy)
    cur = FakeCursor(_state(peak=200_000.0), (205_000.0,))
    wired['install'](cur, 200_000.0)
    assert ab.run_once(session_date=SESSION) == 0
    assert seen['arg'] is cur


def test_empty_positions_read_is_a_soft_failure_before_any_db_connect(monkeypatch, wired):
    """Supplement item 11: an empty/unavailable book must not evaluate at
    all — bench_mv would read 0 and alpha_nav would silently become total
    equity."""
    monkeypatch.setattr(rl, '_load_broker_positions', lambda: {})
    monkeypatch.setattr(ab.psycopg2, 'connect',
                        lambda *_a, **_k: pytest.fail('must not connect on an empty book'))
    assert ab.run_once(session_date=SESSION) == 1


def test_none_positions_read_is_also_a_soft_failure(monkeypatch, wired):
    monkeypatch.setattr(rl, '_load_broker_positions', lambda: None)
    monkeypatch.setattr(ab.psycopg2, 'connect',
                        lambda *_a, **_k: pytest.fail('must not connect on a None book'))
    assert ab.run_once(session_date=SESSION) == 1


def test_empty_book_guard_applies_to_the_halted_retry_branch_too(monkeypatch, wired):
    """Coordinator clarification (binding) on supplement item 11: the guard
    must protect the ALREADY-HALTED retry branch too — never call
    flatten_alpha with an empty positions mapping. flatten_alpha's own
    'nothing to close' early return reports pending=False, which would wrongly
    clear pending_flatten on a broker READ FAILURE rather than a genuine
    full flatten. Since the guard fires before load_state is ever read (it
    sits ahead of the DB connection entirely), a prior halt+pending state is
    left completely untouched."""
    monkeypatch.setenv(ab.ARM_ENV, '1')
    monkeypatch.setattr(rl, '_load_broker_positions', lambda: {})
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda *_a, **_k: pytest.fail(
                            'must not attempt a flatten on an empty/unavailable book'))
    breached = datetime(2026, 9, 16, 17, 42, tzinfo=timezone.utc)
    cur = FakeCursor(_state(halted=True, peak=200_000.0, breached_at=breached,
                            reason='drawdown', pending=True), (205_000.0,))
    wired['install'](cur, 100_000.0)
    monkeypatch.setattr(ab.psycopg2, 'connect',
                        lambda *_a, **_k: pytest.fail(
                            'must not evaluate (or even connect) on an empty/unavailable book'))
    assert ab.run_once(session_date=SESSION) == 1
    assert cur.sql_matching('UPDATE account_breaker_state') == []


def test_autocommit_true_connection_is_rejected(monkeypatch, wired):
    """Supplement item 4 + fix round 1 item 3: run_once() checks
    conn.autocommit before the first guarded call — a misconfigured
    autocommit connection would silently disable every savepoint guard in
    the module. Fix round 1 replaced the original `assert` (stripped under
    `python -O`) with a plain `if` + `return 2`, matching the brief's
    exit-code contract; no DB call may happen beyond the check itself."""
    class BadConn:
        autocommit = True

        def cursor(self):
            pytest.fail('must not reach the cursor with autocommit=True')

        def commit(self):
            pytest.fail('must not commit with autocommit=True')

        def close(self):
            pass

    monkeypatch.setattr(ab.psycopg2, 'connect', lambda *_a, **_k: BadConn())
    monkeypatch.setattr(at, '_fetch_account_state', lambda sess: {'equity': 100_000.0})
    assert ab.run_once(session_date=SESSION) == 2


# ── no breach ───────────────────────────────────────────────────────────────

def test_clean_tick_updates_the_peak_and_logs_rule_none(wired, caplog):
    cur = FakeCursor(_state(peak=160_000.0), (205_000.0,))
    wired['install'](cur, 200_000.0)     # alpha = 200000 - 41000 = 159000
    with caplog.at_level('INFO', logger=ab.logger.name):
        assert ab.run_once(session_date=SESSION) == 0
    line = [m for m in (r.getMessage() for r in caplog.records)
            if m.startswith('[account_breaker] ')][-1]
    assert ' shadow ' in line and 'rule=none' in line and 'breach=0' in line
    assert 'halted=0' in line and 'open_src=stored' in line
    (_sql, params), = cur.sql_matching('UPDATE account_breaker_state')
    assert params[0] is False and params[3] == 160_000.0


# ── breach, shadow ──────────────────────────────────────────────────────────

def test_shadow_breach_does_not_halt_and_submits_nothing(monkeypatch, wired, caplog):
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda *_a, **_k: pytest.fail('shadow must not submit'))
    cur = FakeCursor(_state(peak=200_000.0), (205_000.0,))
    wired['install'](cur, 100_000.0)     # alpha = 59000, dd = -0.705
    with caplog.at_level('INFO', logger=ab.logger.name):
        assert ab.run_once(session_date=SESSION) == 0
    line = [m for m in (r.getMessage() for r in caplog.records)
            if m.startswith('[account_breaker] ')][-1]
    assert ' shadow ' in line and 'breach=1' in line and 'halted=0' in line
    assert 'flatten_ok=1' in line          # AAPL would close; SPY is exempt
    (_sql, params), = cur.sql_matching('UPDATE account_breaker_state')
    assert params[0] is False              # halted stays FALSE with the flag off
    assert cur.sql_matching('INSERT INTO circuit_breaker_fires') == []
    assert wired['posts'] == []            # spec: shadow logs a line ONLY


# ── breach, armed ───────────────────────────────────────────────────────────

def test_armed_breach_flattens_halts_and_posts(monkeypatch, wired, caplog):
    monkeypatch.setenv(ab.ARM_ENV, '1')
    closed = []
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (closed.append(sym), (True, {}))[1])
    cur = FakeCursor(_state(peak=200_000.0), (205_000.0,))
    wired['install'](cur, 100_000.0)
    with caplog.at_level('INFO', logger=ab.logger.name):
        assert ab.run_once(session_date=SESSION) == 0
    assert closed == ['AAPL']                        # SPY untouched
    line = [m for m in (r.getMessage() for r in caplog.records)
            if m.startswith('[account_breaker] ')][-1]
    assert ' armed ' in line and 'halted=1' in line and 'pending=0' in line

    # Deviation 4 (module docstring): supplement item 1 requires the halt
    # latch to be persisted+committed BEFORE the flatten and again after —
    # two UPDATE statements, not one.
    updates = cur.sql_matching('UPDATE account_breaker_state')
    assert len(updates) == 2

    # Fix round 1, item 4 test 2 (latch-first pinned): the FIRST write must
    # already carry halted=True and pending_flatten=True — proving the latch
    # lands BEFORE any broker action, not merely that two writes happened.
    _sql0, params0 = updates[0]
    assert params0[0] is True and params0[6] is True

    # Inspect the final (post-flatten) row.
    _sql, params = updates[-1]
    assert params[0] is True
    # Deviation 3 (amended by C1 amendment 2a / F3): this fixture has no alpha-P&L
    # tables, so the ARMED tick is a fallback tick and the drawdown rule is
    # SKIPPED (dd_nav -70.5 % is never fed to it); only daily_loss (-51.2 %) fires.
    assert params[1] == 'daily_loss'
    assert isinstance(params[2], datetime)
    assert len(cur.sql_matching('INSERT INTO circuit_breaker_fires')) == 1

    # posts[0] is the once-per-30-min degraded notice; the HALTED post follows.
    assert 'DRAWDOWN rule is' in wired['posts'][0][1]
    channel, msg = [p for p in wired['posts'] if 'HALTED' in p[1]][0]
    assert channel == 'trade-reports'
    assert 'OPENCLAW_ACCOUNT_BREAKER_REARM=' in msg
    assert 'PROCESS-WIDE' in msg           # supplement item 10
    # Fix round 1, item 1 test: the HALTED post must say the option book
    # stays open — _is_equity_symbol/_OCC_RE exclude OCC legs from the
    # flatten by design, and that must not be left implicit to the operator.
    assert 'option book stays open' in msg
    assert 'OCC' in msg


def test_already_halted_retries_the_pending_flatten_only(monkeypatch, wired):
    monkeypatch.setenv(ab.ARM_ENV, '1')
    closed = []
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (closed.append(sym), (True, {}))[1])
    breached = datetime(2026, 9, 16, 17, 42, tzinfo=timezone.utc)
    cur = FakeCursor(_state(halted=True, peak=200_000.0, breached_at=breached,
                            reason='drawdown', pending=True), (205_000.0,))
    wired['install'](cur, 100_000.0)
    assert ab.run_once(session_date=SESSION) == 0
    assert closed == ['AAPL']
    (_sql, params), = cur.sql_matching('UPDATE account_breaker_state')
    assert params[0] is True and params[3] == 200_000.0     # peak NOT moved
    assert params[6] is False                                # pending cleared


def test_operator_token_rearms_then_evaluates_normally(monkeypatch, wired):
    breached = datetime(2026, 9, 16, 17, 42, tzinfo=timezone.utc)
    monkeypatch.setenv(ab.REARM_ENV, breached.isoformat())
    cur = FakeCursor(_state(halted=True, peak=200_000.0, breached_at=breached,
                            reason='drawdown'), (205_000.0,))
    wired['install'](cur, 200_000.0)
    assert ab.run_once(session_date=SESSION) == 0
    rearm, = [c for c in cur.sql_matching('UPDATE account_breaker_state')
              if 'rearmed_at = NOW()' in c[0]]
    assert rearm[1][0] == 159_000.0        # peak reset to the current alpha NAV


# ── supplement item 1: pre-flatten persistence failure ──────────────────────

def test_persist_failure_before_flatten_takes_no_broker_action(monkeypatch, wired):
    """Supplement item 1 (CRITICAL): if the pre-flatten halt-latch write
    fails, run_once must not submit any broker action, must not post
    'HALTED' as accomplished fact, and must return 1 so the next tick
    re-evaluates cleanly."""
    monkeypatch.setenv(ab.ARM_ENV, '1')
    monkeypatch.setattr(
        rl, '_close_symbol',
        lambda *_a, **_k: pytest.fail(
            'must not submit when the halt latch failed to persist'))
    cur = FailFirstUpdateCursor(_state(peak=200_000.0), (205_000.0,))
    wired['install'](cur, 100_000.0)
    assert ab.run_once(session_date=SESSION) == 1
    assert cur.sql_matching('INSERT INTO circuit_breaker_fires') == []
    # (the F3 degraded-notice post precedes it: no alpha-P&L tables in this fixture)
    posts = [p for p in wired['posts'] if 'DRAWDOWN rule is' not in p[1]]
    assert len(posts) == 1
    channel, msg = posts[0]
    assert channel == 'trade-reports'
    assert 'NOT latched' in msg
    assert 'HALTED' not in msg


def test_commit_failure_before_flatten_takes_no_broker_action(monkeypatch, wired):
    """Fix round 1, item 4 test 1: the OTHER half of supplement item 1's
    gate — save_state's pre-flatten UPDATE can land cleanly while the
    commit that makes it durable still fails. A raising conn.commit() must
    be treated exactly like a raising UPDATE: no broker action, a
    non-HALTED warning post instead, return 1."""
    monkeypatch.setenv(ab.ARM_ENV, '1')
    monkeypatch.setattr(
        rl, '_close_symbol',
        lambda *_a, **_k: pytest.fail(
            'must not submit when the halt latch failed to commit'))
    cur = FakeCursor(_state(peak=200_000.0), (205_000.0,))
    conn = RaisingCommitConn(cur)
    monkeypatch.setattr(ab.psycopg2, 'connect', lambda *_a, **_k: conn)
    monkeypatch.setattr(at, '_fetch_account_state', lambda sess: {'equity': 100_000.0})
    assert ab.run_once(session_date=SESSION) == 1
    assert cur.sql_matching('INSERT INTO circuit_breaker_fires') == []
    # (the F3 degraded-notice post precedes it: no alpha-P&L tables in this fixture)
    posts = [p for p in wired['posts'] if 'DRAWDOWN rule is' not in p[1]]
    assert len(posts) == 1
    channel, msg = posts[0]
    assert channel == 'trade-reports'
    assert 'NOT latched' in msg
    assert 'HALTED' not in msg


# ── F-5: escalation after N consecutive pending ticks ────────────────────────

def test_flatten_escalation_posts_once_after_the_configured_tick_threshold(monkeypatch, wired):
    """F-5 (deferred from Task 5's fix round 1 item 5, folded into Task 7 by
    the supplement): the retry counter escalates to the operator exactly
    once when it crosses the configured threshold (flatten_attempts,
    migration 158) — never re-posting on every subsequent still-pending
    tick."""
    monkeypatch.setenv(ab.ARM_ENV, '1')
    monkeypatch.setattr(ab, 'FLATTEN_ESCALATE_AFTER', 2)
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (False, {'error': 'still rejected'}))
    breached = datetime(2026, 9, 16, 17, 42, tzinfo=timezone.utc)
    cur = FakeCursor(_state(halted=True, peak=200_000.0, breached_at=breached,
                            reason='drawdown', pending=True), (205_000.0,))
    cur._queue.append((1,))     # prior flatten_attempts: this is the 2nd consecutive pending tick
    wired['install'](cur, 100_000.0)
    assert ab.run_once(session_date=SESSION) == 0
    assert len(wired['posts']) == 1
    channel, msg = wired['posts'][0]
    assert channel == 'trade-reports'
    assert 'still PENDING' in msg
    # Fix round 1, item 2: flat['tickers'] is the ATTEMPTED set, never the
    # residual/still-open set (residual identities aren't tracked at all) —
    # the old "Residual symbols: [...]" wording must be gone entirely.
    assert 'Residual symbols' not in msg
    assert "attempted: ['AAPL']" in msg
    assert 'fail=1 partial=0 pending=1' in msg
    assert 'residual identities are not tracked' in msg
    # Fix round 1, item 1: the escalation post must say the option book
    # stays open too.
    assert 'option book stays open' in msg
    assert 'OCC' in msg
    (_sql, params), = cur.sql_matching('UPDATE account_breaker_state')
    assert params[7] == 2              # flatten_attempts persisted as 2


def test_flatten_escalation_does_not_repost_below_threshold(monkeypatch, wired):
    monkeypatch.setenv(ab.ARM_ENV, '1')
    monkeypatch.setattr(ab, 'FLATTEN_ESCALATE_AFTER', 12)
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (False, {'error': 'still rejected'}))
    breached = datetime(2026, 9, 16, 17, 42, tzinfo=timezone.utc)
    cur = FakeCursor(_state(halted=True, peak=200_000.0, breached_at=breached,
                            reason='drawdown', pending=True), (205_000.0,))
    cur._queue.append((1,))
    wired['install'](cur, 100_000.0)
    assert ab.run_once(session_date=SESSION) == 0
    assert wired['posts'] == []


# ── format_line: peak=None (supplement item 5) ───────────────────────────────

def test_format_line_renders_peak_and_dd_as_na_when_none():
    st = {'peak': None, 'dd': None, 'daily': None, 'rule': 'none', 'breach': False}
    line = ab.format_line('shadow', equity=1.0, bench_mv=0.0, alpha=1.0, st=st,
                          open_equity=0.0, open_src='equity', halted=False)
    assert ' peak=n/a dd=n/a ' in line


# ── cron wiring (source assertion; no node runtime needed) ──────────────────

def test_account_breaker_spawns_inside_the_existing_five_minute_cron():
    js = CRON.read_text()
    marker = "cron.schedule('*/5 9-16 * * 1-5'"
    assert js.count(marker) == 1, 'the 5-min RTH cron must stay a single block'
    start = js.index(marker)
    end = js.index("}, { timezone: 'America/New_York' });", start)
    block = js[start:end]
    assert "'src/execution/position_circuit_breaker.py'" in block
    assert "'src/execution/account_breaker.py'" in block
    assert 'account_breaker_' in block            # its own dated log file


def test_no_new_cron_expression_was_introduced():
    js = CRON.read_text()
    schedules = re.findall(r"cron\.schedule\('([^']+)'", js)
    assert schedules.count('*/5 9-16 * * 1-5') == 1
