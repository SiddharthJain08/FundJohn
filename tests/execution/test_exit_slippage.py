"""B2 exit-leg slippage: what the exit actually cost vs the level it aimed at.

Sign convention matches execution_signals.fill_slippage_bps (migration 145):
dir_sign * (level - price) / level * 1e4, dir_sign = +1 LONG / -1 SHORT, so a
POSITIVE number always means "worse than the level we wanted".
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import psycopg2.errors

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import alpaca_reconcile as ar  # noqa: E402


class _Cursor:
    """Fake cursor: scripted fetchall() (candidate rows) and fetchone()
    (orphan count / ah count — dispatched independently, fix round 2, item 2:
    backfill_exit_slippage now issues TWO COUNT queries, and orphans=/
    ah_unmatched= must be separately assertable, not coupled to one shared
    scripted value), calls recorded verbatim for SQL-text assertions.

    Dispatch: _EXIT_ORPHAN_COUNT_SQL is the only one of the two with a
    'NOT EXISTS' clause (the alpaca_submissions client_order_id match), so
    fetchone() keys off the most recently executed statement's text rather
    than call order — this also keeps _HonestCursor (below) working
    unmodified, since it appends to the same self.calls list."""

    def __init__(self, rows=(), orphan_count=0, ah_count=None):
        self.rows = list(rows)
        self.orphan_count = orphan_count
        self.ah_count = orphan_count if ah_count is None else ah_count
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((' '.join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        last = self.calls[-1][0] if self.calls else ''
        if 'NOT EXISTS' in last:
            return (self.orphan_count,)
        return (self.ah_count,)

    def updates(self):
        return [c for c in self.calls if c[0].startswith('UPDATE signal_pnl')]


class _HonestCursor(_Cursor):
    """Same fake, plus the honesty contract test_broker_fills_ingest.py's
    _ReconcileCursor uses: after `trigger(norm_sql)` matches once and raises
    `raise_exc`, EVERY subsequent execute() raises
    psycopg2.errors.InFailedSqlTransaction until a 'ROLLBACK TO SAVEPOINT'
    statement clears it — the same thing a real aborted Postgres transaction
    enforces. Without this, a test could pass even if production code forgot
    the ROLLBACK TO SAVEPOINT line entirely (the trap fires once, everything
    after is silently accepted)."""

    def __init__(self, *, trigger, raise_exc, **kwargs):
        super().__init__(**kwargs)
        self._trigger = trigger
        self._raise_exc = raise_exc
        self._aborted = False

    def execute(self, sql, params=None):
        norm = ' '.join(sql.split())
        if norm.startswith('ROLLBACK TO SAVEPOINT'):
            self._aborted = False
            self.calls.append((norm, params))
            return
        if self._aborted:
            raise psycopg2.errors.InFailedSqlTransaction(
                'current transaction is aborted, commands ignored until end of transaction block')
        if self._trigger is not None and self._trigger(norm):
            self._trigger = None  # raise once, like a real missing-table error
            self._aborted = True
            raise self._raise_exc
        self.calls.append((norm, params))


# ── which level was this exit aiming at ────────────────────────────────────

def test_exit_level_kind_no_longer_special_cases_ahsx_prefix():
    """Fix round 2, item 1: afterhours_tp.py:349/:352 submits BOTH
    stop_breach and tp_reach exits through the same ahsx_{sym}_{ts}
    client_order_id, so a prefix match alone cannot tell which level an
    ahsx_ fill was aiming at (the old hardcoded 'stop' scored a tp_reach
    fill near target_1 thousands of bps "off"). That disambiguation now
    happens upstream via pick_ah_level (called from plan_exit_slippage);
    exit_level_kind itself falls through to its order_type default for an
    ahsx_ coid, since nothing routes an ahsx_ row through it anymore."""
    assert ar.exit_level_kind('limit', 'ahsx_AAA_1') == 'target'


def test_ahtp_coid_scores_against_the_target():
    assert ar.exit_level_kind('limit', 'ahtp_AAA_1') == 'target'


def test_stop_and_stop_limit_and_trailing_stop_order_types_score_against_the_stop():
    assert ar.exit_level_kind('stop', None) == 'stop'
    assert ar.exit_level_kind('STOP_LIMIT', '') == 'stop'
    assert ar.exit_level_kind('trailing_stop', None) == 'stop'


def test_a_plain_limit_leg_scores_against_the_target():
    assert ar.exit_level_kind('limit', 'oc_AAA_1_tp') == 'target'


# ── pick_ah_level: disambiguate an ahsx_ fill by proximity (fix round 2,
#    item 1) ─────────────────────────────────────────────────────────────

def test_pick_ah_level_stop_breach_shaped_fill_scores_the_stop():
    """A fill priced near stop_loss looks like afterhours_tp.py's
    stop_breach branch (:349)."""
    assert ar.pick_ah_level(9.95, stop_loss=10.0, target_1=12.0) == ('stop', 10.0)


def test_pick_ah_level_tp_reach_shaped_fill_scores_the_target():
    """A fill priced near target_1 looks like afterhours_tp.py's tp_reach
    branch (:352) — the whole point of this fix is that it must NOT be
    scored against stop_loss just because it came through an ahsx_ coid."""
    assert ar.pick_ah_level(11.94, stop_loss=10.0, target_1=12.0) == ('target', 12.0)


def test_pick_ah_level_only_stop_nonnull_uses_the_stop():
    assert ar.pick_ah_level(50.0, stop_loss=10.0, target_1=None) == ('stop', 10.0)


def test_pick_ah_level_only_target_nonnull_uses_the_target():
    assert ar.pick_ah_level(50.0, stop_loss=None, target_1=12.0) == ('target', 12.0)


def test_pick_ah_level_neither_level_nonnull_returns_none():
    assert ar.pick_ah_level(10.0, stop_loss=None, target_1=None) is None


def test_pick_ah_level_exact_tie_favors_the_stop():
    assert ar.pick_ah_level(11.0, stop_loss=10.0, target_1=12.0) == ('stop', 10.0)


def test_pick_ah_level_bad_price_returns_none():
    assert ar.pick_ah_level('nope', stop_loss=10.0, target_1=12.0) is None


# ── signed adverse-positive bp ─────────────────────────────────────────────

def test_long_exit_below_its_level_is_positive_adverse():
    assert abs(ar.exit_slippage_bps('LONG', 10.0, 9.95) - 50.0) < 1e-6


def test_long_exit_above_its_level_is_favourable_negative():
    assert abs(ar.exit_slippage_bps('LONG', 10.0, 10.05) + 50.0) < 1e-6


def test_short_exit_above_its_level_is_positive_adverse():
    assert abs(ar.exit_slippage_bps('SHORT', 10.0, 10.05) - 50.0) < 1e-6


def test_short_exit_below_its_level_is_favourable_negative():
    assert abs(ar.exit_slippage_bps('SHORT', 10.0, 9.95) + 50.0) < 1e-6


def test_missing_or_nonpositive_inputs_return_none():
    assert ar.exit_slippage_bps('LONG', None, 10.0) is None
    assert ar.exit_slippage_bps('LONG', 0.0, 10.0) is None
    assert ar.exit_slippage_bps('LONG', 10.0, None) is None
    assert ar.exit_slippage_bps('LONG', 10.0, 'nope') is None


# ── plan_exit_slippage ─────────────────────────────────────────────────────

def _row(otype, coid, price, direction='LONG', stop=10.0, tgt=12.0,
         sig='sig-1', pnl=date(2026, 9, 11), aid='act-3'):
    return (aid, otype, coid, price, sig, direction, stop, tgt, pnl)


def test_plan_uses_the_stop_for_a_stop_leg():
    plan = ar.plan_exit_slippage([_row('stop', 'oc_1_sl', 9.95)])
    assert plan == [('sig-1', date(2026, 9, 11), 50.0)]


def test_plan_uses_the_target_for_a_tp_leg():
    plan = ar.plan_exit_slippage([_row('limit', 'oc_1_tp', 11.94)])
    assert plan[0][0] == 'sig-1'
    assert abs(plan[0][2] - 50.0) < 1e-6


def test_plan_uses_the_stop_for_an_ahsx_limit():
    """Default _row() price (9.95) is stop-breach-shaped against stop=10.0,
    target=12.0 — proximity picks the stop, same result the old hardcoded
    'ahsx_ always scores stop' behavior gave for this particular price."""
    plan = ar.plan_exit_slippage([_row('limit', 'ahsx_AAA_1', 9.95)])
    assert abs(plan[0][2] - 50.0) < 1e-6


def test_plan_uses_the_target_for_a_tp_reach_shaped_ahsx_fill():
    """Fix round 2, item 1: an ahsx_ fill priced near target_1 (a tp_reach
    exit) must score against the target, not get misattributed to the stop
    thousands of bps away just because of the ahsx_ prefix."""
    plan = ar.plan_exit_slippage([_row('limit', 'ahsx_AAA_1', 11.94, stop=10.0, tgt=12.0)])
    assert abs(plan[0][2] - 50.0) < 1e-6  # small bps, scored against the target


def test_plan_drops_an_ahsx_row_with_no_usable_level():
    assert ar.plan_exit_slippage(
        [_row('limit', 'ahsx_AAA_1', 9.95, stop=None, tgt=None)]) == []


def test_plan_skips_rows_with_no_usable_level():
    assert ar.plan_exit_slippage([_row('stop', 'oc_1_sl', 9.95, stop=None)]) == []


# ── fix round 1, item 1: candidate SQL scopes to matching ticker + opposite
#    side, so an mleg option leg (OCC ticker != underlying) or a same-side
#    add-on fill can never be scored as an exit. Not DB-testable per the
#    "never a real Postgres" constraint, so asserted as SQL text present on
#    both UNION branches. ──────────────────────────────────────────────────

def test_candidate_sql_requires_the_fills_own_ticker_to_match_the_submission():
    """Guards the mleg-leg case: a bracket's OCC-symbol option leg has
    bf.ticker != the underlying ticker on alpaca_submissions and must not
    join through as an equity exit."""
    assert 'bf.ticker = s.ticker' in ar._EXIT_CANDIDATE_SQL


def test_candidate_sql_requires_opposite_side_on_both_branches():
    """A same-side add-on fill on an open bracket is not an exit."""
    sql = ar._EXIT_CANDIDATE_SQL
    assert sql.count("bf.side = 'sell'") == 2
    assert sql.count("bf.side = 'buy'") == 2


# ── fix round 2, item 3: branch 2's direction literals must mirror branch
#    1's normalized UPPER(direction) IN (...) form, not a raw ('long'/
#    'short') string comparison ────────────────────────────────────────────

def test_candidate_sql_normalizes_direction_literals_on_both_branches():
    sql = ar._EXIT_CANDIDATE_SQL
    assert sql.count("(UPPER(es.direction) IN (") == 1
    assert sql.count("(UPPER(s2.direction) IN (") == 1
    assert sql.count("NOT IN ('LONG','BUY','BUY_VOL')") == 2
    assert "s2.direction = 'long'" not in sql
    assert "s2.direction = 'short'" not in sql


# ── fix round 1, item 2: per-signal idempotency, not just "latest row is
#    NULL" ────────────────────────────────────────────────────────────────

def test_candidate_sql_is_per_signal_idempotent_via_not_exists():
    """A fill already attributed to an OLDER signal_pnl row must not be
    re-planned just because a newer row got upserted on a later run_date."""
    sql = ar._EXIT_CANDIDATE_SQL
    assert sql.count('NOT EXISTS') == 2  # one per UNION branch
    assert sql.count('p.exit_slippage_bps IS NOT NULL') == 2
    # the old "only the latest row is NULL" gate is gone
    assert 'sp.exit_slippage_bps' not in sql


# ── fix round 1, item 3: top-level after-hours exits (no parent_order_id) ──

def test_candidate_sql_has_a_second_branch_for_top_level_ah_exits():
    sql = ar._EXIT_CANDIDATE_SQL
    assert 'UNION ALL' in sql
    assert 'parent_order_id IS NULL' in sql
    assert "'ahsx_'" in sql
    assert "'ahtp_'" in sql


def test_candidate_sql_bounds_branch_2_by_filled_at_before_the_lateral():
    """The parent/prefix/filled_at filters on broker_fills sit in a subquery
    ahead of the LATERAL join, not in the outer WHERE after it — otherwise
    the lateral (a per-row submission lookup) would run once per row in the
    whole append-only broker_fills history instead of once per row in the
    lookback window."""
    sql = ar._EXIT_CANDIDATE_SQL
    from_bf_subquery = sql.split('UNION ALL')[1].split('JOIN LATERAL')[0]
    assert 'parent_order_id IS NULL' in from_bf_subquery
    assert 'filled_at >=' in from_bf_subquery


def test_ahsx_top_level_exit_is_scored_against_the_stop():
    """Simulates a row the branch-2 UNION arm would hand plan_exit_slippage:
    a parentless ahsx_ fill joined to its signal's stop level."""
    cur = _Cursor(rows=[_row('limit', 'ahsx_AAA_1', 9.95)])
    assert ar.backfill_exit_slippage(cur, '2026-09-11') == 1
    sql, params = cur.updates()[0]
    assert abs(params[0] - 50.0) < 1e-6


