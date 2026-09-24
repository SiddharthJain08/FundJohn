"""fill_slippage.py — daily realized-slippage line for #trade-reports.

Stream B item 14 (docs/specs/2026-09-12-quantdinger-adoptions-spec.md:144-166).
Report-only: gates nothing, routes nothing. fill_slippage_line ALWAYS returns a
string (never None, never raises) — on any failure it renders
'fill_slippage: n/a (<ExceptionClass>: <reason>)' rather than omitting the line,
because a silent skip must never be indistinguishable from "nothing to report"
(fix round 1, 2026-09-14; the identical gap in bench_realized.bench_realized_line
is a separate, out-of-scope task).

Entry bp: execution_signals.fill_slippage_bps (migration 145, written by
parity_mark.backfill_broker_fill_truth) vs the official close.
Exit bp:  signal_pnl.exit_slippage_bps (migration 155, written by
alpaca_reconcile.backfill_exit_slippage) vs the signal's own stop/target.
Latency: alpaca_submissions.filled_at - submitted_at.

The verdict compares OUR MEDIAN realized entry bp against OUR OWN modelled
one-way half-spread for the same tickers (data/derived/ticker_cost_bps.json via
backtest.unified_backtest.load_ticker_cost_bps:101-125) — never a foreign asset
class's bands: OK <= 1.5x modelled, WARN <= 3x, FAIL above.

Dedup (fix round 1): execution_signals is only UNIQUE on (strategy_id,
signal_date, ticker, direction) — NOT on target_date. An overnight signal and a
same-day signal (or a long+short flip) for the same strategy/ticker can both
carry the SAME target_date, so both join the SAME alpaca_submissions row
(unique on run_date, strategy_id, ticker). Left unguarded, that one physical
fill's bp/notional/latency gets counted once per execution_signals row instead
of once per fill. _ENTRY_SQL/_EXIT_SQL carry a DISTINCT ON (per submission) at
the SQL layer, AND load_entry_rows/load_exit_rows re-apply the same collapse
in Python via _dedupe_by_submission — the Python layer is the one under test
(a fake DB cursor can't exercise Postgres' own DISTINCT ON), so it is the
actual guarantee; the SQL clause is defense in depth for the real query plan.
Rows with no resolvable submission (strategy_id is None — an unmatched LEFT
JOIN leg in _EXIT_SQL) are never deduped against each other: each is distinct
data, not a duplicate of anything.

Cost split (fix round 1): the benchmark sleeve (S_beta_spy, ~78% of NAV) is one
rebalance leg large enough to dominate an undifferentiated dollar figure. The
dollar cost is split alpha vs bench by each row's strategy_id membership in
the benchmark-sleeve set (execution.benchmark_sleeve.load_benchmark_sleeve_ids
— strategy_registry.parameters ->> 'benchmark_sleeve'); bp stats (mean/median/
p90) stay UNSPLIT and include the benchmark sleeve, since the verdict is about
realized execution quality, not book composition.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# F3 / review I-4: an exception raised while connecting (own=True path above)
# can be a psycopg2 error whose message embeds the DSN, credentials and all
# (e.g. `connection to server ... failed: FATAL: password authentication
# failed for user "x"` variants, or a raw `postgresql://user:pw@host/db`).
# This line is posted to Discord — scrub the DSN's userinfo BEFORE truncating
# to 60 chars, since truncation alone offers no guarantee the credentials
# land before the cut.
_DSN_USERINFO_RE = re.compile(r'postgres(ql)?://[^@\s]*@')

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


def summarize(entry_rows, exit_rows, cost_bps=None, bench_ids=None) -> dict:
    """Pure: rows -> the stats dict format_line renders.

    entry_rows: [{'ticker', 'bps', 'notional_usd', 'latency_s', 'strategy_id'}]
    exit_rows:  [{'ticker', 'bps', 'notional_usd', 'strategy_id'}]
    ('strategy_id' is optional on both — a row without one is never treated as
    a benchmark row.)

    cost_alpha_usd / cost_bench_usd are the SIGNED bp-weighted notional sums,
    split by whether the row's strategy_id is in bench_ids (default: none are
    — fail-open, same posture as benchmark_sleeve.load_benchmark_sleeve_ids).
    cost_usd is their sum, retained for convenience. A favourable fill offsets
    an adverse one within its own bucket — this is realized cost, not an
    adverse-only tally."""
    bench_ids = bench_ids or set()
    entry_rows = [r for r in (entry_rows or []) if r.get('bps') is not None]
    exit_rows = [r for r in (exit_rows or []) if r.get('bps') is not None]
    e_bps = [r['bps'] for r in entry_rows]
    x_bps = [r['bps'] for r in exit_rows]
    lat = [r['latency_s'] for r in entry_rows if r.get('latency_s') is not None]

    def _row_cost(r):
        return float(r['bps']) / 10000.0 * float(r.get('notional_usd') or 0.0)

    all_rows = entry_rows + exit_rows
    cost_bench = sum(_row_cost(r) for r in all_rows if r.get('strategy_id') in bench_ids)
    cost_alpha = sum(_row_cost(r) for r in all_rows if r.get('strategy_id') not in bench_ids)

    tickers = [r['ticker'] for r in entry_rows if r.get('ticker')]
    model = modelled_median_bps(tickers, cost_bps=cost_bps) if tickers else None
    e_median = _median(e_bps)
    return {
        'entry_n': len(e_bps), 'entry_mean': _mean(e_bps),
        'entry_median': e_median, 'entry_p90': _pctile(e_bps, 0.90),
        'exit_n': len(x_bps), 'exit_mean': _mean(x_bps),
        'latency_median_s': _median(lat),
        'cost_alpha_usd': cost_alpha,
        'cost_bench_usd': cost_bench,
        'cost_usd': cost_alpha + cost_bench,
        'modelled_median': model,
        'verdict': verdict(e_median, model),
    }


def _bp(v):
    return 'n/a' if v is None else f'{float(v):.1f}bp'


def _sec(v):
    return 'n/a' if v is None else f'{float(v):.1f}s'


def format_line(st: dict, run_date) -> str:
    """The one-line digest string. n=0 renders 'n/a', never a 0 that would read
    as 'we paid no slippage today'. cost is split alpha/bench (fix round 1) so
    the benchmark sleeve's one large rebalance leg never dominates a single
    undifferentiated dollar figure."""
    return (f"fill_slippage: entry n={st['entry_n']} mean={_bp(st['entry_mean'])} "
            f"median={_bp(st['entry_median'])} p90={_bp(st['entry_p90'])} | "
            f"exit n={st['exit_n']} mean={_bp(st['exit_mean'])} | "
            f"latency_med={_sec(st['latency_median_s'])} | "
            f"cost=${st['cost_alpha_usd']:,.0f} alpha / ${st['cost_bench_usd']:,.0f} bench | "
            f"modelled_med={_bp(st['modelled_median'])} verdict={st['verdict']} "
            f"asof={str(run_date)[:10]}")


# DISTINCT ON (per submission) is defense in depth for the real query plan;
# _dedupe_by_submission (Python, below) is the tested guarantee — see module
# docstring "Dedup (fix round 1)".
_ENTRY_SQL = """
    SELECT DISTINCT ON (s.run_date, s.strategy_id, s.ticker)
           es.ticker,
           es.fill_slippage_bps,
           COALESCE(s.filled_qty, 0) * COALESCE(es.broker_fill_price, 0) AS notional_usd,
           EXTRACT(EPOCH FROM (s.filled_at - s.submitted_at)) AS latency_s,
           s.strategy_id
      FROM execution_signals es
      JOIN alpaca_submissions s ON s.run_date = es.target_date
                               AND s.ticker = es.ticker
                               AND s.strategy_id = es.strategy_id
     WHERE es.target_date = %s
       AND es.fill_slippage_bps IS NOT NULL
     ORDER BY s.run_date, s.strategy_id, s.ticker, es.id
