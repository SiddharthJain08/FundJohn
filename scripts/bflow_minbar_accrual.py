#!/usr/bin/env python3
"""SP-6 B-flow — nightly minute-bar ACCRUAL (one settled equity session).

Fetches RTH 1-minute bars for the equity tickers WORKED on a single trading
session into the shared session-parquet cache so the Phase-1b predictability
study keeps growing out-of-sample. READ-ONLY against Postgres; the only thing
it writes is the per-session minute-bar parquet (via the append-only
``minbar_cache.get_session_bars`` — cache-hit skip + atomic write + invalid-
symbol ejection all live there).

Usage:
    python3 -m scripts.bflow_minbar_accrual [--date YYYY-MM-DD] [--dry-run]
    (or invoked as a file: scripts/bflow_minbar_accrual.py)

--date    session to accrue (default: today, America/New_York). For a same-day
          run we REFUSE before 16:05 ET (exit 2) — minute bars for the open
          session are not yet settled.
--dry-run run the trading-day gate and report the decision + ticker count, but
          DO NOT fetch or write anything.

TRADING-DAY GATE = INTENTS EXIST (deliberately broader than the research
extraction in order_set.py, which is narrower than we want here):

  * order_set.PRIMARY_SQL joins signal_pnl and requires status='closed' — a
    just-completed session has few/no closed trades (multi-day holds still
    open), so reusing it would emit "no intents" on a real trading day.
  * order_set.BROKER_SQL requires filled_avg_price IS NOT NULL and qty<>0 — a
    signals-but-no-fill day (e.g. DTBP=0) would false-negative.

So the gate is written fresh with INTENT-EXISTENCE semantics:

  tickers = (execution_signals worked this session: target_date == --date,
             equity only i.e. ticker NOT LIKE '%-USD' — execution_signals has
             NO instrument_class column, and crypto signals carry a -USD
             ticker and trade weekends, so they must be excluded)
            UNION
            (alpaca_submissions submitted on --date ET, equity, ANY status:
             instrument_class IS NULL OR ='equity', ticker NOT NULL)

In the EOD->open execution model target_date IS the worked/fill session (same
convention sp6_phasec_verify.py uses), so the primary side needs NO parquet —
it does NOT depend on prices.parquet having today's SPY row (which may not have
landed when the nightly timer fires). The broker side needs no parquet either.

If the union is EMPTY -> log "no intents, skipping" and exit 0 WITHOUT writing
anything to the cache (this is the holiday/non-trading-day guard; we never use
a naive weekday check, which would pollute the cache with holiday sentinels).
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys

# America/New_York close-equivalent guard: refuse a same-day run before 16:05.
_CLOSE_GUARD_H = 16
_CLOSE_GUARD_M = 5

# Module-resolution: this script lives in <worktree>/scripts; the bflow package
# is under <worktree>/src. The systemd unit sets PYTHONPATH=<worktree>/src, but
# make a direct `./scripts/bflow_minbar_accrual.py` invocation work too.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _now_et():
    from zoneinfo import ZoneInfo
    return datetime.datetime.now(ZoneInfo("America/New_York"))


def _today_et_iso():
    return _now_et().date().isoformat()


# --------------------------------------------------------------------------
# trading-day gate (intent existence) — READ-ONLY
# --------------------------------------------------------------------------
_PRIMARY_GATE_SQL = """
    SELECT DISTINCT ticker
    FROM execution_signals
    WHERE target_date = %s
      AND ticker IS NOT NULL
      AND ticker NOT LIKE '%%-USD'
"""

_BROKER_GATE_SQL = """
    SELECT DISTINCT ticker
    FROM alpaca_submissions
    WHERE run_date = %s
      AND (instrument_class IS NULL OR instrument_class = 'equity')
      AND ticker IS NOT NULL