def test_parentless_fill_with_no_ah_prefix_is_skipped_and_counted(capsys):
    """Neither UNION branch can attribute it (no parent, no ah prefix) — it
    never reaches plan_exit_slippage, but the orphan count still surfaces
    it in the log line."""
    cur = _Cursor(rows=[], orphan_count=1, ah_count=0)
    assert ar.backfill_exit_slippage(cur, '2026-09-11') == 0
    assert cur.updates() == []
    out = capsys.readouterr().out
    assert 'n=0' in out
    assert 'orphans=1' in out


def test_backfill_logs_one_line_every_run_including_a_zero_plan(capsys):
    cur = _Cursor(rows=[], orphan_count=0, ah_count=0)
    assert ar.backfill_exit_slippage(cur, '2026-09-11') == 0
    out = capsys.readouterr().out
    assert 'exit slippage' in out
    assert 'n=0' in out
    assert 'orphans=0' in out
    assert 'ah_unmatched=0' in out


# ── fix round 2, item 2: orphan count predicate + ah_unmatched ─────────────

def test_orphan_count_sql_excludes_fills_with_a_matching_submission_coid():
    """An entry fill also has parent_order_id IS NULL (it IS the parent
    order, not a leg) — without the NOT EXISTS(...) submission-match check,
    every entry fill in the lookback window would inflate `orphans=` even
    though it's not a genuine attribution gap. An entry's own
    client_order_id always matches its own alpaca_submissions row (TEXT NOT
    NULL UNIQUE since migration 043), so NOT EXISTS correctly excludes it."""
    sql = ar._EXIT_ORPHAN_COUNT_SQL
    assert 'NOT EXISTS' in sql
    assert 'a.client_order_id = bf.client_order_id' in sql


