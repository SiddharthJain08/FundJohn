"""alpaca_trader.py — Alpaca paper trading for FundJohn signals."""
import os
import json
import logging
from datetime import date

logger = logging.getLogger(__name__)

# Starting paper portfolio value — used as fallback if account equity fetch fails.
# Update this if the paper account is reset or funded differently.
PAPER_PORTFOLIO   = 100_000.0
MIN_KELLY_FLOOR   = 0.005   # minimum fraction used when kelly_pos==0 (yellow test signals)


def _alpaca_session():
    import requests
    sess = requests.Session()
    sess.headers.update({
        "APCA-API-KEY-ID":     os.environ.get("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        "Content-Type":        "application/json",
    })
    sess._base = os.environ.get(
        "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
    ).rstrip("/")
    return sess


def _fetch_equity(sess):
    """Return current paper account equity, falling back to PAPER_PORTFOLIO."""
    try:
        r = sess.get(f"{sess._base}/v2/account", timeout=10)
        r.raise_for_status()
        equity = float(r.json().get("equity") or 0)
        if equity > 0:
            logger.info(f"[Alpaca] Account equity: ${equity:,.2f}")
            return equity
    except Exception as exc:
        logger.warning(f"[Alpaca] equity fetch failed ({exc}) — using ${PAPER_PORTFOLIO:,.0f}")
    return PAPER_PORTFOLIO


def _price_str(price):
    """Format price for Alpaca: 2dp for stocks >= $1, 4dp for sub-dollar."""
    dp = 2 if price >= 1.0 else 4
    return f"{round(price, dp):.{dp}f}"


def execute_alpaca_orders(green_opts, run_date=None):
    """
    Submit bracket orders to Alpaca paper trading.

    Sizing:
      notional  = equity * kelly_pos
      qty       = floor(notional / entry_price), minimum 1 share

    kelly_pos is already half-Kelly and capped at MAX_POSITION_PCT (5%).
    For signals where kelly_pos==0 (e.g. yellow verification signals),
    MIN_KELLY_FLOOR (0.5%) is used so sizing is meaningful rather than
    defaulting to 1 share across all price levels.
    """
    if run_date is None:
        run_date = date.today().isoformat()

    sess    = _alpaca_session()
    equity  = _fetch_equity(sess)
    base    = sess._base
    results = []

    logger.info(f"[Alpaca] Sizing against equity=${equity:,.2f}  signals={len(green_opts)}")

    for opt in green_opts:
        ticker    = opt.get("ticker", "???")
        kelly_pos = float(opt.get("kelly_pos") or 0.0)
        entry     = opt.get("entry")
        stop      = opt.get("stop")
        target    = opt.get("t3") or opt.get("t2") or opt.get("t1") or opt.get("target")
        label     = str(opt.get("strat_label") or opt.get("label") or "")
        side      = "sell" if str(opt.get("direction") or "").lower() == "short" else "buy"

        if not (entry and stop and target):
            logger.warning(f"[Alpaca] {ticker}: missing price levels — skip")
            results.append({"ticker": ticker, "status": "SKIP", "reason": "missing levels"})
            continue

        entry  = float(entry)
        stop   = float(stop)
        target = float(target)

        if side == "buy" and not (stop < entry < target):
            logger.warning(
                f"[Alpaca] {ticker}: bracket invalid "
                f"(stop={stop:.2f} < entry={entry:.2f} < tp={target:.2f} failed)"
            )
            results.append({"ticker": ticker, "status": "SKIP", "reason": "invalid bracket"})
            continue

        # Use MIN_KELLY_FLOOR for zero-kelly signals (yellow test run) so sizing
        # reflects actual portfolio scale rather than forcing 1 share on everything.
        effective_kelly = kelly_pos if kelly_pos > 0 else MIN_KELLY_FLOOR
        notional        = equity * effective_kelly
        qty             = max(1, int(notional / entry))

        logger.info(
            f"[Alpaca] {ticker}: kelly_pos={kelly_pos*100:.2f}%  "
            f"effective={effective_kelly*100:.2f}%  "
            f"notional=${notional:,.0f}  entry=${entry:.2f}  qty={qty}"
        )

        import time as _t
        coid = f"FJ{run_date.replace('-','')}_{ticker}_{label[:4]}_{_t.time_ns()//1_000_000_000 % 10000}"[:48]

        body = {
            "symbol":          ticker,
            "qty":             str(qty),
            "side":            side,
            "type":            "market",
            "time_in_force":   "day",
            "order_class":     "bracket",
            "take_profit":     {"limit_price": _price_str(target)},
            "stop_loss":       {"stop_price":  _price_str(stop)},
            "client_order_id": coid,
        }

        try:
            r = sess.post(f"{base}/v2/orders", data=json.dumps(body), timeout=15)
            if r.status_code in (200, 201):
                oid = r.json().get("id", "?")
                logger.info(
                    f"[Alpaca] OK {ticker} {side.upper()} x{qty}sh "
                    f"entry~{entry:.2f} TP={target:.2f} SL={stop:.2f} "
                    f"kelly={effective_kelly*100:.1f}% notional=${notional:,.0f} order={oid}"
                )
                results.append({
                    "ticker":    ticker,
                    "status":    "SUBMITTED",
                    "order_id":  oid,
                    "side":      side,
                    "qty":       qty,
                    "entry":     entry,
                    "tp":        target,
                    "sl":        stop,
                    "kelly_pos": effective_kelly,
                    "notional":  notional,
                    "label":     label,
                })
            else:
                logger.warning(f"[Alpaca] {ticker}: HTTP {r.status_code} {r.text[:200]}")
                results.append({
                    "ticker": ticker, "status": "ERROR",
                    "http": r.status_code, "body": r.text[:200],
                })
        except Exception as exc:
            logger.warning(f"[Alpaca] {ticker}: {exc}")
            results.append({"ticker": ticker, "status": "EXCEPTION", "reason": str(exc)})

    return results


def get_positions(sess):
    """Return list of open positions from Alpaca paper account."""
    try:
        r = sess.get(f"{sess._base}/v2/positions", timeout=10)
        r.raise_for_status()
        out = []
        for p in r.json():
            out.append({
                'symbol':            p.get('symbol', ''),
                'qty':               int(float(p.get('qty', 0))),
                'avg_entry_price':   float(p.get('avg_entry_price') or 0),
                'current_price':     float(p.get('current_price') or 0),
                'unrealized_plpc':   float(p.get('unrealized_plpc') or 0),
                'side':              p.get('side', 'long'),
                'market_value':      float(p.get('market_value') or 0),
            })
        return out
    except Exception as exc:
        logger.warning(f"[Alpaca] get_positions failed: {exc}")
        return []


def get_portfolio_history(sess):
    """Return daily + weekly P&L data from Alpaca portfolio history endpoint."""
    result = {
        'equity_now':     0.0,
        'equity_1d_ago':  0.0,
        'equity_1w_ago':  0.0,
        'daily_pnl':      0.0,
        'weekly_pnl':     0.0,
        'daily_pnl_pct':  0.0,
        'weekly_pnl_pct': 0.0,
    }
    equity_now = _fetch_equity(sess)
    result['equity_now'] = equity_now

    period_map = [('1D', '1d', 'daily'), ('1W', '1w', 'weekly')]
    for period, ago_key, label in period_map:
        try:
            r = sess.get(
                f"{sess._base}/v2/account/portfolio/history",
                params={'period': period, 'timeframe': '1D'},
                timeout=10,
            )
            r.raise_for_status()
            data     = r.json()
            equities = data.get('equity', [])
            if equities and len(equities) >= 2:
                past = float(equities[0] or 0)
                result[f'equity_{ago_key}_ago'] = past
                pnl     = equity_now - past
                pnl_pct = (pnl / past * 100) if past > 0 else 0.0
                result[f'{label}_pnl']     = pnl
                result[f'{label}_pnl_pct'] = pnl_pct
        except Exception as exc:
            logger.warning(f"[Alpaca] portfolio history ({period}) failed: {exc}")

    return result


def execute_recommendation(rec_id, postgres_uri):
    """
    Load a position_recommendations row by id, execute the action on Alpaca,
    and update the DB row with the result.
    Returns dict: {ok, order_id, action, ticker, detail}
    """
    import psycopg2, psycopg2.extras

    conn = psycopg2.connect(postgres_uri)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM position_recommendations WHERE id=%s", [rec_id])
    rec = cur.fetchone()
    if not rec:
        conn.close()
        return {'ok': False, 'error': f'rec {rec_id} not found'}
    if rec['status'] != 'pending':
        conn.close()
        return {'ok': False, 'error': f'rec already {rec["status"]}'}

    ticker = rec['ticker']
    action = rec['action']
    sess   = _alpaca_session()
    base   = sess._base

    order_id    = None
    alpaca_stat = None
    alpaca_err  = None
    ok          = False
    detail      = ''

    try:
        if action == 'EXIT_EARLY':
            r = sess.delete(f"{base}/v2/positions/{ticker}", params={'percentage': '100'}, timeout=15)
            if r.status_code in (200, 207):
                ok       = True
                order_id = (r.json() or [{}])[0].get('id') if isinstance(r.json(), list) else r.json().get('id')
                alpaca_stat = 'filled'
                detail      = f'position closed via DELETE /v2/positions/{ticker}'
            elif r.status_code in (403, 404):
                # Fallback: market sell order
                positions = get_positions(sess)
                pos = next((p for p in positions if p['symbol'] == ticker), None)
                qty = pos['qty'] if pos else 1
                body = {
                    'symbol': ticker, 'qty': str(qty), 'side': 'sell',
                    'type': 'market', 'time_in_force': 'day',
                }
                r2 = sess.post(f"{base}/v2/orders", json=body, timeout=15)
                if r2.status_code in (200, 201):
                    ok = True
                    order_id = r2.json().get('id')
                    alpaca_stat = 'submitted'
                    detail = f'market sell fallback x{qty}sh'
                else:
                    alpaca_err = r2.text[:200]
                    detail = f'sell fallback failed: {r2.status_code}'
            else:
                alpaca_err = r.text[:200]
                detail = f'DELETE failed: {r.status_code}'

        elif action == 'INCREASE_SIZE':
            equity = _fetch_equity(sess)
            entry  = float(rec['entry_price'])
            stop   = float(rec['stop_loss']) if rec['stop_loss'] else entry * 0.95
            target = float(rec['profit_target']) if rec['profit_target'] else entry * 1.10
            notional = equity * MIN_KELLY_FLOOR
            qty = max(1, int(notional / entry))
            body = {
                'symbol': ticker, 'qty': str(qty), 'side': 'buy',
                'type': 'market', 'time_in_force': 'day',
                'order_class': 'bracket',
                'take_profit': {'limit_price': _price_str(target)},
                'stop_loss':   {'stop_price':  _price_str(stop)},
            }
            r = sess.post(f"{base}/v2/orders", json=body, timeout=15)
            if r.status_code in (200, 201):
                ok = True
                order_id = r.json().get('id')
                alpaca_stat = 'submitted'
                detail = f'added x{qty}sh notional=${notional:,.0f}'
            else:
                alpaca_err = r.text[:200]
                detail = f'order failed: {r.status_code}'

        elif action == 'REDUCE_SIZE':
            positions = get_positions(sess)
            pos = next((p for p in positions if p['symbol'] == ticker), None)
            qty = max(1, (pos['qty'] // 3)) if pos and pos['qty'] > 0 else 1
            body = {
                'symbol': ticker, 'qty': str(qty), 'side': 'sell',
                'type': 'market', 'time_in_force': 'day',
            }
            r = sess.post(f"{base}/v2/orders", json=body, timeout=15)
            if r.status_code in (200, 201):
                ok = True
                order_id = r.json().get('id')
                alpaca_stat = 'submitted'
                detail = f'sold x{qty}sh (1/3 reduction)'
            else:
                alpaca_err = r.text[:200]
                detail = f'reduce failed: {r.status_code}'

        else:
            detail = f'action {action} not executable'

    except Exception as exc:
        alpaca_err = str(exc)
        detail = f'exception: {exc}'

    # Update DB
    try:
        cur.execute(
            """UPDATE position_recommendations
               SET status=%s, resolved_at=NOW(), resolved_by='bot-button',
                   alpaca_order_id=%s, alpaca_status=%s, alpaca_error=%s
               WHERE id=%s""",
            ['approved' if ok else 'pending', order_id, alpaca_stat, alpaca_err, rec_id]
        )
        conn.commit()
    except Exception as dbe:
        logger.warning(f"[execute_recommendation] DB update failed: {dbe}")
    finally:
        conn.close()

    return {'ok': ok, 'order_id': order_id, 'action': action, 'ticker': ticker, 'detail': detail}


def build_alpaca_post(alpaca_results, run_date=None):
    if not alpaca_results:
        return ""
    if run_date is None:
        run_date = date.today().isoformat()

    ok  = [r for r in alpaca_results if r["status"] == "SUBMITTED"]
    err = [r for r in alpaca_results if r["status"] != "SUBMITTED"]
    lines = [f"**Alpaca Paper Orders — {run_date}**"]

    for r in ok:
        notional_str = f"${r.get('notional', 0):,.0f}"
        lines.append(
            f"  OK {r['ticker']} {r['side'].upper()} x{r['qty']}sh"
            f" | entry~{r['entry']:.2f}  TP={r['tp']:.2f}  SL={r['sl']:.2f}"
            f" | {r['kelly_pos']*100:.1f}% = {notional_str}  [{r['label']}]"
        )
    for r in err:
        detail = r.get("reason") or r.get("body") or ""
        lines.append(f"  ERR {r['ticker']} {r['status']}: {str(detail)[:80]}")

    return "\n".join(lines)
