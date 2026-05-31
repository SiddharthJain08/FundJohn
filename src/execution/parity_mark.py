#!/usr/bin/env python3
"""parity_mark.py — mark entry at close[T+1], re-anchor brackets for parity.

When a signal reaches its target_date (the T+1 trading session), this module
marks the official close price as the entry, re-anchors the stop/target bracket
using the same _reanchor_bracket logic as the t+1 backtest (_per_bar_simulate),
and transitions the row to FILLED.

This is the "parity" guarantee: the execution ledger must mirror the backtest's
bracket geometry so that signal_pnl's daily-close exit walk reproduces backtest
results.  The mark_entry_price is the OFFICIAL close (not the broker fill).

API:
    finalize_parity_marks(cur, closes: dict[str, float], run_date) -> int
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def finalize_parity_marks(cur, closes: dict, run_date) -> int:
    """Mark execution_signals rows at close[T+1] and re-anchor brackets.

    Selects all EXECUTING/FILLED rows where target_date == run_date AND
    the ticker is present in the closes dict.  For each:
      1. Computes direction_sign (+1 LONG, -1 SHORT) via _signal_to_long_short;
         skips neutral/unknown directions (returns 0).
      2. Guards against None/NaN in entry_price, stop_loss, target_1 — rows
         with bad numeric fields are skipped and logged.
      3. Calls _reanchor_bracket(ref=entry_price, entry_price=close,
         direction=direction_sign, stop_ref=stop_loss, target_ref=target_1)
         to shift the bracket to the new fill level (percentage-preserving).
      4. UPDATEs the row: mark_entry_price=close, fill_price=close,
         stop_loss/target_1=re-anchored values, filled_at=NOW(),
         lifecycle_state='FILLED'.

    Args:
        cur:      psycopg2 cursor (caller owns transaction + commit).
        closes:   dict[ticker -> float] — latest official close per ticker.
        run_date: date — matched against execution_signals.target_date.

    Returns:
        Number of rows updated.
    """
    from backtest.unified_backtest import _reanchor_bracket, _signal_to_long_short

    if not closes:
        return 0

    # Fetch EXECUTING/FILLED signals whose target_date falls on run_date.
    # We filter by closes dict in Python — allows a simple equality predicate
    # without a Postgres IN clause that varies in length.
    cur.execute("""
        SELECT id, ticker, direction, entry_price, stop_loss, target_1
          FROM execution_signals
         WHERE lifecycle_state IN ('EXECUTING', 'FILLED')
           AND target_date = %s
         ORDER BY id
    """, (run_date,))

    rows = cur.fetchall()
    if not rows:
        logger.debug("[parity_mark] No EXECUTING/FILLED rows for target_date=%s", run_date)
        return 0

    now_ts = datetime.now(timezone.utc)
    updated = 0

    for row in rows:
        # Support both dict-cursor and tuple-cursor access
        if hasattr(row, 'keys'):
            row_id, ticker = row['id'], row['ticker']
            direction_raw = row['direction']
            entry_price_raw = row['entry_price']
            stop_raw = row['stop_loss']
            target_raw = row['target_1']
        else:
            row_id, ticker, direction_raw, entry_price_raw, stop_raw, target_raw = row

        # Skip if ticker not in today's closes
        if ticker not in closes:
            continue

        # Map direction to ±1; skip neutral/unsupported directions
        direction_sign = _signal_to_long_short(direction_raw)
        if direction_sign == 0:
            logger.warning(
                "[parity_mark] Skipping signal %s (%s): unsupported direction '%s'",
                row_id, ticker, direction_raw,
            )
            continue

        # Guard against None/NaN in core price fields
        import math as _math

        def _safe_float(v, field: str):
            """Return float(v) or None if v is None/NaN/Inf."""
            if v is None:
                return None
            try:
                f = float(v)
                return f if _math.isfinite(f) else None
            except (ValueError, TypeError):
                return None

        entry_price = _safe_float(entry_price_raw, 'entry_price')
        stop_loss = _safe_float(stop_raw, 'stop_loss')
        target_1 = _safe_float(target_raw, 'target_1')

        if any(v is None for v in (entry_price, stop_loss, target_1)):
            logger.warning(
                "[parity_mark] Skipping signal %s (%s): None/NaN in "
                "entry_price=%s stop_loss=%s target_1=%s",
                row_id, ticker, entry_price_raw, stop_raw, target_raw,
            )
            continue

        mark_price = float(closes[ticker])

        # Re-anchor bracket: preserves percentage distances from entry_price
        # (the original reference) at the new mark level (official close).
        # _reanchor_bracket keyword-only: ref=original entry, entry_price=new mark.
        new_stop, new_target = _reanchor_bracket(
            ref=entry_price,
            entry_price=mark_price,
            direction=direction_sign,
            stop_ref=stop_loss,
            target_ref=target_1,
        )

        cur.execute("""
            UPDATE execution_signals
               SET mark_entry_price  = %s,
                   fill_price        = %s,
                   stop_loss         = %s,
                   target_1          = %s,
                   filled_at         = %s,
                   lifecycle_state   = 'FILLED'
             WHERE id = %s
        """, (mark_price, mark_price, new_stop, new_target, now_ts, row_id))

        n = cur.rowcount
        updated += max(n, 0)
        logger.info(
            "[parity_mark] Signal %s %s %s: ref=%.4f mark=%.4f "
            "stop %.4f→%.4f target %.4f→%.4f",
            row_id, ticker, direction_raw,
            entry_price, mark_price,
            stop_loss, new_stop, target_1, new_target,
        )

    logger.info("[parity_mark] %d signal(s) marked for target_date=%s", updated, run_date)
    return updated