def test_ah_count_sql_counts_ah_prefixed_parentless_fills_regardless_of_match():
    """Unlike the orphan count, this one does NOT check for a submission
    match — it's the raw count backfill_exit_slippage compares against how
    many branch-2 rows actually made it into the plan."""
    sql = ar._EXIT_AH_COUNT_SQL
    assert 'parent_order_id IS NULL' in sql
    assert "'ahsx_'" in sql
    assert "'ahtp_'" in sql
    assert 'NOT EXISTS' not in sql


def test_ah_fill_with_no_branch2_match_shows_as_ah_unmatched(capsys):
    """An ah-prefixed fill the COUNT query sees but that never produced a
    branch-2 plan row (submission out of the lookback window, no matching
    signal, or no usable level) must surface as ah_unmatched=, not vanish
    silently."""
    cur = _Cursor(rows=[], orphan_count=0, ah_count=1)
    assert ar.backfill_exit_slippage(cur, '2026-09-11') == 0
    out = capsys.readouterr().out
    assert 'ah_unmatched=1' in out


def test_ah_unmatched_never_goes_negative_when_every_ah_row_lands(capsys):
    """ah_count and the plan's branch-2 rows can both legitimately be
    non-zero and equal (every ah fill the COUNT sees also landed in the
    plan) — must clamp at 0, not print a negative count."""
    cur = _Cursor(rows=[_row('limit', 'ahsx_AAA_1', 9.95)], orphan_count=0, ah_count=1)
    assert ar.backfill_exit_slippage(cur, '2026-09-11') == 1
    out = capsys.readouterr().out
    assert 'ah_unmatched=0' in out


