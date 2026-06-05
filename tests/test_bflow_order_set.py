"""Tests for src/research/bflow/order_set.py — historical order-intent extraction
(§3 of the Phase-1 oracle kill-test design), both grains.

NO network, NO DB by default: every query path is exercised through a fake
connection whose cursor returns pre-canned stub rows. Stub rows mirror the LIVE
shape: signal_date / worked_session arrive as ISO 'YYYY-MM-DD' STRINGS because
the SQL casts the DATE columns with ``::text`` (psycopg2 otherwise hands back
datetime.date, which is unorderable against the str session index and would
crash bisect / the ``>= today`` lexicographic comparison).

The single DB-touching integration test is gated on BFLOW_IT=1, read-only.
"""
import bisect
import os

import pytest

from src.research.bflow import order_set


# --------------------------------------------------------------------------
# fake conn/cursor (matches the only surface order_set uses:
# conn.cursor(cursor_factory=...) -> cursor.execute(...) ; cursor.fetchall())
# --------------------------------------------------------------------------
class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.last_cursor = None

    def cursor(self, cursor_factory=None):
        self.last_cursor = _FakeCursor(self._rows)
        return self.last_cursor


# ==========================================================================
# 1. resolve_worked_session — strictly-after with weekend/holiday gaps
# ==========================================================================
SESSIONS = ["2026-04-13", "2026-04-14", "2026-04-15", "2026-04-16",
            # 17 (Fri) MISSING — holiday gap
            "2026-04-20", "2026-04-21"]


def test_resolve_worked_session_normal_next_day():
    # signal on Mon 13 -> worked session = Tue 14.
    assert order_set.resolve_worked_session("2026-04-13", SESSIONS) == "2026-04-14"


def test_resolve_worked_session_skips_gap():
    # signal on Thu 16; Fri 17 is missing -> first STRICTLY after = Mon 20.
    assert order_set.resolve_worked_session("2026-04-16", SESSIONS) == "2026-04-20"


def test_resolve_worked_session_exact_match_excluded():
    # An exact session date is NOT its own worked session (strictly after).
    assert order_set.resolve_worked_session("2026-04-14", SESSIONS) == "2026-04-15"


def test_resolve_worked_session_past_end_returns_none():
    assert order_set.resolve_worked_session("2026-04-21", SESSIONS) is None
    assert order_set.resolve_worked_session("2026-12-31", SESSIONS) is None


def test_resolve_worked_session_before_start():
    # signal before the whole index -> first session.
    assert order_set.resolve_worked_session("2026-01-01", SESSIONS) == "2026-04-13"


# ==========================================================================
# 2. primary_intents — row processing, exclusion accounting, intent shape
# ==========================================================================
def _primary_row(signal_date, ticker="AAPL", direction="LONG", n=3,
                 sum_size=0.12, regime="LOW_VOL"):
    """Mirror the grouped SQL row shape (signal_date already ::text -> str)."""
    return {
        "signal_date": signal_date,
        "ticker": ticker,
        "direction": direction,
        "n_signals": n,
        "sum_size_pct": sum_size,
        "regime_state": regime,
    }


def test_primary_intents_resolves_and_shapes():
    today = "2026-04-21"
    rows = [
        _primary_row("2026-04-13", ticker="AAPL", direction="LONG"),
        _primary_row("2026-04-16", ticker="MSFT", direction="SHORT"),
    ]
    conn = _FakeConn(rows)
    intents, exclusions = order_set.primary_intents(today, SESSIONS, conn=conn)
    assert len(intents) == 2
    a = next(i for i in intents if i["ticker"] == "AAPL")
    assert a["grain"] == "primary"
    assert a["worked_session"] == "2026-04-14"
    assert a["direction"] == "LONG"
    assert a["n_signals"] == 3
    assert isinstance(a["sum_size_pct"], float)  # Decimal must be cast
    assert a["regime_state"] == "LOW_VOL"
    m = next(i for i in intents if i["ticker"] == "MSFT")
    assert m["worked_session"] == "2026-04-20"  # gap-skip
    assert exclusions == {} or sum(exclusions.values()) == 0