"""

# LEFT JOIN: an exit leg with no matching submission has s.strategy_id/ticker
# NULL — COALESCE to es.id (always unique) so those legs are never collapsed
# into each other by DISTINCT ON; only legs that genuinely share one
# submission row get deduped.
_EXIT_SQL = """
    SELECT DISTINCT ON (COALESCE(s.strategy_id, es.id::text), COALESCE(s.ticker, es.id::text))
           es.ticker,
           sp.exit_slippage_bps,
           COALESCE(sp.closed_price, 0) * COALESCE(s.filled_qty, 0) AS notional_usd,
           s.strategy_id
      FROM signal_pnl sp
      JOIN execution_signals es ON es.id = sp.signal_id
      LEFT JOIN alpaca_submissions s ON s.run_date = es.target_date
                                    AND s.ticker = es.ticker
                                    AND s.strategy_id = es.strategy_id
     WHERE sp.pnl_date = %s
       AND sp.exit_slippage_bps IS NOT NULL
     ORDER BY COALESCE(s.strategy_id, es.id::text), COALESCE(s.ticker, es.id::text), es.id
"""


def _dedupe_by_submission(rows):
    """Collapse rows that resolve to the SAME (strategy_id, ticker) submission
    down to one, keeping the first seen (rows arrive in the SQL's own
    deterministic ORDER BY). An overnight signal and a same-day signal — or a
    long+short flip — for the same strategy/ticker both join the SAME
    alpaca_submissions row; one physical fill must contribute its
    bp/notional/latency exactly once. A row with no resolvable submission
    (strategy_id is None) is never deduped against anything — it is distinct
    data, not a duplicate. run_date is not part of the key because every row
    passed to this function comes from a single load_entry_rows/
    load_exit_rows call already scoped to one run_date."""
    seen = set()
    out = []
    for r in rows:
        sid = r.get('strategy_id')
        if sid is None:
            out.append(r)
            continue
        key = (sid, r.get('ticker'))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def load_entry_rows(conn, run_date) -> list:
    with conn.cursor() as cur:
        cur.execute(_ENTRY_SQL, (run_date,))
        rows = [{'ticker': r[0], 'bps': float(r[1]),
                 'notional_usd': float(r[2] or 0.0),
                 'latency_s': (None if r[3] is None else float(r[3])),
                 'strategy_id': r[4]}
                for r in (cur.fetchall() or [])]
        return _dedupe_by_submission(rows)


def load_exit_rows(conn, run_date) -> list:
    with conn.cursor() as cur:
        cur.execute(_EXIT_SQL, (run_date,))
        rows = [{'ticker': r[0], 'bps': float(r[1]),
                 'notional_usd': float(r[2] or 0.0),
                 'strategy_id': r[3]}
                for r in (cur.fetchall() or [])]
        return _dedupe_by_submission(rows)


def fill_slippage_line(run_date, *, conn=None, cost_bps=None, bench_ids=None):
    """The full line — ALWAYS a string, never None and never raises. On any
    failure (a pre-migration-155 schema, a DB outage, anything) it renders
    'fill_slippage: n/a (<ExceptionClass>: <reason>)' so the failure is
    reported, not silently indistinguishable from "nothing happened today"
    (fix round 1). Owns its connection when conn is None — mirrors
    bench_realized.bench_realized_line:164-189. bench_ids defaults to the
    live benchmark-sleeve strategy ids (execution.benchmark_sleeve) read on
    the SAME connection, unless injected (tests always inject)."""
    import os
    own = conn is None
    try:
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        if bench_ids is None:
            from execution.benchmark_sleeve import load_benchmark_sleeve_ids
            bench_ids = load_benchmark_sleeve_ids(conn)
        st = summarize(load_entry_rows(conn, run_date),
                       load_exit_rows(conn, run_date),
                       cost_bps=cost_bps, bench_ids=bench_ids)
        return format_line(st, run_date)
    except Exception as e:  # noqa: BLE001
        scrubbed = _DSN_USERINFO_RE.sub('postgres://***@', str(e))
        reason = f'{type(e).__name__}: {scrubbed[:60]}'
        logger.warning('[fill_slippage] degraded (%s)', reason)
        return f'fill_slippage: n/a ({reason})'
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
