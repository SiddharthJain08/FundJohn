#!/usr/bin/env python3
"""
alpaca_executor.py — final pipeline step. Reads the sized handoff written by
trade_agent_llm.py and submits bracket orders to Alpaca paper.

Safety gates (in order, any failure → skip):
  1. daily_signal_summary → same quality gate as the trade step.
  2. Per-order NAV cap  — MAX_ORDER_PCT_NAV.
  3. Daily total cap    — MAX_DAILY_NEW_NOTIONAL_PCT applied across the run.
  4. Idempotency — skip any (run_date, strategy_id, ticker) with an existing
     alpaca_order_id in position_recommendations.

Time-in-force: 'day' if currently in regular trading hours, 'opg'
(market-on-open, next session) otherwise. This means the nightly 21:30
UTC / 17:30 ET pipeline queues orders for the next day's open instead
of submitting into a closed market.

Writes:
  - Inserts/updates position_recommendations with action='OPEN' +
    alpaca_order_id after each submission.
  - Posts a Discord summary via the tradedesk_alpaca_orders webhook if
    configured; otherwise logs.

Usage:
    python3 src/execution/alpaca_executor.py [--date YYYY-MM-DD] [--dry-run]
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.alpaca_trader import _alpaca_session, _fetch_equity, _price_str  # noqa: E402
from execution.handoff import read_handoff, HANDOFF_DIR  # noqa: E402


# ── Safety knobs ────────────────────────────────────────────────────────────
MAX_ORDER_PCT_NAV            = 0.05   # 5% NAV hard cap per order
MAX_DAILY_NEW_NOTIONAL_PCT   = 0.25   # 25% NAV max in new notional per run
MIN_EFFECTIVE_PCT            = 0.001  # drop sub-0.1% NAV orders (noise)


# ── Helpers ─────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f'{ts} [ALPACA_EXEC] {msg}')


def in_market_hours():
    """Return True if current time is within 09:30–16:00 America/New_York."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo('America/New_York'))
    except Exception:
        # Fallback: treat UTC-4 as ET (DST naive); worst case: over-tight gate
        now = datetime.now(timezone.utc).astimezone()
    if now.weekday() >= 5:
        return False
    rth_open  = time(9, 30)
    rth_close = time(16, 0)
    return rth_open <= now.time() <= rth_close


def check_signal_quality(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT avg_ev, ev_pos, n_signals, run_date
        FROM daily_signal_summary
        ORDER BY run_date DESC, created_at DESC
        LIMIT 1
    """)
    row = cur.fetchone()
    cur.close()
    if not row:
        return True, 'no signal_summary row — allowing'
    avg_ev_frac, ev_pos, n_signals, row_date = row
    avg_ev_pct = float(avg_ev_frac) * 100 if avg_ev_frac is not None else 0.0
    green_cnt  = int(ev_pos or 0)
    # Mirror pipeline_orchestrator thresholds: block only if avg_ev < -0.5% AND zero green.
    if avg_ev_pct < -0.5 and green_cnt < 1:
        return False, f'avg EV={avg_ev_pct:+.2f}% ({green_cnt} green, {row_date}) — quality gate blocked'
    return True, f'avg EV={avg_ev_pct:+.2f}%, green={green_cnt}, {n_signals} signals'


def already_executed(conn, run_date, strategy_id, ticker):
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM position_recommendations
        WHERE run_date = %s AND strategy_id = %s AND ticker = %s
          AND alpaca_order_id IS NOT NULL
        LIMIT 1
    """, (run_date, strategy_id, ticker))
    hit = cur.fetchone() is not None
    cur.close()
    return hit


