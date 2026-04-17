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
