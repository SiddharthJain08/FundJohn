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
    (orphan count), calls recorded verbatim for SQL-text assertions."""

    def __init__(self, rows=(), orphan_count=0):
        self.rows = list(rows)
        self.orphan_count = orphan_count
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((' '.join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return (self.orphan_count,)

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

def test_ahsx_coid_scores_against_the_stop_not_the_target():
    """ahsx_ exits are marketable LIMITS emulating a stop (classify_exit_fills
    tags them 'ah_exit'); typing alone would score them against target_1."""
    assert ar.exit_level_kind('limit', 'ahsx_AAA_1') == 'stop'


def test_ahtp_coid_scores_against_the_target():
    assert ar.exit_level_kind('limit', 'ahtp_AAA_1') == 'target'


def test_stop_and_stop_limit_and_trailing_stop_order_types_score_against_the_stop():
    assert ar.exit_level_kind('stop', None) == 'stop'
    assert ar.exit_level_kind('STOP_LIMIT', '') == 'stop'
    assert ar.exit_level_kind('trailing_stop', None) == 'stop'


def test_a_plain_limit_leg_scores_against_the_target():
    assert ar.exit_level_kind('limit', 'oc_AAA_1_tp') == 'target'


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
    plan = ar.plan_exit_slippage([_row('limit', 'ahsx_AAA_1', 9.95)])
    assert abs(plan[0][2] - 50.0) < 1e-6


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
    cur = _Cursor(rows=[], orphan_count=1)
    assert ar.backfill_exit_slippage(cur, '2026-09-11') == 0
    assert cur.updates() == []
    out = capsys.readouterr().out
    assert 'n=0' in out
    assert '1 orphan' in out


def test_backfill_logs_one_line_every_run_including_a_zero_plan(capsys):
    cur = _Cursor(rows=[], orphan_count=0)
    assert ar.backfill_exit_slippage(cur, '2026-09-11') == 0
    out = capsys.readouterr().out
    assert 'exit slippage' in out
    assert 'n=0' in out
    assert '0 orphan' in out


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
