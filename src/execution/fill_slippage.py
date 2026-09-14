"""fill_slippage.py — daily realized-slippage line for #trade-reports.

Stream B item 14 (docs/specs/2026-09-12-quantdinger-adoptions-spec.md:144-166).
Report-only: gates nothing, routes nothing, and never raises out of
fill_slippage_line (returns None on any failure, logged) — same contract as
bench_realized.bench_realized_line.

Entry bp: execution_signals.fill_slippage_bps (migration 145, written by
parity_mark.backfill_broker_fill_truth) vs the official close.
Exit bp:  signal_pnl.exit_slippage_bps (migration 155, written by
alpaca_reconcile.backfill_exit_slippage) vs the signal's own stop/target.
Latency: alpaca_submissions.filled_at - submitted_at.

The verdict compares OUR MEDIAN realized entry bp against OUR OWN modelled
one-way half-spread for the same tickers (data/derived/ticker_cost_bps.json via
backtest.unified_backtest.load_ticker_cost_bps:101-125) — never a foreign asset
class's bands: OK <= 1.5x modelled, WARN <= 3x, FAIL above.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

OK_MULT = 1.5
WARN_MULT = 3.0


def _median(xs):
    vals = sorted(float(x) for x in xs)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _mean(xs):
    vals = [float(x) for x in xs]
    return (sum(vals) / len(vals)) if vals else None


def _pctile(xs, q: float):
    """Nearest-rank percentile — stable on the 1-20 row samples a daily cycle
    produces, where linear interpolation invents values we never paid."""
    vals = sorted(float(x) for x in xs)
    if not vals:
        return None
    import math
    k = max(1, math.ceil(q * len(vals)))
    return vals[min(k, len(vals)) - 1]


def verdict(median_bps, modelled_bps) -> str:
    """'OK' | 'WARN' | 'FAIL' | 'n/a' — realized median vs our own cost model."""
    if median_bps is None or modelled_bps is None:
        return 'n/a'
    try:
        model = float(modelled_bps)
        realized = float(median_bps)
    except (TypeError, ValueError):
        return 'n/a'
    if model <= 0:
        return 'n/a'
    ratio = realized / model
    if ratio <= OK_MULT:
        return 'OK'
    if ratio <= WARN_MULT:
        return 'WARN'
    return 'FAIL'


def modelled_median_bps(tickers, cost_bps=None):
    """Median modelled one-way half-spread over the tickers we actually traded,
    or None when the artifact is missing or covers none of them."""
    if cost_bps is None:
        from backtest.unified_backtest import load_ticker_cost_bps
        cost_bps = load_ticker_cost_bps()
    if not cost_bps:
        return None
    vals = [float(cost_bps[t]) for t in tickers if t in cost_bps]
    return _median(vals) if vals else None


def summarize(entry_rows, exit_rows, cost_bps=None) -> dict:
    """Pure: rows -> the stats dict format_line renders.

    entry_rows: [{'ticker', 'bps', 'notional_usd', 'latency_s'}]
    exit_rows:  [{'ticker', 'bps', 'notional_usd'}]
    cost_usd is the SIGNED bp-weighted notional sum, so a favourable fill offsets
    an adverse one — it is realized cost, not an adverse-only tally."""
    entry_rows = [r for r in (entry_rows or []) if r.get('bps') is not None]
    exit_rows = [r for r in (exit_rows or []) if r.get('bps') is not None]
    e_bps = [r['bps'] for r in entry_rows]
    x_bps = [r['bps'] for r in exit_rows]
    lat = [r['latency_s'] for r in entry_rows if r.get('latency_s') is not None]
    cost = sum(float(r['bps']) / 10000.0 * float(r.get('notional_usd') or 0.0)
               for r in entry_rows + exit_rows)
    tickers = [r['ticker'] for r in entry_rows if r.get('ticker')]
    model = modelled_median_bps(tickers, cost_bps=cost_bps) if tickers else None
    e_median = _median(e_bps)
    return {
        'entry_n': len(e_bps), 'entry_mean': _mean(e_bps),
        'entry_median': e_median, 'entry_p90': _pctile(e_bps, 0.90),
        'exit_n': len(x_bps), 'exit_mean': _mean(x_bps),
        'latency_median_s': _median(lat),
        'cost_usd': cost,
        'modelled_median': model,
        'verdict': verdict(e_median, model),
    }


def _bp(v):
    return 'n/a' if v is None else f'{float(v):.1f}bp'


def _sec(v):
    return 'n/a' if v is None else f'{float(v):.1f}s'


def format_line(st: dict, run_date) -> str:
    """The one-line digest string. n=0 renders 'n/a', never a 0 that would read
    as 'we paid no slippage today'."""
    return (f"fill_slippage: entry n={st['entry_n']} mean={_bp(st['entry_mean'])} "
            f"median={_bp(st['entry_median'])} p90={_bp(st['entry_p90'])} | "
            f"exit n={st['exit_n']} mean={_bp(st['exit_mean'])} | "
            f"latency_med={_sec(st['latency_median_s'])} | "
            f"cost=${st['cost_usd']:,.0f} | "
            f"modelled_med={_bp(st['modelled_median'])} verdict={st['verdict']} "
            f"asof={str(run_date)[:10]}")


_ENTRY_SQL = """
    SELECT es.ticker,
           es.fill_slippage_bps,
           COALESCE(s.filled_qty, 0) * COALESCE(es.broker_fill_price, 0),
           EXTRACT(EPOCH FROM (s.filled_at - s.submitted_at))
      FROM execution_signals es
      JOIN alpaca_submissions s ON s.run_date = es.target_date
                               AND s.ticker = es.ticker
                               AND s.strategy_id = es.strategy_id
     WHERE es.target_date = %s
       AND es.fill_slippage_bps IS NOT NULL
"""

_EXIT_SQL = """
    SELECT es.ticker,
           sp.exit_slippage_bps,
           COALESCE(sp.closed_price, 0) * COALESCE(s.filled_qty, 0)
      FROM signal_pnl sp
      JOIN execution_signals es ON es.id = sp.signal_id
      LEFT JOIN alpaca_submissions s ON s.run_date = es.target_date
                                    AND s.ticker = es.ticker
                                    AND s.strategy_id = es.strategy_id
     WHERE sp.pnl_date = %s
       AND sp.exit_slippage_bps IS NOT NULL
"""


def load_entry_rows(conn, run_date) -> list:
    with conn.cursor() as cur:
        cur.execute(_ENTRY_SQL, (run_date,))
        return [{'ticker': r[0], 'bps': float(r[1]),
                 'notional_usd': float(r[2] or 0.0),
                 'latency_s': (None if r[3] is None else float(r[3]))}
                for r in (cur.fetchall() or [])]


def load_exit_rows(conn, run_date) -> list:
    with conn.cursor() as cur:
        cur.execute(_EXIT_SQL, (run_date,))
        return [{'ticker': r[0], 'bps': float(r[1]),
                 'notional_usd': float(r[2] or 0.0)}
                for r in (cur.fetchall() or [])]


def fill_slippage_line(run_date, *, conn=None, cost_bps=None):
    """The full line, or None on any failure (logged). Owns its connection when
    conn is None — mirrors bench_realized.bench_realized_line:164-189."""
    import os
    own = conn is None
    try:
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        st = summarize(load_entry_rows(conn, run_date),
                       load_exit_rows(conn, run_date), cost_bps=cost_bps)
        return format_line(st, run_date)
    except Exception as e:  # noqa: BLE001
        logger.warning('[fill_slippage] skipped (%s: %s)', type(e).__name__, e)
        return None
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
