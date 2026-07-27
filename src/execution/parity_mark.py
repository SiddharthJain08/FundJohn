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
    # Hoisted out of the per-sibling loop (function-top local import — codebase
    # idiom).  Used by the SP-6 Phase C D1 continuation-roll sibling close below.
    from execution.open_reconcile import drop_signal_close

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
            # Savepoint-isolate the sibling-close work.  This is the FIRST
            # raise-capable code inside the engine's parity span
            # (engine.py:1576-1607), whose except handler only LOGS — it does
            # NOT roll back.  Without a savepoint, a single raise here (corrupt/
            # poisoned sibling row, or any DB error inside drop_signal_close)
            # would leave the connection in InFailedSqlTransaction, which then
            # aborts detect_confluence and discards the ENTIRE EOD cycle's writes
            # at the final conn.commit().  The savepoint is established AFTER the
            # FILLED UPDATE above (which is already committed-into-the-txn and
            # counted in `updated`), so a rollback degrades a failed roll to
            # "this roll skipped, loudly logged" WITHOUT unwinding the FILLED
            # mark.  Mirrors engine.write_signals' SAVEPOINT sp_signal pattern.
            # The savepoint spans BOTH the sibling SELECT and the
            # drop_signal_close loop — any of them can raise on a poisoned row,
            # and one savepoint over the whole loop means a failure on any
            # sibling rolls back the whole roll for THIS filled row (atomic skip).
            cur.execute("SAVEPOINT sp_roll")
            try:
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
                # Multi-sibling case: when several older spent siblings exist
                # (e.g. a roll missed a session), the loop closes ALL of them at
                # the CURRENT mark — pnl is realized at roll-time for each.  This
                # is acceptable under the one-net-position-per-(strategy,ticker)
                # invariant: there is a single live position, so the older rows
                # are stale trackers that must all be retired at the same mark.
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
                cur.execute("RELEASE SAVEPOINT sp_roll")
            except Exception as _roll_err:
                cur.execute("ROLLBACK TO SAVEPOINT sp_roll")
                cur.execute("RELEASE SAVEPOINT sp_roll")
                logger.error(
                    "[parity_mark] D1 sibling-close FAILED for continuation %s "
                    "(%s) at mark=%.4f — roll SKIPPED, sibling(s) left open; "
                    "FILLED mark stands. err=%s",
                    row_id, ticker, mark_price, _roll_err,
                )

    logger.info(
        "[parity_mark] %d signal(s) marked FILLED (broker-held) for target_date=%s",
        updated, run_date,
    )
    return updated


def backfill_broker_fill_truth(cur, run_date, workspace_id: str = 'default',
                               lookback_days: int = 5) -> int:
    """Populate broker_fill_price + fill_slippage_bps (migration 145) on FILLED
    parity rows from alpaca_submissions.filled_avg_price (fix 7, 2026-07-27).

    The parity guarantee keeps mark_entry_price = official close; this records
    what execution ACTUALLY paid alongside it, so live cost/edge decay is
    measurable (fill_slippage_bps > 0 = paid worse than the mark). Set-based,
    idempotent (only NULL broker_fill_price rows), self-healing over a trailing
    window because reconcile can land filled_avg_price after the first parity
    pass. Savepoint-isolated: a failure here must never poison the engine's
    parity transaction. Returns rows updated (0 on any failure)."""
    cur.execute("SAVEPOINT sp_fill_truth")
    try:
        cur.execute("""
            UPDATE execution_signals es
               SET broker_fill_price = s.fav,
                   fill_slippage_bps = CASE
                       WHEN es.mark_entry_price IS NOT NULL AND es.mark_entry_price > 0
                       THEN (CASE WHEN UPPER(es.direction) IN ('LONG', 'BUY', 'BUY_VOL')
                                  THEN 1 ELSE -1 END)
                            * (s.fav - es.mark_entry_price) / es.mark_entry_price * 10000
                   END
              FROM (SELECT run_date, ticker, AVG(filled_avg_price) AS fav
                      FROM alpaca_submissions
                     WHERE run_date > %s::date - %s
                       AND filled_avg_price IS NOT NULL AND filled_avg_price > 0
                     GROUP BY run_date, ticker) s
             WHERE es.lifecycle_state IN ('FILLED', 'CLOSED_AT_OPEN')
               AND es.broker_fill_price IS NULL
               AND es.target_date = s.run_date
               AND es.ticker = s.ticker
               AND es.workspace_id = %s
        """, (run_date, int(lookback_days), workspace_id))
        n = cur.rowcount or 0
        cur.execute("RELEASE SAVEPOINT sp_fill_truth")
        if n:
            logger.info("[parity_mark] broker-fill truth backfilled on %d row(s)", n)
        return n
    except Exception as e:
        cur.execute("ROLLBACK TO SAVEPOINT sp_fill_truth")
        cur.execute("RELEASE SAVEPOINT sp_fill_truth")
        logger.error("[parity_mark] broker-fill backfill failed (%s) — skipped", e)
        return 0