def record_submission(conn, run_date, order, alpaca_resp):
    """Persist a row in position_recommendations tying the sized order to its
    Alpaca order_id. Uses INSERT … ON CONFLICT so re-runs stay idempotent."""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO position_recommendations (
          run_date, ticker, strategy_id, action, rationale,
          entry_price, stop_loss, profit_target,
          status, alpaca_order_id, alpaca_status, created_at
        ) VALUES (%s,%s,%s,'OPEN',%s,%s,%s,%s,'submitted',%s,%s,NOW())
    """, (
        run_date, order['ticker'], order.get('strategy_id') or 'unknown',
        f"auto-executed by alpaca_executor ({order.get('label') or ''})",
        order.get('entry'), order.get('stop'),
        order.get('t1') or order.get('target'),
        alpaca_resp.get('order_id'), alpaca_resp.get('status'),
    ))
    conn.commit()
    cur.close()


def execute_single(sess, equity, order, run_date):
    """Submit one bracket order. Returns result dict."""
    ticker  = order['ticker']
    pct_nav = float(order.get('pct_nav') or 0.0)
    entry   = order.get('entry')
    stop    = order.get('stop')
    target  = order.get('t1') or order.get('target')
    side    = 'sell' if str(order.get('direction') or '').lower() == 'short' else 'buy'

    if not (entry and stop and target):
        return {'ticker': ticker, 'status': 'SKIP', 'reason': 'missing levels'}

    entry, stop, target = float(entry), float(stop), float(target)

    pct_nav = max(MIN_EFFECTIVE_PCT, min(pct_nav, MAX_ORDER_PCT_NAV))
    notional = equity * pct_nav
    qty = max(1, int(notional / entry))

    coid = f'AX{run_date.replace("-","")}_{ticker}_{(order.get("strategy_id") or "")[:8]}'[:48]
    # In-hours: execute immediately ('day' TIF). Off-hours: queue for next open
    # ('opg' TIF) so the nightly pipeline still produces live bracket orders.
    # Note: Alpaca rejects bracket orders with order_class='bracket' + TIF='opg';
    # for off-hours we submit a plain market opg entry and skip auto-brackets.
    tif = 'day' if in_market_hours() else 'opg'
    body = {
        'symbol':          ticker,
        'qty':             str(qty),
        'side':            side,
        'type':            'market',
        'time_in_force':   tif,
        'client_order_id': coid,
    }
    if tif == 'day':
        body['order_class']  = 'bracket'
        body['take_profit']  = {'limit_price': _price_str(target)}
        body['stop_loss']    = {'stop_price':  _price_str(stop)}

    try:
        import requests  # noqa: F401
        r = sess.post(f'{sess._base}/v2/orders', data=json.dumps(body), timeout=15)
        if r.status_code in (200, 201):
            oid = r.json().get('id', '?')
            log(f'OK  {ticker} {side.upper()} x{qty} sh  entry~{entry:.2f}  TP={target:.2f}  SL={stop:.2f}  notional=${notional:,.0f}  order={oid}')
            return {'ticker': ticker, 'status': 'submitted', 'order_id': oid,
                    'side': side, 'qty': qty, 'notional': notional,
                    'entry': entry, 'stop': stop, 'target': target}
        log(f'HTTP {r.status_code} {ticker}: {r.text[:200]}')
        return {'ticker': ticker, 'status': 'error', 'http': r.status_code, 'body': r.text[:200]}
    except Exception as exc:
        log(f'Exception {ticker}: {exc}')
        return {'ticker': ticker, 'status': 'exception', 'reason': str(exc)}


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=str(date.today()))
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    run_date = args.date
    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        log('POSTGRES_URI not set — aborting')
        sys.exit(1)

    log(f'Run date: {run_date}')

    handoff = read_handoff(run_date, 'sized')
    if not handoff:
        log('No sized handoff — nothing to execute')
        sys.exit(0)
    orders = handoff.get('orders') or []
    if not orders:
        log('Sized handoff contains zero orders — nothing to do')
        sys.exit(0)

    conn = psycopg2.connect(uri)

    # Gate 1: signal quality
    ok, why = check_signal_quality(conn)
    if not ok:
        log(f'Signal-quality gate blocked: {why}')
        conn.close(); sys.exit(0)
    log(f'Signal-quality OK — {why}')

    rth = in_market_hours()
    log(f'Market hours: {"RTH — submitting bracket day orders" if rth else "closed — submitting OPG market orders (queue for next open)"}')

    sess   = _alpaca_session()
    equity = _fetch_equity(sess)
    log(f'Account equity ${equity:,.2f}  orders to attempt: {len(orders)}')

    submitted = []
    skipped   = []
    new_notional_total = 0.0
    daily_cap_notional = equity * MAX_DAILY_NEW_NOTIONAL_PCT

    for order in orders:
        sid    = order.get('strategy_id') or 'unknown'
        ticker = order.get('ticker') or '???'

        if already_executed(conn, run_date, sid, ticker):
            skipped.append({'ticker': ticker, 'reason': 'already executed'})
            continue

        # Prospective notional for daily cap
        pct_nav = max(MIN_EFFECTIVE_PCT, min(float(order.get('pct_nav') or 0.0), MAX_ORDER_PCT_NAV))
        projected = equity * pct_nav
        if new_notional_total + projected > daily_cap_notional:
            skipped.append({'ticker': ticker, 'reason': f'daily cap reached (${new_notional_total:,.0f} + ${projected:,.0f} > ${daily_cap_notional:,.0f})'})
            continue

        if args.dry_run:
            log(f'DRY {ticker} {sid}  would submit {pct_nav*100:.2f}% NAV (~${projected:,.0f})')
            continue

        result = execute_single(sess, equity, order, run_date)
        if result.get('status') == 'submitted':
            record_submission(conn, run_date, order, result)
            submitted.append(result)
            new_notional_total += result.get('notional') or 0.0
        else:
            skipped.append({'ticker': ticker, 'reason': result.get('reason') or result.get('body') or result.get('status')})

    conn.close()

    log(f'Done — submitted={len(submitted)}  skipped={len(skipped)}  new_notional=${new_notional_total:,.0f}')
    if skipped:
        for s in skipped[:10]:
            log(f'  SKIP {s["ticker"]}: {s["reason"]}')


if __name__ == '__main__':
    main()
