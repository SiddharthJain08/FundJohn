#!/usr/bin/env python3
"""
SP-6 Phase C — rolled_continuation stat-exclusion regression.

Commit 1e096f2 introduced D1 roll-closes: when a held position rolls to a new
continuation row, the spent row is closed with
`signal_pnl.close_reason='rolled_continuation'`, days_held≈1, and a real
realized_pnl_pct.  These are accounting *segments* of ONE ongoing position, NOT
trades.  Unfiltered they inflate closed-trade counts, dilute win-rate, and
collapse avg_days_held toward 1 — corrupting exactly the metrics used to verify
the §13 fix.  The rows must STAY (audit + pnl continuity); only the STAT
consumers must exclude them.

This test pins three invariants for every stat consumer that was patched:
  1. the rolled_continuation row is EXCLUDED from counts / averages,
  2. a NULL-close_reason closed row is STILL INCLUDED — the SQL NULL trap
     (`close_reason <> 'rolled_continuation'` would wrongly drop NULLs;
      `IS DISTINCT FROM` keeps them),
  3. a real-reason closed row (stop_loss / target_1) is included.

Throwaway-DB pattern: schema-only pg_dump of the production `openclaw` DB into a
brand-new `openclaw_metrics_test` DB on the same postgres container, seeded with
the minimal FK chain (default workspace → approved strategy_registry row →
execution_signals → signal_pnl).  POSTGRES_URI is overridden for THIS process
only.  The test DB is DROPped at teardown.  Production POSTGRES_URI never
receives a write.

The two JS sites (src/channels/api/server.js, src/engine/daily-health-digest.js)
cannot be pytested, so their EXACT SQL is duplicated here (with the source
file:line it was copied from) and executed through the same test DB.  Duplication
is acceptable for a stat-exclusion pin; staleness risk is flagged at each block —
if the .js SQL changes, update the copy here.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid

import psycopg2
import pytest

# Make `import execution.*` / `import research.*` / `import maintenance.*` work.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

CONTAINER = "openclaw-postgres"
PROD_DB = "openclaw"
TEST_DB = "openclaw_metrics_test"

ROLL = "rolled_continuation"


def _prod_uri() -> str:
    """Production POSTGRES_URI (read-only here — used only to derive creds and
    to schema-dump). Never written to."""
    uri = os.environ.get("POSTGRES_URI", "")
    if not uri:
        # Fall back to .env on the box.
        env_path = os.path.join(REPO_ROOT, ".env")
        with open(env_path) as fh:
            for line in fh:
                if line.startswith("POSTGRES_URI="):
                    uri = line.split("=", 1)[1].strip()
                    break
    if not uri:
        pytest.skip("POSTGRES_URI not available")
    return uri


def _parse_uri(uri: str) -> dict:
    m = re.match(
        r"postgres(?:ql)?://([^:]+):([^@]+)@([^:/]+):(\d+)/(\w+)", uri
    )
    if not m:
        pytest.skip(f"could not parse POSTGRES_URI: {uri[:30]}...")
    user, pw, host, port, _db = m.groups()
    return {"user": user, "pw": pw, "host": host, "port": port}


def _test_uri(creds: dict) -> str:
    return (
        f"postgresql://{creds['user']}:{creds['pw']}@"
        f"{creds['host']}:{creds['port']}/{TEST_DB}"
    )


def _docker_psql(creds: dict, db: str, sql: str) -> None:
    subprocess.run(
        ["docker", "exec", "-e", f"PGPASSWORD={creds['pw']}", CONTAINER,
         "psql", "-U", creds["user"], "-d", db, "-c", sql],
        check=True, capture_output=True,
    )


@pytest.fixture(scope="module")
def test_db():
    """Build a throwaway DB, seed it, hand back its URI, drop it at teardown."""
    prod = _prod_uri()
    creds = _parse_uri(prod)

    # Reachability preflight — skip cleanly if docker/postgres unavailable.
    try:
        subprocess.run(["docker", "exec", CONTAINER, "true"],
                       check=True, capture_output=True)
    except Exception:
        pytest.skip("postgres docker container not reachable")

    # 1. Fresh DB from a schema-only dump of production.
    _docker_psql(creds, "postgres",
                 f"DROP DATABASE IF EXISTS {TEST_DB};")
    _docker_psql(creds, "postgres", f"CREATE DATABASE {TEST_DB};")
    subprocess.run(
        ["docker", "exec", "-e", f"PGPASSWORD={creds['pw']}", CONTAINER, "sh",
         "-c",
         f"pg_dump -U {creds['user']} --schema-only --no-owner "
         f"--no-privileges {PROD_DB} | psql -U {creds['user']} -d {TEST_DB}"],
        check=True, capture_output=True,
    )

    uri = _test_uri(creds)
    _seed(uri)
    try:
        yield uri
    finally:
        # Drop the throwaway DB — terminate any lingering connections first.
        try:
            _docker_psql(
                creds, "postgres",
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{TEST_DB}' AND pid <> pg_backend_pid();")
        except Exception:
            pass
        _docker_psql(creds, "postgres", f"DROP DATABASE IF EXISTS {TEST_DB};")


# Stable ids so the per-signal LATERAL joins resolve deterministically.
WS_ID = "cad1a456-0b65-40ae-8be6-3530e36c53c2"  # 'default'
STRAT = "S_roll_test"
TICKER = "ROLLTST"
SIG_NULL = "11111111-1111-1111-1111-111111111111"
SIG_REAL = "22222222-2222-2222-2222-222222222222"
SIG_ROLL = "33333333-3333-3333-3333-333333333333"


def _seed(uri: str) -> None:
    """Seed the minimal FK chain + three CLOSED signal_pnl rows.

    Three distinct execution_signals (one per pnl row) so the per-signal
    LATERAL joins in send_report / backfill_oue resolve one closed row each.
    All three execution_signals are status='closed' so they pass the outer
    `WHERE es.status='closed'`.  oue_kind is left NULL so backfill_oue's
    `oue_kind IS NULL` filter selects them.
    """
    conn = psycopg2.connect(uri)
    conn.autocommit = True
    cur = conn.cursor()
    # default workspace (gen id matches production's 'default' uuid).
    cur.execute(
        "INSERT INTO workspaces (id, name) VALUES (%s, 'default') "
        "ON CONFLICT (id) DO NOTHING", (WS_ID,))
    # approved strategy_registry row.
    cur.execute(
        "INSERT INTO strategy_registry (id, name, implementation_path, status) "
        "VALUES (%s, %s, %s, 'approved') ON CONFLICT (id) DO NOTHING",
        (STRAT, "Roll Test", "src/strategies/implementations/S_roll_test.py"))

    # Same ticker on all three (the ticker-alpha test groups by ticker), so we
    # vary signal_date to satisfy the UNIQUE (strategy_id, signal_date, ticker,
    # direction) constraint on execution_signals. All dates are recent so the
    # 7/30/42-day windowed aggregates still see every row.
    rows = [
        (SIG_NULL, None,        5.0,  3, 0),   # NULL close_reason — must STAY
        (SIG_REAL, "stop_loss", -2.0, 4, 1),   # real close_reason — must STAY
        (SIG_ROLL, ROLL,         8.0, 1, 2),   # roll segment       — EXCLUDED
    ]
    for sig_id, reason, realized, days, day_off in rows:
        cur.execute(
            "INSERT INTO execution_signals "
            "(id, strategy_id, workspace_id, signal_date, ticker, direction, "
            " status, oue_kind) "
            "VALUES (%s,%s,%s, CURRENT_DATE - %s::int, %s,'LONG','closed', NULL)",
            (sig_id, STRAT, WS_ID, day_off, TICKER))
        # pnl_date / closed_at stay at CURRENT_DATE for ALL three rows so
        # send_report._load_closed_positions (keyed `pnl_date::date = run_date`)
        # and the 7/30/42-day windowed aggregates pick up every row. Only
        # execution_signals.signal_date varied above (for the UNIQUE key).
        cur.execute(
            "INSERT INTO signal_pnl "
            "(signal_id, strategy_id, workspace_id, pnl_date, status, "
            " close_reason, realized_pnl_pct, days_held, closed_at) "
            "VALUES (%s,%s,%s, CURRENT_DATE, 'closed', %s, %s, %s, CURRENT_DATE)",
            (sig_id, STRAT, WS_ID, reason, realized, days))
    cur.close()
    conn.close()


# ── helpers ───────────────────────────────────────────────────────────────

def _scalar(uri: str, sql: str, params=None):
    conn = psycopg2.connect(uri)
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        return cur.fetchone()
    finally:
        conn.close()


# ── Python sites ────────────────────────────────────────────────────────────

def test_forensics_excludes_roll(test_db, monkeypatch):
    """src/research/strategy_forensics.py:88-97 — n_closed / avg_hold_days."""
    monkeypatch.setenv("POSTGRES_URI", test_db)
    import importlib
    import research.strategy_forensics as forensics
    importlib.reload(forensics)            # re-bind module-level PG_URI to test DB
    monkeypatch.setattr(forensics, "PG_URI", test_db, raising=True)

    dossier = forensics.build_dossier(STRAT, days=30)
    summ = dossier["pnl_summary"]
    # 3 closed rows seeded; roll EXCLUDED → n_closed == 2 (NULL + stop_loss).
    assert summ["n_closed"] == 2, summ
    # avg_hold_days over {3,4} == 3.5; if the roll (days=1) leaked in it would
    # collapse toward (3+4+1)/3 ≈ 2.67.
    assert abs(float(summ["avg_hold_days"]) - 3.5) < 1e-6, summ
    # n_rows (no status filter) still sees all 3 — sanity that rows exist.
    assert summ["n_rows"] == 3, summ


def test_send_report_excludes_roll(test_db, monkeypatch):
    """src/execution/send_report.py:_load_closed_positions (432-466) +
    _aggregate_closed_positions buckets."""
    monkeypatch.setenv("POSTGRES_URI", test_db)
    import importlib
    import execution.send_report as sr
    importlib.reload(sr)

    rows = sr._load_closed_positions(str_today())
    reasons = sorted((r["close_reason"] or "NULL") for r in rows)
    assert reasons == ["NULL", "stop_loss"], reasons          # roll excluded
    assert ROLL not in {r["close_reason"] for r in rows}

    agg = sr._aggregate_closed_positions(rows)
    assert agg["total_closed"] == 2, agg
    assert ROLL not in agg["by_reason"], agg["by_reason"]
    assert agg["by_reason"].get("unknown") == 1   # the NULL-reason leg
    assert agg["by_reason"].get("stop_loss") == 1
    # roll's days_held=1 must not dilute avg toward 1: kept rows {3,4} → 3.5.
    assert abs(agg["avg_days_held"] - 3.5) < 1e-6, agg


def test_backfill_oue_select_excludes_roll(test_db):
    """src/maintenance/backfill_oue.py:50-67 — the closed-signal SELECT.

    Execute the exact SELECT (copied from the module) against the test DB and
    assert the roll segment is not among the rows to classify.
    """
    sql = """
        SELECT es.id, es.strategy_id, es.ticker, es.signal_date,
               sp.realized_pnl_pct, sp.days_held, sp.pnl_date
          FROM execution_signals es
          JOIN LATERAL (
              SELECT realized_pnl_pct, days_held, pnl_date
                FROM signal_pnl
               WHERE signal_id = es.id
                 AND status = 'closed'
                 AND realized_pnl_pct IS NOT NULL
                 AND close_reason IS DISTINCT FROM 'rolled_continuation'
               ORDER BY pnl_date DESC
               LIMIT 1
          ) sp ON TRUE
         WHERE es.status = 'closed'
           AND es.oue_kind IS NULL
         ORDER BY es.signal_date, es.id
    """
    conn = psycopg2.connect(test_db)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        sig_ids = {str(r[0]) for r in cur.fetchall()}
    finally:
        conn.close()
    # NULL + stop_loss signals selected; roll signal excluded.
    assert SIG_NULL in sig_ids
    assert SIG_REAL in sig_ids
    assert SIG_ROLL not in sig_ids
    assert len(sig_ids) == 2, sig_ids


def str_today():
    import datetime
    return str(datetime.date.today())


# ── JS sites — SQL duplicated from .js source, run via psycopg2 ───────────────

def test_server_js_lifetime_stats_sql(test_db):
    """DUPLICATED from src/channels/api/server.js (avg-stats + win-lifetime
    aggregates, ~856-875).  STALENESS RISK: if that SQL changes, update here.
    Three-row pin: roll excluded, NULL kept, real kept."""
    # avg_days_held aggregate (server.js avg-pnl block).
    row = _scalar(test_db, """
        SELECT COUNT(*) AS n,
               ROUND(AVG(NULLIF(days_held, 0))::numeric, 2) AS avg_days_held
          FROM signal_pnl WHERE status = 'closed'
            AND close_reason IS DISTINCT FROM 'rolled_continuation'
    """)
    assert row[0] == 2, row                       # roll excluded; NULL+real kept
    assert abs(float(row[1]) - 3.5) < 1e-6, row   # (3+4)/2; not collapsed to ~1

    # win-lifetime block.
    win = _scalar(test_db, """
        SELECT COUNT(*) AS closed_count,
               COUNT(*) FILTER (WHERE realized_pnl_pct > 0) AS wins
          FROM signal_pnl
         WHERE status = 'closed' AND realized_pnl_pct IS NOT NULL
           AND close_reason IS DISTINCT FROM 'rolled_continuation'
    """)
    assert win[0] == 2, win
    # winners among kept rows: NULL(+5.0) → win; stop_loss(-2.0) → loss.
    assert win[1] == 1, win


def test_server_js_win30d_sql(test_db):
    """DUPLICATED from src/channels/api/server.js win30dSql (~826-836).
    STALENESS RISK: keep in sync with the .js literal."""
    win = _scalar(test_db, """
        SELECT COUNT(*) AS closed_count,
               COUNT(*) FILTER (WHERE realized_pnl_pct > 0) AS wins
           FROM signal_pnl
          WHERE status='closed' AND realized_pnl_pct IS NOT NULL
            AND close_reason IS DISTINCT FROM 'rolled_continuation'
            AND closed_at >= CURRENT_DATE - INTERVAL '42 days'
    """)
    assert win[0] == 2, win
    assert win[1] == 1, win


def test_daily_health_digest_sql(test_db):
    """DUPLICATED from src/engine/daily-health-digest.js (~56-59).
    STALENESS RISK: keep in sync with the .js literal."""
    row = _scalar(test_db, """
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE realized_pnl_pct > 0) AS wins
          FROM signal_pnl WHERE status='closed'
            AND closed_at > NOW() - INTERVAL '7 days'
            AND close_reason IS DISTINCT FROM 'rolled_continuation'
    """)
    assert row[0] == 2, row
    assert row[1] == 1, row


def test_ticker_alpha_sql_excludes_roll(test_db):
    """DUPLICATED from src/channels/api/server.js ticker-alpha tickerStatsRes
    (~484-491), which reads signal_performance (a thin VIEW over signal_pnl
    carrying close_reason).  STALENESS RISK: keep in sync with the .js literal."""
    row = _scalar(test_db, """
        SELECT COUNT(*)::int AS n
        FROM signal_performance sp
        JOIN execution_signals es ON es.id = sp.signal_id
        WHERE sp.status = 'closed' AND es.ticker = %s
          AND sp.close_reason IS DISTINCT FROM 'rolled_continuation'
    """, (TICKER,))
    assert row[0] == 2, row    # NULL + stop_loss; roll excluded


def test_null_trap_directly(test_db):
    """The exact NULL trap, isolated.  `<> 'rolled_continuation'` would drop the
    NULL-reason row (NULL <> x is NULL → filtered); `IS DISTINCT FROM` keeps it.
    This pins that the NULL-reason closed row STAYS counted."""
    hostile = _scalar(test_db, """
        SELECT COUNT(*) FROM signal_pnl
         WHERE status='closed' AND close_reason <> 'rolled_continuation'
    """)
    safe = _scalar(test_db, """
        SELECT COUNT(*) FROM signal_pnl
         WHERE status='closed' AND close_reason IS DISTINCT FROM 'rolled_continuation'
    """)
    # NULL-hostile drops the NULL row → only 1 (stop_loss); NULL-safe keeps 2.
    assert hostile[0] == 1, hostile
    assert safe[0] == 2, safe
