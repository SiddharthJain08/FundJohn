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
        SELECT 1 FROM alpaca_submissions
        WHERE run_date = %s AND strategy_id = %s AND ticker = %s
          AND alpaca_order_id IS NOT NULL
        LIMIT 1
    """, (run_date, strategy_id, ticker))
    hit = cur.fetchone() is not None
    cur.close()
    return hit


def record_submission(conn, run_date, order, alpaca_resp, tif, order_class, coid):
    """Persist a row in alpaca_submissions tying the sized order to its
    Alpaca order_id. UNIQUE (run_date, strategy_id, ticker) makes re-runs
    idempotent."""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO alpaca_submissions (
          run_date, ticker, strategy_id, direction, qty,
          entry_price, stop_price, target_price, pct_nav, notional_usd,
          time_in_force, order_class, client_order_id,
          alpaca_order_id, alpaca_status, submitted_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT (run_date, strategy_id, ticker) DO UPDATE SET
          alpaca_order_id = EXCLUDED.alpaca_order_id,
          alpaca_status   = EXCLUDED.alpaca_status,
          submitted_at    = EXCLUDED.submitted_at
    """, (
        run_date, order['ticker'], order.get('strategy_id') or 'unknown',
        (order.get('direction') or 'long').lower(),
        alpaca_resp.get('qty') or 0,
        alpaca_resp.get('entry') or order.get('entry'),
        order.get('stop'),
        order.get('t1') or order.get('target'),
        order.get('pct_nav'),
        alpaca_resp.get('notional'),
        tif, order_class, coid,
        alpaca_resp.get('order_id'),
        alpaca_resp.get('status'),
    ))
    conn.commit()
    cur.close()


def _normalize_alpaca_symbol(raw: str) -> str | None:
    """Map engine/handoff ticker → Alpaca-accepted symbol.
    Returns None for symbols Alpaca paper doesn't support (futures, indices,
    FX pairs, crypto not on Alpaca's list) — caller must skip those."""
    if not raw:
        return None
    t = raw.strip().upper()
    # Yahoo class-share convention uses a dash; Alpaca wants a dot.
    if '.' not in t and '-' in t and t.count('-') == 1 and len(t.split('-')[1]) <= 2 and not t.endswith('-USD'):
        t = t.replace('-', '.')
    # Known non-equity symbols Alpaca paper rejects — skip cleanly.
    if t.startswith('^'):                       return None   # indices (^VIX, ^GSPC, ^DJI, …)
    if '=' in t:                                return None   # futures/FX (NG=F, GC=F, AUDUSD=X, …)
    if t.endswith('-USD'):                      return None   # crypto tickers not in Alpaca universe
    return t


def execute_single(sess, equity, order, run_date):
    """Submit one bracket order. Returns result dict."""
    raw_ticker = order['ticker']
    ticker = _normalize_alpaca_symbol(raw_ticker)
    if ticker is None:
        return {'ticker': raw_ticker, 'status': 'SKIP', 'reason': f'unsupported on Alpaca paper ({raw_ticker})'}
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
    order_class = 'bracket' if tif == 'day' else 'simple'
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
                    'entry': entry, 'stop': stop, 'target': target,
                    'tif': tif, 'order_class': order_class, 'client_order_id': coid}
        # Duplicate client_order_id → recover the existing order so DB stays in sync.
        if r.status_code == 422 and 'client_order_id' in (r.text or '').lower():
            try:
                g = sess.get(f'{sess._base}/v2/orders:by_client_order_id', params={'client_order_id': coid}, timeout=10)
                if g.status_code == 200:
                    oid = g.json().get('id', '?')
                    log(f'RECOVERED {ticker} (duplicate client_order_id) → existing order={oid}')
                    return {'ticker': ticker, 'status': 'recovered', 'order_id': oid,
                            'side': side, 'qty': qty, 'notional': notional,
                            'entry': entry, 'stop': stop, 'target': target,
                            'tif': tif, 'order_class': order_class, 'client_order_id': coid}
            except Exception:
                pass
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
        if result.get('status') in ('submitted', 'recovered'):
            record_submission(conn, run_date, order, result,
                              result.get('tif') or 'day',
                              result.get('order_class') or 'simple',
                              result.get('client_order_id') or '')
            submitted.append(result)
            new_notional_total += result.get('notional') or 0.0
        else:
            skipped.append({'ticker': ticker, 'reason': result.get('reason') or result.get('body') or result.get('status')})

    conn.close()

    log(f'Done — submitted={len(submitted)}  skipped={len(skipped)}  new_notional=${new_notional_total:,.0f}')
    if skipped:
        for s in skipped[:10]:
            log(f'  SKIP {s["ticker"]}: {s["reason"]}')

    # Soft-fail on partial: when orders were requested but we couldn't submit
    # all of them, surface it to the operator explicitly. We still return 0
    # so the pipeline's `report` step runs and posts the full picture to
    # Discord — partial is an observability concern, not a pipeline abort.
    if submitted and skipped:
        _alert_partial(run_date, len(submitted), len(orders), skipped)


def _alert_partial(run_date, n_ok, n_total, skipped):
    """One line to #trade-reports when some orders didn't make it in.
    Uses the DataBot REST API (same pattern as send_report.py)."""
    import requests as _rq
    token = os.environ.get('DATABOT_TOKEN') or os.environ.get('BOT_TOKEN', '')
    if not token:
        return
    headers = {'Authorization': f'Bot {token}'}
    try:
        r = _rq.get('https://discord.com/api/v10/users/@me/guilds', headers=headers, timeout=5)
        if not r.ok:
            return
        cid = None
        for g in r.json():
            rc = _rq.get(f"https://discord.com/api/v10/guilds/{g['id']}/channels", headers=headers, timeout=5)
            if not rc.ok:
                continue
            for ch in rc.json():
                if ch.get('name') == 'trade-reports' and ch.get('type') == 0:
                    cid = ch['id']
                    break
            if cid:
                break
        if not cid:
            return
        reasons = ', '.join(f"{s['ticker']}({(s['reason'] or 'unknown')[:40]})" for s in skipped[:8])
        msg = (f'⚠️ **Alpaca partial submit — {run_date}** — {n_ok}/{n_total} orders in; '
               f'{n_total - n_ok} skipped: {reasons}{" …" if len(skipped) > 8 else ""}')
        _rq.post(f'https://discord.com/api/v10/channels/{cid}/messages',
                 headers={**headers, 'Content-Type': 'application/json'},
                 json={'content': msg[:1900]}, timeout=5)
    except Exception as e:
        log(f'partial-submit alert failed: {e}')


if __name__ == '__main__':
    main()
