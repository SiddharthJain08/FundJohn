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
import subprocess
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

# Alpaca CLI binary. Override with ALPACA_CLI_BIN env var if installed elsewhere.
ALPACA_CLI = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')


# ── Helpers ─────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f'{ts} [ALPACA_EXEC] {msg}')


_market_hours_cache = {'is_open': None, 'cached_at': 0.0}


def in_market_hours():
    """Return True if regular trading hours per `alpaca clock`.

    Cached for 60s — the CLI subprocess shell-out is ~50–100 ms, and tight
    loops (orchestrator → alpaca_executor → execute_single) might call this
    several times per cycle. Falls back to a static 09:30–16:00 ET weekday
    window if the CLI is unavailable (e.g. credentials missing in tests).
    """
    import time as _time
    now = _time.time()
    if (_market_hours_cache['is_open'] is not None and
            now - _market_hours_cache['cached_at'] < 60.0):
        return _market_hours_cache['is_open']

    try:
        proc = subprocess.run(
            [ALPACA_CLI, 'clock'],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            payload = json.loads(proc.stdout)
            is_open = bool(payload.get('is_open'))
            _market_hours_cache.update({'is_open': is_open, 'cached_at': now})
            return is_open
        # CLI returned an error — log and fall through to static check.
        log(f'alpaca clock returned rc={proc.returncode} — falling back to static ET window')
    except Exception as exc:
        log(f'alpaca clock failed ({type(exc).__name__}: {exc}) — falling back to static ET window')

    # Static fallback: 09:30–16:00 America/New_York, Mon–Fri.
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo('America/New_York'))
    except Exception:
        now_et = datetime.now(timezone.utc).astimezone()
    if now_et.weekday() >= 5:
        return False
    rth_open  = time(9, 30)
    rth_close = time(16, 0)
    return rth_open <= now_et.time() <= rth_close


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
          alpaca_order_id, alpaca_status, alpaca_http, alpaca_error,
          submitted_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT (run_date, strategy_id, ticker) DO UPDATE SET
          alpaca_order_id = EXCLUDED.alpaca_order_id,
          alpaca_status   = EXCLUDED.alpaca_status,
          alpaca_http     = EXCLUDED.alpaca_http,
          alpaca_error    = EXCLUDED.alpaca_error,
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
        alpaca_resp.get('http'),
        alpaca_resp.get('body') or alpaca_resp.get('reason'),
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