"""


def worked_equity_tickers(session, conn):
    """Return the sorted UNION of equity tickers WORKED on ``session``.

    primary  = execution_signals.target_date == session (equity-only)
    broker   = alpaca_submissions.run_date == session (equity, any status)

    Both sides are read-only and parquet-free. An empty union means
    'no intents this session' (holiday / non-trading day)."""
    cur = conn.cursor()
    cur.execute(_PRIMARY_GATE_SQL, (session,))
    tickers = {r[0] for r in cur.fetchall() if r[0]}
    cur.execute(_BROKER_GATE_SQL, (session,))
    tickers |= {r[0] for r in cur.fetchall() if r[0]}
    return sorted(tickers)


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def _parse_args(argv):
    p = argparse.ArgumentParser(prog="bflow_minbar_accrual")
    p.add_argument("--date", default=None,
                   help="session to accrue (YYYY-MM-DD); default today (ET)")
    p.add_argument("--dry-run", action="store_true",
                   help="run the gate + report decision; fetch/write NOTHING")
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args([] if argv is None else argv)

    today = _today_et_iso()
    session = args.date or today

    # (1) same-day close guard — minute bars not settled before 16:05 ET.
    if session == today:
        now = _now_et()
        if (now.hour, now.minute) < (_CLOSE_GUARD_H, _CLOSE_GUARD_M):
            print(f"[bflow-accrual] {session} is today and it is "
                  f"{now:%H:%M} ET (< {_CLOSE_GUARD_H:02d}:{_CLOSE_GUARD_M:02d} "
                  "ET close+5min); session not settled — refusing.", flush=True)
            return 2

    # import after sys.path fixup
    from research.bflow.order_set import _conn
    from research.bflow.minbar_cache import get_session_bars

    # (2) trading-day gate = intents exist.
    conn = _conn()
    try:
        tickers = worked_equity_tickers(session, conn)
    finally:
        conn.close()

    if not tickers:
        print(f"[bflow-accrual] {session}: no intents "
              "(0 equity tickers via execution_signals.target_date "
              "UNION alpaca_submissions.run_date) — skipping, "
              "no cache write.", flush=True)
        return 0

    print(f"[bflow-accrual] {session}: {len(tickers)} equity intent ticker(s) "
          "worked this session.", flush=True)

    if args.dry_run:
        preview = ", ".join(tickers[:20])
        more = "" if len(tickers) <= 20 else f" (+{len(tickers) - 20} more)"
        print(f"[bflow-accrual] DRY-RUN: would fetch minute bars for "
              f"{len(tickers)} tickers: {preview}{more}", flush=True)
        print("[bflow-accrual] DRY-RUN: no fetch, no cache write.", flush=True)
        return 0

    # (3) fetch via the cache (cache-hit skip + dot-normalization + surgical
    #     invalid-symbol ejection + fail-loud all live in get_session_bars).
    #     cache_dir defaults to $BFLOW_CACHE_DIR or the shared openclaw dir —
    #     we deliberately DO NOT override it so the weekly rerun reads the
    #     SAME cache the accrual writes.
    bars_map = get_session_bars(session, tickers)

    # (4) log session / ticker count / bar rows / zero-bar tickers.
    #     NOTE on "ejected symbols": get_session_bars surfaces a truly-invalid
    #     (400-ejected) symbol as an empty bar list — indistinguishable from a
    #     symbol that simply had zero bars in the RTH window. So we report
    #     ZERO-BAR tickers (the superset: ejected-invalid + genuinely-empty);
    #     the per-session parquet's minute=-1 sentinel remembers them so they
    #     are never refetched.
    total_bars = sum(len(b) for b in bars_map.values())
    zero_bar = sorted(t for t, b in bars_map.items() if not b)
    print(f"[bflow-accrual] {session}: cached/fetched {len(bars_map)} tickers, "
          f"{total_bars} bar rows.", flush=True)
    if zero_bar:
        print(f"[bflow-accrual] {session}: {len(zero_bar)} zero-bar ticker(s) "
              f"(ejected-invalid OR empty-RTH; sentinel-remembered): "
              f"{', '.join(zero_bar)}", flush=True)
    else:
        print(f"[bflow-accrual] {session}: no zero-bar tickers.", flush=True)
    return 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main(sys.argv[1:]))
