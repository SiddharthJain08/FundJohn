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
    finalize_parity_marks(cur, closes: dict[str, float], run_date,
                          workspace_id: str = 'default',
                          broker_loader=None) -> int
"""
import logging
import math
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _safe_float(v, field: str):
    """Return float(v) or None if v is None/NaN/Inf."""
    if v is None:
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def _norm_ticker(s: str) -> str:
    """Normalize a broker symbol or DB ticker for comparison.

    Converts '/' to '-' (crypto: BTC/USD → BTC-USD) and upper-cases.
    Applied to BOTH broker symbols and signal tickers before comparison.
    """
    return str(s).replace('/', '-').upper()


def finalize_parity_marks(cur, closes: dict, run_date,
                          workspace_id: str = 'default',
                          broker_loader=None) -> int:
    """Mark execution_signals rows at close[T+1] and re-anchor brackets.

    Selects all APPROVED/EXECUTING/FILLED rows where target_date == run_date
    AND workspace_id matches AND the ticker is present in the closes dict.
    For each row a broker cross-check is performed: only signals whose ticker
    is HELD in the broker book are marked FILLED.  Signals not held are left
    in their current lifecycle_state (typically APPROVED for EOD opens that
    did not fill).

    For each row that passes all checks:
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
        cur:           psycopg2 cursor (caller owns transaction + commit).
        closes:        dict[ticker -> float] — latest official close per ticker.
        run_date:      date — matched against execution_signals.target_date.
        workspace_id:  str — scopes the SELECT to a single workspace
                       (default='default').
        broker_loader: optional callable() -> dict[str, float] returning
                       current broker positions as {symbol: signed_market_value}.
                       Pass a stub in tests to avoid live Alpaca calls.
                       Defaults to execution.regime_blended_sizer._load_broker_positions_usd.

    Returns:
        Number of rows updated (marked FILLED).
    """
    from backtest.unified_backtest import _reanchor_bracket, _signal_to_long_short

    if not closes:
        return 0

    # Fetch APPROVED/EXECUTING/FILLED signals whose target_date falls on
    # run_date and belong to the specified workspace.
    # APPROVED is widened to capture EOD opens that were submitted but whose
    # lifecycle_state was never advanced past APPROVED before the 4 PM mark.
    # We filter by closes dict in Python — allows a simple equality predicate
    # without a Postgres IN clause that varies in length.
    # strategy_id + target_date are fetched for the SP-6 Phase C D1
    # continuation-roll sibling close (see end of the marked-FILLED block).
    cur.execute("""
        SELECT id, ticker, direction, entry_price, stop_loss, target_1,
               strategy_id, target_date
          FROM execution_signals
         WHERE lifecycle_state IN ('APPROVED', 'EXECUTING', 'FILLED')
           AND target_date = %s
           AND workspace_id = %s
         ORDER BY id
    """, (run_date, workspace_id))

    rows = cur.fetchall()
    if not rows:
        logger.debug(
            "[parity_mark] No APPROVED/EXECUTING/FILLED rows for target_date=%s",
            run_date,
        )
        return 0

    # Load broker positions once after the no-rows guard to avoid a live
    # Alpaca call when there is nothing to process.
    if broker_loader is None:
        from execution.regime_blended_sizer import _load_broker_positions_usd
        broker_loader = _load_broker_positions_usd

    broker_raw = broker_loader() or {}
    # Build a normalized set of held tickers for O(1) lookup.
    # Both sides normalised: '/' → '-', upper-cased (handles BTC/USD vs BTC-USD).
    held = {_norm_ticker(sym) for sym in broker_raw}

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
            row_strategy_id = row['strategy_id']
            row_target_date = row['target_date']
        else:
            (row_id, ticker, direction_raw, entry_price_raw, stop_raw,
             target_raw, row_strategy_id, row_target_date) = row

        # Skip if ticker not in today's closes
        if ticker not in closes:
            continue

        # Broker cross-check: only mark signals that are ACTUALLY HELD.
        # An APPROVED signal whose order did not fill is NOT held; marking it
        # FILLED would create a phantom position in signal_pnl.
        if _norm_ticker(ticker) not in held:
            logger.debug(
                "[parity_mark] Signal %s (%s) NOT held in broker — leaving unmarked",
                row_id, ticker,
            )
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

        # ── SP-6 Phase C — D1 double-pnl mitigation ──────────────────────
        # When THIS row is a CONTINUATION (engine.write_signals minted it for a
        # later target_date because an older row was still tracking the live
        # position), the older spent sibling must be CLOSED at the same official
        # close mark so pnl is realized at the roll point — no gap, no double-
        # count.  We detect the continuation purely by the presence of an OLDER
        # sibling that is actually tracking a live position:
        #   lifecycle_state='FILLED', status='open',
        #   target_date < this row's target_date,
        #   same strategy_id + ticker + workspace_id, different id.
        # A first-time fill has no such sibling → nothing is closed.  Closing
        # uses the existing signal-close semantics (open_reconcile.drop_signal_close):
        # signal_pnl gets status='closed', close_reason='rolled_continuation',
        # closed_at + realized_pnl_pct off the sibling's mark; execution_signals
        # gets status='closed', lifecycle_state='CLOSED_AT_OPEN', fill_price=mark.
        # NEVER DELETE.  Self-gating: gate-off rows have lifecycle_state=NULL and
        # target_date=NULL, so this whole finalize loop is a no-op for them and
        # no continuation/sibling ever exists.
        if row_target_date is not None and row_strategy_id is not None:
            cur.execute("""
                SELECT id FROM execution_signals
                 WHERE strategy_id    = %s
                   AND ticker         = %s
                   AND workspace_id   = %s
                   AND status         = 'open'
                   AND lifecycle_state = 'FILLED'
                   AND target_date IS NOT NULL
                   AND target_date    < %s
                   AND id            <> %s
                 ORDER BY target_date ASC
            """, (row_strategy_id, ticker, workspace_id, row_target_date, row_id))
            sibling_rows = cur.fetchall()
            if sibling_rows:
                from execution.open_reconcile import drop_signal_close
                for srow in sibling_rows:
                    sib_id = srow['id'] if hasattr(srow, 'keys') else srow[0]
                    drop_signal_close(
                        cur, sib_id, ticker, mark_price,
                        reason='rolled_continuation',
                    )
                    logger.info(
                        "[parity_mark] D1: rolled spent sibling %s (%s) into "
                        "continuation %s at mark=%.4f",
                        sib_id, ticker, row_id, mark_price,
                    )

    logger.info(
        "[parity_mark] %d signal(s) marked FILLED (broker-held) for target_date=%s",
        updated, run_date,
    )
    return updated
