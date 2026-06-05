"""SP-6 B-flow Phase 1 — historical order-intent extraction (spec §3).

Two grains, both READ-ONLY against Postgres:

  * PRIMARY (signal grain): `signal_pnl sp JOIN execution_signals es ON es.id = sp.signal_id`
    grouped to (signal_date, ticker, direction). One intent per distinct triple;
    the worked session = first SPY trading session STRICTLY AFTER signal_date
    (the signals[t]->fill[t+1] scheme). `execution_signals` has NO `signal_id`
    column — its PK `id` (uuid) is the FK target of `signal_pnl.signal_id`.

  * BROKER ($-weighted realism slice): `alpaca_submissions` real fills. Here
    `run_date` IS the execution session — no t+1 shift. Equity-only filter is
    NULL-safe because equity submissions carry instrument_class NULL by design
    (only option/crypto rows set it).

Worked sessions are resolved against the SPY date index of prices.parquet.
The raw `date.unique()` of that parquet is POLLUTED by 1,059 weekend crypto
dates (BTC-USD etc. trade Sat/Sun) — never use it; filter to SPY rows.

Date columns are cast to ::text in SQL so psycopg2 hands back ISO strings
(datetime.date is unorderable against the str session index and would crash
bisect / the `>= today` lexicographic comparison).
"""
from __future__ import annotations

import bisect
import os
from collections import Counter

import psycopg2
import psycopg2.extras

DEFAULT_PRICES_PATH = "/root/openclaw/data/master/prices.parquet"


# --------------------------------------------------------------------------
# env + connection (b1_order_source.py:19-31 pattern — worktrees lack .env)
# --------------------------------------------------------------------------
def _env(key):
    """Read a key from /root/openclaw/.env (the worktree has no .env)."""
    val = os.environ.get(key)
    if val:
        return val
    for line in open("/root/openclaw/.env"):
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _conn():
    """psycopg2 connection from POSTGRES_URI. All queries are READ-ONLY."""
    return psycopg2.connect(_env("POSTGRES_URI"))


# --------------------------------------------------------------------------
# SPY session index
# --------------------------------------------------------------------------
def load_spy_sessions(path=DEFAULT_PRICES_PATH):
    """Return the sorted list of SPY trading-session dates ('YYYY-MM-DD').

    WARNING: use SPY rows ONLY. The raw `date.unique()` of prices.parquet is
    polluted by 1,059 weekend crypto dates (BTC-USD/ETH-USD trade Sat/Sun);
    those are NOT trading sessions and would corrupt worked-session resolution.
    """
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    table = pq.read_table(path, columns=["ticker", "date"])
    spy = table.filter(pc.equal(table["ticker"], "SPY"))
    dates = {str(d) for d in spy.column("date").to_pylist() if d is not None}
    return sorted(dates)


def resolve_worked_session(signal_date, sessions):
    """First session STRICTLY AFTER `signal_date`, or None past the end.

    `sessions` must be sorted ascending ISO-date strings. `signal_date` is an
    ISO string (cast ::text in SQL). Uses bisect_right so an exact match is
    excluded (strictly-after) and a gap (missing Friday) skips to the next
    available session. Mirrors b1_run._resolve_session resolve_next_session=True.
    """
    idx = bisect.bisect_right(sessions, signal_date)
    if idx >= len(sessions):
        return None
    return sessions[idx]


# --------------------------------------------------------------------------
# PRIMARY grain (signal_pnl x execution_signals)
# --------------------------------------------------------------------------
PRIMARY_SQL = """
    SELECT es.signal_date::text AS signal_date,
           es.ticker            AS ticker,
           es.direction         AS direction,
           COUNT(*)             AS n_signals,
           SUM(COALESCE(es.position_size_pct, 0)) AS sum_size_pct,
           MAX(es.regime_state) AS regime_state
    FROM signal_pnl sp
    JOIN execution_signals es ON es.id = sp.signal_id
    WHERE sp.status = 'closed'
      AND es.signal_date IS NOT NULL
      -- rolled_continuation rows are D1 roll segments, not trades. NULL-safe
      -- (historical close_reason is NULL). Currently a no-op (no rolls exist
      -- yet) — kept as future-proofing.
      AND sp.close_reason IS DISTINCT FROM 'rolled_continuation'
    GROUP BY es.signal_date, es.ticker, es.direction
    ORDER BY es.signal_date, es.ticker, es.direction
"""


