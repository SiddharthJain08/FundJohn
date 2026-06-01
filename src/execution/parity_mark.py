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
    cur.execute("""
        SELECT id, ticker, direction, entry_price, stop_loss, target_1
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
        else:
            row_id, ticker, direction_raw, entry_price_raw, stop_raw, target_raw = row

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

    logger.info(
        "[parity_mark] %d signal(s) marked FILLED (broker-held) for target_date=%s",
        updated, run_date,
    )
    return updated


def finalize_execution_ledger(cur, closes: dict, run_date,
                              workspace_id: str = 'default') -> int:
    """Materialize the per-ORDER execution ledger on alpaca_submissions.

    For each filled ENTRY order on run_date, record the official close[T+1]
    benchmark (official_close) and
        exec_ledger_usd = (official_close - filled_avg_price)
                          x (direction_sign x filled_qty)
    where direction_sign = +1 for 'long', -1 for 'short'.

    exec_ledger_usd > 0 ⟺ the fill BEAT the close benchmark (long filled below the
    close / short filled above it) — the §28 beat-close objective.

    Order grain: alpaca_submissions is one row per consolidated broker order, so the
    ledger is intrinsically per-order (NOT per signal/strategy). Sentinel close/orphan
    orders (strategy_id starting with '__') are excluded (entry-only scope). Orders
    with no reconciled fill yet (filled_avg_price NULL) are left NULL and backfilled by
    a later idempotent re-run.

    Args:
        cur:          psycopg2 cursor (caller owns the transaction).
        closes:       dict[ticker -> float] official close per ticker (the same dict
                      finalize_parity_marks receives).
        run_date:     date — matched against alpaca_submissions.run_date.
        workspace_id: unused (alpaca_submissions has no workspace_id); accepted so the
                      call signature mirrors finalize_parity_marks.

    Returns:
        Number of alpaca_submissions rows updated.
    """
    if not closes:
        return 0

    # Normalize the closes keys once so BTC-USD (closes) matches BTC/USD (submission).
    closes_norm = {}
    for _k, _v in closes.items():
        _fv = _safe_float(_v, 'close')
        if _fv is not None:
            closes_norm[_norm_ticker(_k)] = _fv

    cur.execute("""
        SELECT id, strategy_id, ticker, direction,
               filled_avg_price, filled_qty, broker_status
          FROM alpaca_submissions
         WHERE run_date = %s
         ORDER BY id
    """, (run_date,))
    rows = cur.fetchall()
    if not rows:
        return 0

    updated = 0
    for row in rows:
        if hasattr(row, 'keys'):
            row_id = row['id']
            strategy_id = row['strategy_id']
            ticker = row['ticker']
            direction_raw = row['direction']
            avg_raw = row['filled_avg_price']
            qty_raw = row['filled_qty']
            status = row['broker_status']
        else:
            (row_id, strategy_id, ticker, direction_raw,
             avg_raw, qty_raw, status) = row

        # Entry-only scope: skip sentinel close/orphan orders.
        if strategy_id and str(strategy_id).startswith('__'):
            continue
        # Skip explicit non-fill broker states (error / rejected / ...).
        if status is not None and status not in ('filled', 'partial'):
            continue

        avg = _safe_float(avg_raw, 'filled_avg_price')
        qty = _safe_float(qty_raw, 'filled_qty')
        if avg is None or qty is None:
            continue  # no reconciled fill yet → leave NULL (deferrable)

        close = closes_norm.get(_norm_ticker(ticker))
        if close is None:
            continue

        d = str(direction_raw or '').lower()
        if d not in ('long', 'short'):
            continue
        direction_sign = 1 if d == 'long' else -1

        exec_ledger_usd = (close - avg) * (direction_sign * qty)

        cur.execute("""
            UPDATE alpaca_submissions
               SET official_close  = %s,
                   exec_ledger_usd = %s
             WHERE id = %s
        """, (close, exec_ledger_usd, row_id))
        updated += max(cur.rowcount, 0)

    logger.info(
        "[exec_ledger] %d order(s) ledgered for run_date=%s", updated, run_date,
    )
    return updated