def test_primary_intents_excludes_today_and_future():
    today = "2026-04-20"
    rows = [
        _primary_row("2026-04-13", ticker="AAPL"),      # worked 14 < today -> keep
        _primary_row("2026-04-16", ticker="MSFT"),      # worked 20 == today -> exclude
        _primary_row("2026-04-21", ticker="TSLA"),      # worked None (past end) -> exclude
    ]
    conn = _FakeConn(rows)
    intents, exclusions = order_set.primary_intents(today, SESSIONS, conn=conn)
    assert {i["ticker"] for i in intents} == {"AAPL"}
    # No silent drops: kept + excluded == input rows.
    assert len(intents) + sum(exclusions.values()) == 3
    # Distinct reason keys for the two distinct causes.
    assert exclusions.get("worked_session_ge_today", 0) == 1
    assert exclusions.get("no_worked_session", 0) == 1


def test_primary_intents_uses_primary_sql():
    conn = _FakeConn([])
    order_set.primary_intents("2026-04-21", SESSIONS, conn=conn)
    sql, _ = conn.last_cursor.executed[0]
    assert sql == order_set.PRIMARY_SQL


# ==========================================================================
# 3. broker_intents — run_date verbatim, future drop, SQL guards
# ==========================================================================
def _broker_row(run_date, ticker="AAPL", direction="LONG", qty=10,
                fap=100.5, strategy="s_x"):
    return {
        "run_date": run_date,
        "ticker": ticker,
        "direction": direction,
        "qty": qty,
        "filled_avg_price": fap,
        "strategy_id": strategy,
    }


def test_broker_intents_run_date_verbatim_and_shape():
    today = "2026-04-21"
    rows = [_broker_row("2026-04-20", ticker="AAPL", direction="SHORT",
                        qty=7, fap=250.25, strategy="s_mom")]
    conn = _FakeConn(rows)
    intents, exclusions = order_set.broker_intents(today, conn=conn)
    assert len(intents) == 1
    i = intents[0]
    assert i["grain"] == "broker"
    assert i["worked_session"] == "2026-04-20"  # run_date verbatim, NO t+1 shift
    assert i["ticker"] == "AAPL"
    assert i["direction"] == "SHORT"
    assert isinstance(i["qty"], float)
    assert i["qty"] == 7.0
    assert isinstance(i["filled_avg_price"], float)
    assert i["filled_avg_price"] == 250.25
    assert i["strategy_id"] == "s_mom"


def test_broker_intents_drops_future_run_date():
    today = "2026-04-20"
    rows = [
        _broker_row("2026-04-17", ticker="AAPL"),   # < today -> keep
        _broker_row("2026-04-20", ticker="MSFT"),   # == today -> drop
        _broker_row("2026-04-25", ticker="TSLA"),   # > today -> drop
    ]
    conn = _FakeConn(rows)
    intents, exclusions = order_set.broker_intents(today, conn=conn)
    assert {i["ticker"] for i in intents} == {"AAPL"}
    assert len(intents) + sum(exclusions.values()) == 3
    assert exclusions.get("run_date_ge_today", 0) == 2


def test_broker_sql_has_null_safe_equity_filter():
    assert "instrument_class IS NULL OR instrument_class = 'equity'" in order_set.BROKER_SQL
    assert "qty IS NOT NULL" in order_set.BROKER_SQL
    assert "qty <> 0" in order_set.BROKER_SQL
    assert "filled_avg_price IS NOT NULL" in order_set.BROKER_SQL


def test_primary_sql_has_roll_filter():
    # IS DISTINCT FROM 'rolled_continuation' future-proofing must be present.
    assert "IS DISTINCT FROM 'rolled_continuation'" in order_set.PRIMARY_SQL
    assert "es.id = sp.signal_id" in order_set.PRIMARY_SQL


# ==========================================================================
# 4. regime_for — intent regime else session-map fallback
# ==========================================================================
def test_regime_for_uses_intent_regime_when_present():
    session_map = {"2026-04-14": "HIGH_VOL"}
    intent = {"worked_session": "2026-04-14", "regime_state": "LOW_VOL"}
    assert order_set.regime_for(intent, session_map) == "LOW_VOL"


def test_regime_for_falls_back_to_session_map():
    session_map = {"2026-04-14": "HIGH_VOL"}
    # Falsy regime (None) -> use the session map.
    intent = {"worked_session": "2026-04-14", "regime_state": None}
    assert order_set.regime_for(intent, session_map) == "HIGH_VOL"
    # Empty string is falsy too.
    intent2 = {"worked_session": "2026-04-14", "regime_state": ""}
    assert order_set.regime_for(intent2, session_map) == "HIGH_VOL"


def test_regime_for_missing_session_returns_none():
    session_map = {}
    intent = {"worked_session": "2026-04-14", "regime_state": None}
    assert order_set.regime_for(intent, session_map) is None


