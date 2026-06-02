"""Where the order to 'work' comes from. NO re-sizing — B1 measures timing of the
same qty. live_shadow_orders: today's actual opens (alpaca_submissions).
case_study_orders: curated historical opens where execution mattered
(signal_pnl ⨝ execution_signals).

Grounding (2026-06-02): `execution_signals` has NO `signal_id` column — its PK `id`
(uuid) is the FK target of `signal_pnl.signal_id`; join on `es.id = sp.signal_id`.
`target_date` is NULL for all historical signals (it's the new SP-6 EOD-lane column),
so case studies key off `es.signal_date` and resolve the worked session (= T+1, the
first bar-session strictly after signal_date) downstream via `resolve_next_session`."""
from __future__ import annotations
import os
import psycopg2
import psycopg2.extras

SIZE_CAP = 0.25   # daily sizing cap → normalizes a sizing pct into s_i ∈ [0,1]


def _env(key):
    """Read a key from /root/openclaw/.env (the worktree has no .env — gitignored)."""
    val = os.environ.get(key)
    if val:
        return val
    for line in open('/root/openclaw/.env'):
        if line.startswith(key + '='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    return None


def _conn():
    return psycopg2.connect(_env('POSTGRES_URI'))


def si_from_pct(pct):
    if pct is None:
        return 0.0
    return max(0.0, min(1.0, float(pct) / SIZE_CAP))


def _sign(direction):
    return 1.0 if str(direction or '').upper() in ('LONG', 'BUY') else -1.0


def live_shadow_orders(run_date):
    """run_date 'YYYY-MM-DD' (the T+1 session). Today's equity opens Phase A executed.
    run_date is exact (resolve_next_session=False)."""
    out = []
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT ticker, direction, qty, pct_nav, filled_avg_price,
                      official_close, strategy_id
               FROM alpaca_submissions
               WHERE run_date = %s AND instrument_class = 'equity'
                 AND qty IS NOT NULL AND qty <> 0""", (run_date,))
        for r in cur.fetchall():
            out.append({
                'ticker': r['ticker'], 'signed_qty': _sign(r['direction']) * float(r['qty']),
                's_i': si_from_pct(r['pct_nav']), 'run_date': run_date,
                'naive_fill': float(r['filled_avg_price']) if r['filled_avg_price'] else None,
                'close_t1': float(r['official_close']) if r['official_close'] else None,
                'strategy_id': r['strategy_id'], 'regime_state': None, 'signal_id': None,
                'resolve_next_session': False})
    return out


def case_study_orders(n_losers=25, n_movers=25):
    """Curated: heaviest realized losers + biggest realized gainers. Unit qty (±1) —
    case studies report per-share beat-close (bps); $ scales linearly. close_t1/naive
    resolved later from bars. run_date carries es.signal_date as a lower bound; the
    worked T+1 session is resolved from the bars panel (resolve_next_session=True)."""
    base = """
        SELECT es.ticker, es.direction, es.position_size_pct, es.regime_state,
               es.id::text AS signal_id, es.strategy_id, es.signal_date,
               sp.realized_pnl_pct
        FROM signal_pnl sp JOIN execution_signals es ON es.id = sp.signal_id
        WHERE sp.status = 'closed' AND sp.realized_pnl_pct IS NOT NULL
              AND es.signal_date IS NOT NULL
        ORDER BY {order} LIMIT %s"""
    rows = []
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(base.format(order="sp.realized_pnl_pct ASC"), (n_losers,))
        rows += cur.fetchall()
        cur.execute(base.format(order="sp.realized_pnl_pct DESC"), (n_movers,))
        rows += cur.fetchall()
    out, seen = [], set()
    for r in rows:
        if r['signal_id'] in seen:
            continue
        seen.add(r['signal_id'])
        out.append({
            'ticker': r['ticker'], 'signed_qty': _sign(r['direction']) * 1.0,
            's_i': si_from_pct(r['position_size_pct']),
            'run_date': r['signal_date'].isoformat() if r['signal_date'] else None,
            'naive_fill': None, 'close_t1': None, 'strategy_id': r['strategy_id'],
            'regime_state': r['regime_state'], 'signal_id': r['signal_id'],
            'resolve_next_session': True})
    return out
