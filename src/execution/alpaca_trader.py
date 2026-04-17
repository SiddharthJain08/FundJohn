"""alpaca_trader.py — Alpaca paper trading for FundJohn signals."""
import os
import json
import logging
from datetime import date

logger = logging.getLogger(__name__)


def _alpaca_session():
    import requests
    sess = requests.Session()
    sess.headers.update({
        "APCA-API-KEY-ID":     os.environ.get("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        "Content-Type":        "application/json",
    })
    sess._base = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
    return sess


def execute_alpaca_orders(green_opts, run_date=None):
    if run_date is None:
        run_date = date.today().isoformat()
    sess = _alpaca_session()
    base = sess._base
    results = []
    try:
        eq = sess.get(f"{base}/v2/account", timeout=10)
        eq.raise_for_status()
        equity = float(eq.json().get("equity", 0))
        logger.info(f"[Alpaca] equity=${equity:,.2f}")
    except Exception as exc:
        logger.warning(f"[Alpaca] account fetch failed: {exc}")
        equity = 0.0
    for opt in green_opts:
        ticker    = opt.get("ticker", "???")
        kelly_pos = float(opt.get("kelly_pos") or 0.0)
        entry     = opt.get("entry")
        stop      = opt.get("stop")
        target    = opt.get("t3") or opt.get("t2") or opt.get("t1") or opt.get("target")
        label     = str(opt.get("strat_label") or opt.get("label") or "")
        side      = "sell" if str(opt.get("direction") or "").lower() == "short" else "buy"
        if not (entry and stop and target):
            logger.warning(f"[Alpaca] {ticker}: missing levels — skip")
            results.append({"ticker": ticker, "status": "SKIP", "reason": "missing levels"})
            continue
        entry = float(entry)
        stop  = float(stop)
        target = float(target)
        if side == "buy" and not (stop < entry < target):
            logger.warning(f"[Alpaca] {ticker}: bracket invalid stop={stop:.2f} entry={entry:.2f} tp={target:.2f}")
            results.append({"ticker": ticker, "status": "SKIP", "reason": "invalid bracket"})
            continue
        notional = equity * kelly_pos if equity > 0 else 0.0
        qty  = max(1, int(notional / entry))
        coid = f"FJ{run_date.replace('-','')}_{ticker}_{label[:4]}_{__import__('time').time_ns()//1_000_000_000 % 10000}"[:48]
        body = {
            "symbol": ticker, "qty": str(qty), "side": side,
            "type": "market", "time_in_force": "day", "order_class": "bracket",
            "take_profit": {"limit_price": f"{round(target, 2):.2f}"},
            "stop_loss":   {"stop_price":  f"{round(stop, 2):.2f}"},
            "client_order_id": coid,
        }
        try:
            r = sess.post(f"{base}/v2/orders", data=json.dumps(body), timeout=15)
            if r.status_code in (200, 201):
                oid = r.json().get("id", "?")
                logger.info(f"[Alpaca] OK {ticker} {side.upper()} x{qty} entry~{entry:.2f} TP={target:.2f} SL={stop:.2f} order={oid}")
                results.append({"ticker": ticker, "status": "SUBMITTED", "order_id": oid,
                                 "side": side, "qty": qty, "entry": entry,
                                 "tp": target, "sl": stop, "kelly_pos": kelly_pos, "label": label})
            else:
                logger.warning(f"[Alpaca] {ticker}: HTTP {r.status_code} {r.text[:200]}")
                results.append({"ticker": ticker, "status": "ERROR",
                                 "http": r.status_code, "body": r.text[:200]})
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
        lines.append(
            f"  OK {r['ticker']} {r['side'].upper()} x{r['qty']} sh"
            f" | entry~{r['entry']:.2f} TP={r['tp']:.2f} SL={r['sl']:.2f}"
            f" | Kelly {r['kelly_pos']*100:.1f}% [{r['label']}]"
        )
    for r in err:
        detail = r.get("reason") or r.get("body") or ""
        lines.append(f"  ERR {r['ticker']} {r['status']}: {str(detail)[:80]}")
    return "\n".join(lines)
