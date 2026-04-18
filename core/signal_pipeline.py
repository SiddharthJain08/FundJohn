"""signal_pipeline.py -- Pre-trade gate layer between signal generation and Alpaca.

Entry point: run_pipeline(green_opts, run_date) -> list[dict]

Fetches live Alpaca positions, runs the correlation/concentration gate,
then submits only approved signals for order execution.
"""

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from execution.alpaca_trader import _alpaca_session, get_positions, execute_alpaca_orders
from core.gates.correlation_gate import run_gate

logger = logging.getLogger(__name__)


def run_pipeline(green_opts: list, run_date=None) -> list:
    """
    Gate green signals through correlation/concentration checks, then submit to Alpaca.

    Returns Alpaca submission results for approved signals only.
    """
    if not green_opts:
        return []

    sess             = _alpaca_session()
    positions        = get_positions(sess)
    position_tickers = [p["symbol"] for p in positions]
    open_count       = len(positions)

    signal_tickers = [o["ticker"] for o in green_opts]
    logger.info(
        f"[signal_pipeline] {len(signal_tickers)} signal(s), {open_count} open position(s)"
    )

    approved_tickers, rejected_tickers = run_gate(
        signal_tickers,
        position_tickers,
        open_count,
    )

    if rejected_tickers:
        logger.info(f"[signal_pipeline] Gate rejected: {rejected_tickers}")
    logger.info(f"[signal_pipeline] Gate approved: {approved_tickers}")

    approved_opts = [o for o in green_opts if o["ticker"] in approved_tickers]
    if not approved_opts:
        logger.info("[signal_pipeline] All signals gated -- no Alpaca orders submitted")
        return []

    return execute_alpaca_orders(approved_opts, run_date)
