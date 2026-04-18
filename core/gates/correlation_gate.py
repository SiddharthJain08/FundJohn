"""Correlation and concentration gate for pre-trade signal filtering.

Two checks (applied in order per signal):
  1. Concentration gate  -- reject if open_position_count >= MAX_POSITIONS (8)
  2. Correlation gate    -- reject if max |Pearson corr| vs active positions > 0.75
                           using last 20 trading-day log-returns

Fail-open: if price data is unavailable for a signal ticker, the signal is
approved. This avoids silently blocking trades due to data outages.

Correlation is checked signal-vs-existing-positions only, not signal-vs-signal.
"""

import json
import logging
import os
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

CORR_THRESHOLD = 0.75
MAX_POSITIONS  = 8
LOOKBACK_DAYS  = 20
COSTS_LOG      = Path(__file__).resolve().parent.parent.parent / "data" / "costs.jsonl"


def _fetch_ohlcv(ticker: str, lookback: int = LOOKBACK_DAYS + 5) -> list[float]:
    """Fetch daily close prices from FMP. Returns list of closes, oldest first."""
    api_key = os.environ.get("FMP_API_KEY", "")
    end   = datetime.utcnow().date()
    start = end - timedelta(days=lookback * 2)
    url   = f"https://financialmodelingprep.com/api/v3/historical-price-full/{ticker}"
    r = requests.get(
        url, params={"from": str(start), "to": str(end), "apikey": api_key}, timeout=15
    )
    r.raise_for_status()
    hist = r.json().get("historical", [])
    closes = [d["close"] for d in sorted(hist, key=lambda x: x["date"])]
    return closes[-lookback:]


def _log_rejection(ticker: str, reason: str, detail: str, costs_path: Path) -> None:
    costs_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts":     datetime.utcnow().isoformat(),
        "ticker": ticker,
        "reason": reason,
        "detail": detail,
    }
    with open(costs_path, "a") as f:
        f.write(json.dumps(record) + "\n")


def _post_alert(msg: str) -> None:
    url = os.environ.get("DISCORD_ALERTS_WEBHOOK", "")
    if not url:
        return
    try:
        requests.post(url, json={"content": msg[:1990]}, timeout=10)
    except Exception:
        pass


def _log_returns(prices: list[float]) -> "np.ndarray":
    p = np.array(prices, dtype=float)
    return np.diff(np.log(p + 1e-9))


def run_gate(
    signal_tickers: list[str],
    active_position_tickers: list[str],
    open_position_count: int,
    *,
    price_fetcher: Callable = _fetch_ohlcv,
    alert_fn: Callable = _post_alert,
    costs_path: Path = COSTS_LOG,
) -> "tuple[list[str], list[str]]":
    """
    Run correlation and concentration gates.

    Returns (approved_tickers, rejected_tickers).
    """
    approved = []
    rejected = []
    available_slots = MAX_POSITIONS - open_position_count

    pos_returns = {}
    for pt in active_position_tickers:
        try:
            prices = price_fetcher(pt)
            pos_returns[pt] = (
                _log_returns(prices[-LOOKBACK_DAYS:]) if len(prices) >= LOOKBACK_DAYS else None
            )
        except Exception as e:
            logger.warning(f"[corr_gate] price fetch failed for position {pt}: {e}")
            pos_returns[pt] = None

    for ticker in signal_tickers:
        if available_slots <= 0:
            reason = "concentration_limit"
            detail = f"open_positions={open_position_count} >= max={MAX_POSITIONS}"
            _log_rejection(ticker, reason, detail, costs_path)
            alert_fn(f":no_entry: **Gate rejected `{ticker}`** -- {reason}: {detail}")
            rejected.append(ticker)
            continue

        try:
            sig_prices = price_fetcher(ticker)
        except Exception as e:
            logger.warning(f"[corr_gate] price fetch failed for {ticker}: {e} -- approved (fail-open)")
            approved.append(ticker)
            available_slots -= 1
            continue

        if len(sig_prices) < LOOKBACK_DAYS:
            logger.warning(
                f"[corr_gate] insufficient history for {ticker} "
                f"({len(sig_prices)} days) -- approved (fail-open)"
            )
            approved.append(ticker)
            available_slots -= 1
            continue

        sig_ret      = _log_returns(sig_prices[-LOOKBACK_DAYS:])
        max_corr     = 0.0
        max_corr_pos = None

        for pt, pr in pos_returns.items():
            if pr is None:
                continue
            n = min(len(sig_ret), len(pr))
            if n < 5:
                continue
            c = float(np.corrcoef(sig_ret[-n:], pr[-n:])[0, 1])
            if abs(c) > abs(max_corr):
                max_corr     = c
                max_corr_pos = pt

        if abs(max_corr) > CORR_THRESHOLD:
            reason = "correlation_gate"
            detail = (
                f"max_corr={max_corr:.3f} with {max_corr_pos} "
                f"(threshold={CORR_THRESHOLD})"
            )
            _log_rejection(ticker, reason, detail, costs_path)
            alert_fn(f":no_entry: **Gate rejected `{ticker}`** -- {reason}: {detail}")
            rejected.append(ticker)
        else:
            approved.append(ticker)
            available_slots -= 1

    return approved, rejected