def refresh_live_days(cur) -> int:
    """strategy_registry.live_days = count of distinct target_dates on which the
    strategy actually held a marked position (FILLED / CLOSED_AT_OPEN parity
    rows). Was 0 for every strategy since inception — no writer existed (fix 7,
    2026-07-27). Set-based + savepoint-isolated; returns registry rows changed."""
    cur.execute("SAVEPOINT sp_live_days")
    try:
        cur.execute("""
            UPDATE strategy_registry sr
               SET live_days = s.n
              FROM (SELECT strategy_id, COUNT(DISTINCT target_date) AS n
                      FROM execution_signals
                     WHERE lifecycle_state IN ('FILLED', 'CLOSED_AT_OPEN')
                       AND strategy_id IS NOT NULL
                     GROUP BY strategy_id) s
             WHERE sr.id = s.strategy_id
               AND COALESCE(sr.live_days, 0) <> s.n
        """)
        n = cur.rowcount or 0
        cur.execute("RELEASE SAVEPOINT sp_live_days")
        if n:
            logger.info("[parity_mark] live_days refreshed on %d strategies", n)
        return n
    except Exception as e:
        cur.execute("ROLLBACK TO SAVEPOINT sp_live_days")
        cur.execute("RELEASE SAVEPOINT sp_live_days")
        logger.error("[parity_mark] live_days refresh failed (%s) — skipped", e)
        return 0


def close_stale_trackers(cur, staleness_days: int = 14,
                         held_tickers=None, limit: int = 20000) -> int:
    """Retire abandoned signal trackers (fix 7, 2026-07-27): signals whose
    LATEST signal_pnl mark is 'open' but older than `staleness_days` stopped
    being marked (strategy deprecated / removed from the run set) and pollute
    every live-metrics rollup. Each is closed via drop_signal_close at its
    last-known mark with close_reason='stale_tracker' — UPDATE only, NEVER
    DELETE, canonical close semantics (signal_pnl final row + execution_signals
    retired).

    `held_tickers` is REQUIRED evidence: pass the current broker ticker set; a
    None (broker unreadable) SKIPS the whole pass — never retire a tracker we
    cannot prove is not the live book. Returns signals closed."""
    if held_tickers is None:
        logger.warning("[parity_mark] stale-tracker pass skipped: no broker evidence")
        return 0
    from execution.open_reconcile import drop_signal_close
    held = {_norm_ticker(t) for t in held_tickers}
    cur.execute("""
        SELECT sp.signal_id, es.ticker,
               COALESCE(sp.close_price, es.mark_entry_price, es.entry_price) AS px
          FROM (SELECT DISTINCT ON (signal_id) signal_id, pnl_date, status, close_price
                  FROM signal_pnl ORDER BY signal_id, pnl_date DESC) sp
          JOIN execution_signals es ON es.id = sp.signal_id
         WHERE sp.status = 'open'
           AND sp.pnl_date < CURRENT_DATE - %s
         LIMIT %s
    """, (int(staleness_days), int(limit)))
    stale = cur.fetchall()
    closed = skipped = 0
    for row in stale:
        sig_id, ticker, px = ((row['signal_id'], row['ticker'], row['px'])
                              if hasattr(row, 'keys') else row)
        if ticker and _norm_ticker(ticker) in held:
            skipped += 1
            continue
        pxf = _safe_float(px, 'close_price')
        if pxf is None or pxf <= 0:
            skipped += 1
            continue
        cur.execute("SAVEPOINT sp_stale")
        try:
            drop_signal_close(cur, sig_id, ticker, pxf, reason='stale_tracker')
            cur.execute("RELEASE SAVEPOINT sp_stale")
            closed += 1
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT sp_stale")
            cur.execute("RELEASE SAVEPOINT sp_stale")
            logger.error("[parity_mark] stale-tracker close failed for %s (%s): %s",
                         sig_id, ticker, e)
            skipped += 1
    if closed or skipped:
        logger.info("[parity_mark] stale trackers: %d closed, %d skipped "
                    "(held/unpriced/error)", closed, skipped)
    return closed


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