# ── backfill_exit_slippage ─────────────────────────────────────────────────

def test_backfill_updates_only_null_rows_and_releases_its_savepoint():
    cur = _Cursor([_row('stop', 'oc_1_sl', 9.95)])
    assert ar.backfill_exit_slippage(cur, '2026-09-11') == 1
    sql, params = cur.updates()[0]
    assert 'exit_slippage_bps IS NULL' in sql
    assert params == (50.0, 'sig-1', date(2026, 9, 11))
    assert any(c[0] == 'RELEASE SAVEPOINT sp_exit_slip' for c in cur.calls)


def test_backfill_dry_run_plans_but_writes_nothing():
    cur = _Cursor([_row('stop', 'oc_1_sl', 9.95)])
    assert ar.backfill_exit_slippage(cur, '2026-09-11', dry_run=True) == 1
    assert cur.updates() == []


def test_backfill_rolls_back_and_returns_zero_on_a_missing_table():
    """Trigger on the very first SELECT (the candidate query) — covers the
    "migration 155 not applied yet" case, where nothing downstream ever
    runs."""
    cur = _HonestCursor(
        trigger=lambda norm: norm.startswith('SELECT'),
        raise_exc=psycopg2.errors.UndefinedTable('relation "broker_fills" does not exist'),
    )
    assert ar.backfill_exit_slippage(cur, '2026-09-11') == 0
    assert any(c[0] == 'ROLLBACK TO SAVEPOINT sp_exit_slip' for c in cur.calls)


def test_backfill_rolls_back_a_mid_flight_failure_on_the_orphan_count_query():
    """Trigger on the SECOND query (the orphan COUNT), after the candidate
    SELECT and the UPDATE have already succeeded and been recorded. This is
    the genuine honesty-contract exercise: if backfill_exit_slippage ever
    forgot the ROLLBACK TO SAVEPOINT line, the _aborted flag set by the
    trigger would still be live when RELEASE SAVEPOINT ran next, and
    _HonestCursor would raise InFailedSqlTransaction — turning a silent bug
    into a hard test failure instead of a tautology."""
    cur = _HonestCursor(
        rows=[_row('stop', 'oc_1_sl', 9.95)],
        trigger=lambda norm: norm.startswith('SELECT COUNT(*)'),
        raise_exc=psycopg2.errors.UndefinedTable('relation "broker_fills" does not exist'),
    )
    assert ar.backfill_exit_slippage(cur, '2026-09-11') == 0
    assert len(cur.updates()) == 1  # the UPDATE from the plan ran before the failure
    assert any(c[0] == 'ROLLBACK TO SAVEPOINT sp_exit_slip' for c in cur.calls)
    assert any(c[0] == 'RELEASE SAVEPOINT sp_exit_slip' for c in cur.calls)