# ==========================================================================
# 5. load_spy_sessions — SPY-only, sorted, on a synthetic parquet
# ==========================================================================
def test_load_spy_sessions_filters_crypto_and_sorts(tmp_path):
    import pandas as pd
    p = tmp_path / "prices.parquet"
    df = pd.DataFrame({
        # SPY (out of order) + a crypto ticker on weekend dates that pollute
        # the raw date.unique() — must NOT appear.
        "ticker": ["SPY", "SPY", "SPY", "BTC-USD", "BTC-USD"],
        "date": ["2026-04-15", "2026-04-13", "2026-04-14",
                 "2026-04-18", "2026-04-19"],  # Sat/Sun crypto bars
        "close": [1.0, 2.0, 3.0, 4.0, 5.0],
    })
    df.to_parquet(p)
    sessions = order_set.load_spy_sessions(path=str(p))
    assert sessions == ["2026-04-13", "2026-04-14", "2026-04-15"]
    # Weekend crypto dates excluded.
    assert "2026-04-18" not in sessions
    assert "2026-04-19" not in sessions


# ==========================================================================
# 5b. load_parquet_closes — production (worked_session,ticker)->close loader
#     (spec §5/§6 parquet cross-check; previously the real driver never built
#     this map so the divergence table always rendered "compared 0").
# ==========================================================================
def test_load_parquet_closes_keys_per_ticker_per_session(tmp_path):
    import pandas as pd
    p = tmp_path / "prices.parquet"
    df = pd.DataFrame({
        "ticker": ["SPY", "SPY", "AAPL", "AAPL", "BTC-USD"],
        "date": ["2026-04-13", "2026-04-14", "2026-04-13", "2026-04-14",
                 "2026-04-18"],
        "close": [500.0, 501.0, 170.0, 171.0, 60000.0],
    })
    df.to_parquet(p)
    # Request only a subset of (session, ticker) pairs — the loader must key on
    # the PER-TICKER close (not SPY) for each requested pair, and ignore pairs
    # not asked for.
    pairs = [("2026-04-13", "AAPL"), ("2026-04-14", "SPY")]
    closes = order_set.load_parquet_closes(pairs, path=str(p))
    assert closes[("2026-04-13", "AAPL")] == 170.0
    assert closes[("2026-04-14", "SPY")] == 501.0
    # Unrequested pairs absent.
    assert ("2026-04-13", "SPY") not in closes
    assert ("2026-04-14", "AAPL") not in closes


def test_load_parquet_closes_missing_pair_absent(tmp_path):
    import pandas as pd
    p = tmp_path / "prices.parquet"
    df = pd.DataFrame({
        "ticker": ["AAPL"],
        "date": ["2026-04-13"],
        "close": [170.0],
    })
    df.to_parquet(p)
    # A (session,ticker) with no row in the parquet is simply absent (the
    # caller's _divergence skips absent keys) — never a crash, never a 0.
    closes = order_set.load_parquet_closes(
        [("2026-04-13", "AAPL"), ("2026-04-14", "MSFT")], path=str(p))
    assert closes == {("2026-04-13", "AAPL"): 170.0}


def test_load_parquet_closes_empty_pairs_returns_empty(tmp_path):
    import pandas as pd
    p = tmp_path / "prices.parquet"
    pd.DataFrame({"ticker": ["AAPL"], "date": ["2026-04-13"],
                  "close": [170.0]}).to_parquet(p)
    # No pairs requested -> empty map without reading/scanning anything.
    assert order_set.load_parquet_closes([], path=str(p)) == {}


# ==========================================================================
# 6. INTEGRATION — live DB, read-only, gated on BFLOW_IT=1
# ==========================================================================
@pytest.mark.skipif(os.environ.get("BFLOW_IT") != "1",
                    reason="DB integration; set BFLOW_IT=1 to run (read-only)")
def test_primary_intents_live_db():
    sessions = order_set.load_spy_sessions()
    assert len(sessions) > 2000
    today = "2026-06-05"
    intents, exclusions = order_set.primary_intents(today, sessions)
    assert len(intents) > 3000
    # Spans the eval window.
    ws = sorted(i["worked_session"] for i in intents)
    assert ws[0] < "2026-05-01"
    assert ws[-1] >= "2026-06-01"
    # No silent drops accounting holds (kept + excluded == grouped row count).
    assert isinstance(exclusions, dict)
    # Every intent carries the required keys.
    i0 = intents[0]
    for k in ("grain", "worked_session", "ticker", "direction",
              "n_signals", "sum_size_pct", "regime_state"):
        assert k in i0