def _run_alpaca_cli(args, timeout=30):
    """Run the alpaca CLI subprocess.

    Returns (ok, payload, err) where:
      ok=True  → payload is the parsed stdout JSON (or raw stdout if not JSON);
                 err is None.
      ok=False → payload is None; err is a dict with keys
                 {exit_code, status, code, error, error_json, raw_stderr}.
                 `status` is the HTTP status (422, 404, etc.) when the CLI
                 emits a structured error JSON to stderr; None otherwise.

    The CLI emits JSON to stdout on success and a JSON error envelope to
    stderr on failure (with `"status": <http>`, `"error": "..."`,
    `"code": <numeric>`). Exit code is 0 on success, non-zero on error.
    """
    proc = subprocess.run(
        [ALPACA_CLI, *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode == 0:
        try:
            return True, json.loads(proc.stdout), None
        except json.JSONDecodeError:
            return True, proc.stdout, None
    err = {
        'exit_code': proc.returncode,
        'raw_stderr': proc.stderr,
        'status': None,
        'error': proc.stderr.strip(),
        'code': None,
        'error_json': None,
    }
    try:
        ej = json.loads(proc.stderr)
        err['error_json'] = ej
        err['status'] = ej.get('status')
        err['code']   = ej.get('code')
        if ej.get('error'):
            err['error'] = ej.get('error')
    except json.JSONDecodeError:
        pass
    return False, None, err


def _submit_order_via_cli(*, ticker, side, qty, tif, order_class, target, stop, coid):
    """Submit a single bracket/simple order via `alpaca order submit`.
    Returns the same (ok, payload, err) tuple as _run_alpaca_cli."""
    args = [
        'order', 'submit',
        '--symbol',          ticker,
        '--side',             side,
        '--qty',              str(qty),
        '--type',             'market',
        '--time-in-force',    tif,
        '--client-order-id',  coid,
    ]
    if order_class == 'bracket':
        args += [
            '--order-class',  'bracket',
            '--take-profit',  json.dumps({'limit_price': _price_str(target)}),
            '--stop-loss',    json.dumps({'stop_price':  _price_str(stop)}),
        ]
    return _run_alpaca_cli(args)


def _get_order_by_coid(coid):
    return _run_alpaca_cli(['order', 'get-by-client-id', '--client-order-id', coid])


def execute_single(sess, equity, order, run_date):
    """Submit one bracket order. Returns result dict.

    Computes a unique `client_order_id` BEFORE any SKIP early-return so
    every result dict carries a unique coid (the alpaca_submissions table
    has a UNIQUE constraint on client_order_id; an empty string would
    collide on the second SKIP).
    """
    raw_ticker = order['ticker']
    ticker = _normalize_alpaca_symbol(raw_ticker)

    # Compute coid up-front from the RAW ticker + strategy_id so SKIP rows
    # also get a unique key. Alpaca allows 128 chars on client_order_id.
    import re as _re
    _coid_ticker = (ticker or raw_ticker or 'UNKNOWN').replace(' ', '_')
    _coid_ticker = _re.sub(r'[^A-Za-z0-9._-]', '_', _coid_ticker)
    _sid_clean = _re.sub(r'[^A-Za-z0-9._-]', '_', order.get('strategy_id') or 'unknown')
    coid = f'AX{run_date.replace("-","")}_{_coid_ticker}_{_sid_clean}'[:128]

    if ticker is None:
        return {'ticker': raw_ticker, 'status': 'SKIP',
                'reason': f'unsupported on Alpaca paper ({raw_ticker})',
                'client_order_id': coid}
    pct_nav = float(order.get('pct_nav') or 0.0)
    entry   = order.get('entry')
    stop    = order.get('stop')
    target  = order.get('t1') or order.get('target')
    side    = 'sell' if str(order.get('direction') or '').lower() == 'short' else 'buy'

    if not (entry and stop and target):
        return {'ticker': ticker, 'status': 'SKIP', 'reason': 'missing levels',
                'client_order_id': coid}

    entry, stop, target = float(entry), float(stop), float(target)

    pct_nav = max(MIN_EFFECTIVE_PCT, min(pct_nav, MAX_ORDER_PCT_NAV))
    notional = equity * pct_nav
    qty = max(1, int(notional / entry))
    # In-hours: execute immediately ('day' TIF). Off-hours: queue for next open
    # ('opg' TIF) so the nightly pipeline still produces live bracket orders.
    # Note: Alpaca rejects bracket orders with order_class='bracket' + TIF='opg';
    # for off-hours we submit a plain market opg entry and skip auto-brackets.
    tif = 'day' if in_market_hours() else 'opg'
    order_class = 'bracket' if tif == 'day' else 'simple'

    # Pre-flight stop adjustment: TradeJohn's stop is computed off the
    # signal-time entry, but Alpaca validates stop_loss against the
    # CURRENT base_price (latest trade). When the market drifts between
    # signal generation and submission, otherwise-valid brackets get
    # rejected with 422 "stop_loss.stop_price must be <= base_price -
    # 0.01" (longs) or ">= base_price + 0.01" (shorts). Refetching the
    # latest quote and snapping the stop to the validity boundary
    # rescues these orders rather than dropping them. Limited to
    # bracket day-orders — opg simple orders don't have brackets.
    adjusted_stop_note = None
    if tif == 'day':
        try:
            qr = sess.get(f'{sess._base}/v2/stocks/{ticker}/quotes/latest', timeout=5)
            if qr.status_code == 200:
                qj = qr.json().get('quote', {})
                bid = float(qj.get('bp') or 0.0)
                ask = float(qj.get('ap') or 0.0)
                # Use mid-price as the reference base; fall back to whichever side is non-zero.
                base = ((bid + ask) / 2.0) if (bid > 0 and ask > 0) else (bid or ask)
                if base > 0:
                    # Long: stop must be <= base − 0.01. If TradeJohn's stop is too
                    # high, snap to base − max(0.02, 0.5% of base) to leave a small
                    # margin against further drift.
                    if side == 'buy':
                        max_valid = base - 0.01
                        if stop >= max_valid:
                            new_stop = round(base - max(0.02, base * 0.005), 2)
                            adjusted_stop_note = f'stop adjusted long: {stop:.2f} → {new_stop:.2f} (base={base:.2f})'
                            stop = new_stop
                    # Short: stop must be >= base + 0.01.
                    elif side == 'sell':
                        min_valid = base + 0.01
                        if stop <= min_valid:
                            new_stop = round(base + max(0.02, base * 0.005), 2)
                            adjusted_stop_note = f'stop adjusted short: {stop:.2f} → {new_stop:.2f} (base={base:.2f})'
                            stop = new_stop
        except Exception as _qe:
            # Don't block submission on a quote fetch hiccup; let Alpaca's
            # own validation be the final word. Logged so we can see when
            # the snap path is silently failing (otherwise an order looks
            # like a "fresh" 422 from Alpaca rather than our pre-flight
            # whiffing).
            log(f'  ↯ {ticker}: pre-flight quote fetch failed ({type(_qe).__name__}: {_qe}) — submitting unsnapped')

    if adjusted_stop_note:
        log(f'  ↪ {ticker}: {adjusted_stop_note}')

    try:
        ok, payload, err = _submit_order_via_cli(
            ticker=ticker, side=side, qty=qty, tif=tif,
            order_class=order_class, target=target, stop=stop, coid=coid,
        )
        if ok:
            oid = (payload or {}).get('id', '?') if isinstance(payload, dict) else '?'
            log(f'OK  {ticker} {side.upper()} x{qty} sh  entry~{entry:.2f}  TP={target:.2f}  SL={stop:.2f}  notional=${notional:,.0f}  order={oid}')
            return {'ticker': ticker, 'status': 'submitted', 'order_id': oid,
                    'side': side, 'qty': qty, 'notional': notional,
                    'entry': entry, 'stop': stop, 'target': target,
                    'tif': tif, 'order_class': order_class, 'client_order_id': coid}

        err_text   = (err.get('error') or '').lower()
        err_status = err.get('status')

        # Duplicate client_order_id → recover the existing order so DB stays in sync.
        if err_status == 422 and 'client_order_id' in err_text:
            ok2, payload2, _err2 = _get_order_by_coid(coid)
            if ok2 and isinstance(payload2, dict):
                oid = payload2.get('id', '?')
                log(f'RECOVERED {ticker} (duplicate client_order_id) → existing order={oid}')
                return {'ticker': ticker, 'status': 'recovered', 'order_id': oid,
                        'side': side, 'qty': qty, 'notional': notional,
                        'entry': entry, 'stop': stop, 'target': target,
                        'tif': tif, 'order_class': order_class, 'client_order_id': coid}

        # 422 with base_price violation → Alpaca's error envelope carries the
        # base_price it validated against. If our pre-flight snap whiffed
        # (quote fetch returned 0/exception, or quote was stale by the time
        # the order hit), use the authoritative value from the 422 and retry
        # once with a freshly snapped stop.
        if err_status == 422 and side in ('buy', 'sell') and tif == 'day':
            ej     = err.get('error_json') or {}
            bp_raw = ej.get('base_price')
            if bp_raw and ('stop_loss' in err_text or 'base_price' in err_text):
                try:
                    bp = float(bp_raw)
                except (TypeError, ValueError):
                    bp = 0.0
                if bp > 0:
                    if side == 'buy':
                        new_stop = round(bp - max(0.02, bp * 0.005), 2)
                    else:
                        new_stop = round(bp + max(0.02, bp * 0.005), 2)
                    log(f'  ↻ {ticker}: 422 base_price={bp:.2f} — retry with stop {stop:.2f}→{new_stop:.2f}')
                    ok3, payload3, err3 = _submit_order_via_cli(
                        ticker=ticker, side=side, qty=qty, tif=tif,
                        order_class=order_class, target=target, stop=new_stop, coid=coid,
                    )
                    if ok3:
                        oid = (payload3 or {}).get('id', '?') if isinstance(payload3, dict) else '?'
                        log(f'OK  {ticker} {side.upper()} x{qty} sh  entry~{entry:.2f}  TP={target:.2f}  SL={new_stop:.2f}  notional=${notional:,.0f}  order={oid} (snap-retry)')
                        return {'ticker': ticker, 'status': 'submitted', 'order_id': oid,
                                'side': side, 'qty': qty, 'notional': notional,
                                'entry': entry, 'stop': new_stop, 'target': target,
                                'tif': tif, 'order_class': order_class, 'client_order_id': coid}
                    err = err3 or err

        body_text = (err.get('error') or '')[:200]
        http      = err.get('status') or 0
        log(f'CLI rc={err.get("exit_code")} status={http} {ticker}: {body_text}')
        return {'ticker': ticker, 'status': 'error', 'http': http,
                'body': body_text, 'client_order_id': coid,
                'tif': tif, 'order_class': order_class}
    except subprocess.TimeoutExpired as exc:
        log(f'CLI timeout {ticker}: {exc}')
        return {'ticker': ticker, 'status': 'exception', 'reason': f'cli timeout: {exc}',
                'client_order_id': coid, 'tif': tif, 'order_class': order_class}
    except Exception as exc:
        log(f'Exception {ticker}: {exc}')
        return {'ticker': ticker, 'status': 'exception', 'reason': str(exc),
                'client_order_id': coid, 'tif': tif, 'order_class': order_class}


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
        sys.exit(2)   # config error per Tier 3 exit-code discipline

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
        # Persist *every* attempt — submitted, recovered, AND rejects —
        # so the audit trail in alpaca_submissions reflects what we tried,
        # not just what succeeded. Reject rows carry alpaca_http +
        # alpaca_error and have alpaca_order_id NULL, so already_executed()
        # still treats them as "not yet executed" for retry semantics.
        record_submission(conn, run_date, order, result,
                          result.get('tif') or ('day' if rth else 'opg'),
                          result.get('order_class') or ('bracket' if rth else 'simple'),
                          result.get('client_order_id') or '')
        if result.get('status') in ('submitted', 'recovered'):
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