def primary_intents(today, sessions, conn=None):
    """Extract primary-grain intents grouped to (signal_date, ticker, direction).

    Returns (intents, exclusions). One intent per grouped row; the worked
    session is resolved per-row (two signal_dates may map to one session across
    a weekend gap — they stay separate intents). Rows are excluded (and COUNTED,
    never silently dropped) when:
      * no_worked_session       — signal_date past the end of the SPY index
      * worked_session_ge_today — worked session >= today (no settled close yet)

    Invariant: len(intents) + sum(exclusions.values()) == grouped row count.
    """
    own = conn is None
    if own:
        conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(PRIMARY_SQL)
        rows = cur.fetchall()
    finally:
        if own:
            conn.close()

    intents = []
    exclusions = Counter()
    for r in rows:
        worked = resolve_worked_session(r["signal_date"], sessions)
        if worked is None:
            exclusions["no_worked_session"] += 1
            continue
        if worked >= today:
            exclusions["worked_session_ge_today"] += 1
            continue
        intents.append({
            "grain": "primary",
            "worked_session": worked,
            "ticker": r["ticker"],
            "direction": r["direction"],
            "n_signals": int(r["n_signals"]),
            "sum_size_pct": float(r["sum_size_pct"]) if r["sum_size_pct"] is not None else 0.0,
            "regime_state": r["regime_state"],
        })
    return intents, dict(exclusions)


# --------------------------------------------------------------------------
# BROKER grain (alpaca_submissions real fills)
# --------------------------------------------------------------------------
BROKER_SQL = """
    SELECT run_date::text     AS run_date,
           ticker             AS ticker,
           direction          AS direction,
           qty                AS qty,
           filled_avg_price   AS filled_avg_price,
           strategy_id        AS strategy_id
    FROM alpaca_submissions
    WHERE (instrument_class IS NULL OR instrument_class = 'equity')
      AND qty IS NOT NULL
      AND qty <> 0
      AND filled_avg_price IS NOT NULL
    ORDER BY run_date, ticker
"""


def broker_intents(today, conn=None):
    """Extract broker-grain intents from real equity fills (alpaca_submissions).

    worked_session = run_date VERBATIM (run_date IS the execution session — no
    t+1 shift, unlike the primary grain). Returns (intents, exclusions). Rows
    with run_date >= today are excluded and counted (run_date_ge_today).
    """
    own = conn is None
    if own:
        conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(BROKER_SQL)
        rows = cur.fetchall()
    finally:
        if own:
            conn.close()

    intents = []
    exclusions = Counter()
    for r in rows:
        worked = r["run_date"]
        if worked is None:
            exclusions["no_run_date"] += 1
            continue
        if worked >= today:
            exclusions["run_date_ge_today"] += 1
            continue
        intents.append({
            "grain": "broker",
            "worked_session": worked,
            "ticker": r["ticker"],
            "direction": r["direction"],
            "qty": float(r["qty"]),
            "filled_avg_price": float(r["filled_avg_price"]),
            "strategy_id": r["strategy_id"],
        })
    return intents, dict(exclusions)


# --------------------------------------------------------------------------
# regime (per-session fallback via execution_runs)
# --------------------------------------------------------------------------
SESSION_REGIME_SQL = """
    SELECT DISTINCT ON (run_date)
           run_date::text AS run_date,
           regime_state   AS regime_state
    FROM execution_runs
    ORDER BY run_date, created_at DESC
"""


def session_regime_map(conn=None):
    """Map 'YYYY-MM-DD' -> regime_state from execution_runs (DISTINCT ON run_date,
    latest created_at per date). The only full-span regime source over the eval
    window (market_regime starts 04-29 and lacks HIGH_VOL — gapped, not used).
    """
    own = conn is None
    if own:
        conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(SESSION_REGIME_SQL)
        rows = cur.fetchall()
    finally:
        if own:
            conn.close()
    return {r["run_date"]: r["regime_state"] for r in rows}


def regime_for(intent, session_map):
    """Resolve an intent's regime: its own regime_state if truthy, else the
    per-session fallback from `session_map` keyed on worked_session."""
    own = intent.get("regime_state")
    if own:
        return own
    return session_map.get(intent.get("worked_session"))
