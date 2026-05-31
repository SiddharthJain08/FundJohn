"""src/execution/open_reconcile.py — SP-6 Phase A: open-position reconciliation helpers.

Two module-level helpers for Task 8's run_reconcile():

  drop_signal_close(cur, signal_id, ticker, closed_price, reason='signal_dropped')
  flatten_signal_close(cur, signal_id, ticker, closed_price)

Each helper closes a dropped/flattened signal by:
  1. Upserting a signal_pnl row (status='closed') mirroring engine.update_pnl's upsert.
  2. Updating execution_signals (status='closed', lifecycle_state='CLOSED_AT_OPEN',
     filled_at=NOW(), fill_price=closed_price) to prevent phantom-row re-marking by
     subsequent engine.update_pnl() runs.

Phantom-row fix (SP-6 Task 9):
  Today a dropped/orphan-closed position keeps accruing P&L in update_pnl because
  execution_signals.status remains 'open'. Setting lifecycle_state='CLOSED_AT_OPEN'
  + status='closed' causes engine.update_pnl (post-Task-6) to skip the row via its
  WHERE ... AND (lifecycle_state IS NULL OR lifecycle_state='FILLED') filter, and
  additionally by the explicit CLOSED_AT_OPEN guard at engine.py:1128.

run_reconcile() (SP-6 Task 8b — Option B time-split, §12 of the design doc):
  The 9:30 ET open-window reconcile. A pure CLASSIFY, NO sizing. It diffs the
  carried APPROVED set (sign-only) against the broker book via Task 4's
  `_classify_position_deltas`, then acts ONLY on closes:
    • orphan_close — held, not in APPROVED set (a dropped signal) → close at open
    • flip_close   — APPROVED opposite-sign to broker → close the OLD leg at open
  It IGNORES `delta` (incl. resize-down delta<0) and `flip_open` — those are the
  3:55 production sizer's job (the confirmed asymmetry: full drops/flips close at
  the open; partial reduces + new opens go through the 3:55 sizer into close[T+1]).

  FLATTEN safety (the critical guard): when APPROVED is EMPTY the classifier emits
  the ENTIRE book as orphan_close (byte-identical in *kind* to a partial drop), so
  drop-vs-flatten CANNOT be decided from emission kind. We therefore decide
  `is_flatten = (not target) and bool(broker)` BEFORE the close loop, and a flatten
  is honored ONLY if BOTH `_eod_health_green(today)` AND a gate-ran sentinel are
  true. If either is false we DO NOT enter the close loop at all (zero broker
  submissions) — preserving positions + a loud alert. This is the precise line
  between "strategies legitimately went flat" and the 2026-05-22 empty-signals
  blowout. The dangerous case is gate-not-ran: a crashed premarket gate leaves zero
  APPROVED rows AND no sentinel; without the sentinel check that would look like
  "everything dropped" and flatten the book. Per Task 8b we SKIP + WARN (we do NOT
  promote COMPUTED→APPROVED here; that fail-open promote, if adopted, lives in the
  gate/a later task).
"""
from __future__ import annotations

import logging
import math
import os
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)

# Mirror the module-level constant from engine.py.
WORKSPACE = os.environ.get('WORKSPACE_ID', 'default')


def drop_signal_close(
    cur,
    signal_id: str,
    ticker: str,
    closed_price: float,
    reason: str = 'signal_dropped',
) -> None:
    """Close a signal that was dropped (e.g. position flattened at the open).

    Mirrors engine.py:1196–1225 signal_pnl UPSERT + execution_signals UPDATE.
    Computes realized_pnl_pct from mark_entry_price (or fallback entry_price)
    and direction.  days_held follows the same logic as update_pnl: prefer
    target_date when present, else signal_date.

    Args:
        cur:          psycopg2 cursor (RealDictCursor recommended; bare cursor ok)
        signal_id:    UUID of the execution_signal to close
        ticker:       ticker symbol (context only; signal_id is the key)
        closed_price: market price at which the signal is being closed
        reason:       close_reason written to signal_pnl (default 'signal_dropped')

    Returns:
        None. Raises on DB error.

    Postconditions:
        - signal_pnl row upserted: status='closed', close_reason=reason,
          closed_price=closed_price, realized_pnl_pct computed off mark_entry_price
          (or entry_price fallback when mark is NULL/NaN).
        - execution_signals row updated: status='closed',
          lifecycle_state='CLOSED_AT_OPEN', filled_at=NOW(), fill_price=closed_price.
    """
    # ── 1. Fetch signal metadata ──────────────────────────────────────────
    cur.execute(
        """
        SELECT id, strategy_id, workspace_id, signal_date, direction,
               entry_price, mark_entry_price, target_date
        FROM execution_signals
        WHERE id = %s
        """,
        (signal_id,),
    )
    row = cur.fetchone()
    if row is None:
        logger.warning("drop_signal_close: signal_id %s not found", signal_id)
        return

    # Support both RealDictRow and plain tuple access.
    def _get(key, pos):
        try:
            return row[key]
        except (TypeError, KeyError):
            return row[pos]

    sig_id    = _get('id', 0)
    strat_id  = _get('strategy_id', 1)
    ws_id     = _get('workspace_id', 2)
    sig_date  = _get('signal_date', 3)
    direction = _get('direction', 4)
    _raw_entry = _get('entry_price', 5)
    _raw_mark  = _get('mark_entry_price', 6)
    target_dt  = _get('target_date', 7)

    # ── 2. Resolve effective entry price (mirrors engine.py:1133–1142) ──
    entry: float | None = None
    if _raw_mark is not None:
        try:
            _mark_f = float(_raw_mark)
            entry = _mark_f if math.isfinite(_mark_f) else None
        except (ValueError, TypeError):
            entry = None
    if entry is None and _raw_entry is not None:
        try:
            entry = float(_raw_entry)
            if not math.isfinite(entry):
                entry = None
        except (ValueError, TypeError):
            entry = None

    # ── 3. Compute realized_pnl_pct (guard entry <= 0 / None) ────────────
    direction_upper = (direction or '').upper()
    if entry and entry > 0:
        if direction_upper == 'LONG':
            realized_pct = (closed_price - entry) / entry
        elif direction_upper == 'SHORT':
            realized_pct = (entry - closed_price) / entry
        else:
            realized_pct = 0.0
        if not math.isfinite(realized_pct):
            realized_pct = 0.0
    else:
        realized_pct = 0.0
        logger.warning(
            "drop_signal_close %s: no valid entry price (entry=%s); realized_pnl_pct=0.0",
            sig_id, entry,
        )

    # ── 4. Compute days_held (mirrors engine.py:1154–1158) ───────────────
    run_date = date.today()
    if target_dt is not None and isinstance(target_dt, date):
        days_held = (run_date - target_dt).days
    elif sig_date is not None and isinstance(sig_date, date):
        days_held = (run_date - sig_date).days
    else:
        days_held = 0

    now_utc = datetime.now(timezone.utc)

    # ── 5. Upsert signal_pnl (mirrors engine.py:1196–1219) ───────────────
    cur.execute(
        """
        INSERT INTO signal_pnl
            (signal_id, strategy_id, workspace_id, pnl_date,
             close_price, unrealized_pnl_pct, days_held, status,
             closed_price, closed_at, close_reason, realized_pnl_pct)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (signal_id, pnl_date) DO UPDATE SET
            close_price        = EXCLUDED.close_price,
            unrealized_pnl_pct = EXCLUDED.unrealized_pnl_pct,
            days_held          = EXCLUDED.days_held,
            status             = EXCLUDED.status,
            closed_price       = EXCLUDED.closed_price,
            closed_at          = EXCLUDED.closed_at,
            close_reason       = EXCLUDED.close_reason,
            realized_pnl_pct   = EXCLUDED.realized_pnl_pct
        """,
        (
            sig_id, strat_id, ws_id, run_date,
            closed_price, round(realized_pct, 6), days_held,
            'closed',
            closed_price,
            run_date,          # closed_at is DATE in signal_pnl schema
            reason,
            round(realized_pct, 6),
        ),
    )
    logger.info(
        "drop_signal_close: upserted signal_pnl %s reason=%s realized_pct=%.6f",
        sig_id, reason, realized_pct,
    )

    # ── 6. Update execution_signals → prevent phantom-row re-marking ─────
    cur.execute(
        """
        UPDATE execution_signals
        SET
            status          = 'closed',
            lifecycle_state = 'CLOSED_AT_OPEN',
            filled_at       = %s,
            fill_price      = %s
        WHERE id = %s
        """,
        (now_utc, closed_price, sig_id),
    )
    logger.info(
        "drop_signal_close: updated execution_signals %s → CLOSED_AT_OPEN fill_price=%s",
        sig_id, closed_price,
    )


def flatten_signal_close(
    cur,
    signal_id: str,
    ticker: str,
    closed_price: float,
) -> None:
    """Close a signal that is being flattened (position reduced to zero).

    Identical to drop_signal_close but with close_reason='flattened'.

    Args:
        cur:          psycopg2 cursor
        signal_id:    UUID of the execution_signal to close
        ticker:       ticker symbol (context only)
        closed_price: market price at which the position is being flattened

    Returns:
        None. Raises on DB error.
    """
    drop_signal_close(
        cur=cur,
        signal_id=signal_id,
        ticker=ticker,
        closed_price=closed_price,
        reason='flattened',
    )


# ════════════════════════════════════════════════════════════════════════════
# SP-6 Task 8b — run_reconcile (9:30 ET open-window drops/flatten close-at-open)
# ════════════════════════════════════════════════════════════════════════════

# Gate: the whole EOD reconcile flow is OFF unless this is '1'.
ENV_RECONCILE_GATE = 'OPENCLAW_EOD_RECONCILE'


def _reconcile_enabled() -> bool:
    """True iff OPENCLAW_EOD_RECONCILE == '1' (read at call-time so monkeypatch
    works in tests; mirrors premarket_gate.is_enabled)."""
    return os.environ.get(ENV_RECONCILE_GATE) == '1'


def _normalize_broker_symbol(t: str) -> str:
    """Map a broker symbol to the engine BASE-USD (dash) convention.

    Replicates execution.crypto_redeploy_sizer._normalize_broker_symbol: Alpaca
    returns crypto as 'BTC/USD' (slash) while signals/engine use 'BTC-USD' (dash).
    '/' never appears in an equity ticker, so this can never mis-map an equity
    position. Replicated (not imported) to keep this safety path import-light;
    behaviour is identical and pinned by a parity assertion in the test suite."""
    return (t or '').strip().upper().replace('/', '-')


def _load_approved_set(cur, today: date) -> dict[str, float]:
    """Sign-only carried-APPROVED targets: {ticker: +1.0 if LONG else -1.0}.

    9:30 is classify-only — NO NAV, NO sizing. The magnitude (+1/-1) only carries
    DIRECTION so `_classify_position_deltas` can detect opposite-sign flips; it is
    NEVER used as a notional. Keyed on the normalized broker symbol convention so
    it matches the broker book keys.

    Source: execution_signals WHERE lifecycle_state='APPROVED' AND target_date=today.
    Last write per ticker wins (a per-ticker broker position is netted across
    strategies; the sign-only target only needs one direction per ticker — if two
    APPROVED rows disagree on direction for the same ticker, the classifier would
    see a single sign, which is acceptable for a sign-only classify and is an
    edge the 3:55 sizer resolves with real weights)."""
    cur.execute(
        """
        SELECT ticker, direction
        FROM execution_signals
        WHERE lifecycle_state = 'APPROVED'
          AND target_date = %s
          AND ticker IS NOT NULL
        ORDER BY signal_date ASC, computed_at ASC NULLS FIRST
        """,
        (today,),
    )
    out: dict[str, float] = {}
    for row in cur.fetchall():
        tkr = _norm_row_get(row, 'ticker', 0)
        direction = _norm_row_get(row, 'direction', 1)
        if not tkr:
            continue
        key = _normalize_broker_symbol(tkr)
        out[key] = -1.0 if (str(direction or '').upper() == 'SHORT') else 1.0
    return out


def _eod_health_green(cur, today: date) -> bool:
    """True iff the latest eod_compute_health row for `today` has healthy=True.

    MISSING ROW ⇒ False (treat as unhealthy — fail-closed, never flatten on a day
    the 4 PM compute never wrote a health sentinel)."""
    cur.execute(
        """
        SELECT healthy
        FROM eod_compute_health
        WHERE run_date = %s
        ORDER BY run_at DESC NULLS LAST, id DESC
        LIMIT 1
        """,
        (today,),
    )
    row = cur.fetchone()
    if row is None:
        return False
    healthy = _norm_row_get(row, 'healthy', 0)
    return bool(healthy)


def _gate_ran_today(cur, today: date) -> bool:
    """True iff the premarket gate wrote its __gate_ran__ sentinel for `today`.

    The sentinel (Task 7, premarket_gate._write_gate_ran_sentinel) is committed in
    the SAME transaction as the COMPUTED→APPROVED promotions, so 'sentinel present'
    implies 'the gate ran to completion and any legit APPROVED rows are present'.
    Its ABSENCE on a zero-APPROVED day is the dangerous case (crashed gate) that
    MUST block a flatten."""
    cur.execute(
        """
        SELECT 1
        FROM signal_gate_verdicts
        WHERE gate_type = '__gate_ran__'
          AND target_date = %s
        LIMIT 1
        """,
        (today,),
    )
    return cur.fetchone() is not None


def _norm_row_get(row, key, pos):
    """Support both RealDictRow/dict and plain-tuple cursor rows."""
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        try:
            return row[pos]
        except (TypeError, KeyError, IndexError):
            return None


def _resolve_workspace_id(cur):
    """Resolve the active workspace UUID, mirroring engine.WORKSPACE semantics.

    engine.update_pnl binds `workspace_id = %s` with the module-level WORKSPACE,
    which is the resolved UUID at runtime (env WORKSPACE_ID or the default ws).
    We resolve it here from the workspaces table so the held-row query can carry
    the SAME workspace filter (so a multi-workspace book never over-closes another
    workspace's same-ticker signal). Returns the UUID string, or None when it
    can't be resolved — in which case the caller fails OPEN to no workspace filter
    (correct + inert for a single-workspace system)."""
    name_or_id = WORKSPACE
    try:
        # If WORKSPACE is already a UUID, this name lookup misses → fall through.
        cur.execute("SELECT id FROM workspaces WHERE name = %s LIMIT 1", (name_or_id,))
        row = cur.fetchone()
        if row is not None:
            return str(_norm_row_get(row, 'id', 0))
        # Maybe WORKSPACE is the UUID itself.
        cur.execute("SELECT id FROM workspaces WHERE id::text = %s LIMIT 1", (name_or_id,))
        row = cur.fetchone()
        if row is not None:
            return str(_norm_row_get(row, 'id', 0))
    except Exception as exc:  # noqa: BLE001
        logger.warning('_resolve_workspace_id: lookup failed (%s) — no ws filter', exc)
    return None


def _held_signal_rows(cur, ticker: str) -> list[tuple[str, str]]:
    """Return [(signal_id, direction)] for every currently-HELD ledger row of a
    ticker — i.e. status='open' AND (lifecycle_state IS NULL OR ='FILLED'), scoped
    to the active workspace.

    This mirrors engine.update_pnl's held-row filter (engine.py:1108-1109,
    including its `workspace_id = %s` scope). A broker position is netted
    per-ticker, but the strategy ledger has one row per signal, so an orphan/flip
    close of one broker position must close ALL of that ticker's held ledger rows.
    Match is done on the normalized symbol so a crypto 'BTC/USD' broker key
    resolves to its 'BTC-USD' ledger rows (equity tickers are identical under
    normalization)."""
    norm = _normalize_broker_symbol(ticker)
    ws_id = _resolve_workspace_id(cur)
    if ws_id is not None:
        cur.execute(
            """
            SELECT id, direction, ticker
            FROM execution_signals
            WHERE workspace_id = %s
              AND status = 'open'
              AND (lifecycle_state IS NULL OR lifecycle_state = 'FILLED')
              AND ticker IS NOT NULL
            """,
            (ws_id,),
        )
    else:
        cur.execute(
            """
            SELECT id, direction, ticker
            FROM execution_signals
            WHERE status = 'open'
              AND (lifecycle_state IS NULL OR lifecycle_state = 'FILLED')
              AND ticker IS NOT NULL
            """,
        )
    out: list[tuple[str, str]] = []
    for row in cur.fetchall():
        sig_id = _norm_row_get(row, 'id', 0)
        direction = _norm_row_get(row, 'direction', 1)
        row_tkr = _norm_row_get(row, 'ticker', 2)
        if _normalize_broker_symbol(row_tkr) == norm:
            out.append((str(sig_id), str(direction or '')))
    return out


def _resolve_fill_price(result: dict):
    """Extract a usable close fill price from an ae.execute_single result dict.

    The close-only path returns the fill/limit price under 'entry' (see
    alpaca_executor close_only branches). We DO NOT treat ack as fill blindly:
    the ledger-close is gated by status ∈ {submitted, recovered} AND a finite,
    positive price. (Phase A naive contract; Task 10 inserts the real
    poll-to-terminal OPG dual-path that confirms a {filled} status before the
    ledger write — the 'never ack=fill' invariant lands there.)

    Returns float price or None when no usable fill price is available."""
    if not isinstance(result, dict):
        return None
    if result.get('status') not in ('submitted', 'recovered'):
        return None
    for key in ('fill_price', 'entry'):
        val = result.get(key)
        if val is None:
            continue
        try:
            f = float(val)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f) and f > 0:
            return f
    return None


def run_reconcile(
    dry_run: bool = False,
    conn=None,
    broker_loader=None,
    gate_ran=None,
    run_date: date | None = None,
) -> dict:
    """9:30 ET open-window reconcile — drops/flatten ONLY (Option B time-split).

    Diffs the carried APPROVED set (sign-only) vs the broker book and closes
    AT THE OPEN: orphan_close (dropped signals) + flip_close (old leg of a flip).
    IGNORES delta + flip_open (the 3:55 production sizer's job). Zero-APPROVED +
    non-empty book ⇒ FLATTEN, guarded by health-green AND gate-ran.

    Args:
        dry_run:       log planned closes/flatten; submit nothing; no ledger writes.
        conn:          psycopg2 connection. If supplied, this function does NOT
                       commit (caller manages the txn; test rollback works). If
                       None (production), a new conn is opened, committed, closed.
        broker_loader: callable() -> {ticker: signed_usd}; default
                       regime_blended_sizer._load_broker_positions_usd.
        gate_ran:      override the gate-ran sentinel check (bool). Default:
                       _gate_ran_today(cur, today).
        run_date:      override the trading day. Default datetime.now(utc).date()
                       (same basis as premarket_gate / the signals register).

    Returns:
        {'drops': int, 'flattens': int, 'holds': int, 'errors': int}
    """
    counts = {'drops': 0, 'flattens': 0, 'holds': 0, 'errors': 0}

    # 1. Gate off → no-op early return.
    if not _reconcile_enabled():
        logger.info('%s != 1; reconcile disabled — no-op', ENV_RECONCILE_GATE)
        return counts

    own_conn = conn is None
    if own_conn:
        import psycopg2
        import psycopg2.extras
        uri = os.environ.get('POSTGRES_URI')
        if not uri:
            raise RuntimeError('POSTGRES_URI env var not set')
        conn = psycopg2.connect(uri, cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cur = conn.cursor()
        today = run_date or datetime.now(timezone.utc).date()

        # 2. Sign-only APPROVED targets (NO NAV / sizing).
        target = _load_approved_set(cur, today)

        # 3. Broker book (re-fetched immediately before any close), keys normalized.
        from execution.regime_blended_sizer import _classify_position_deltas
        if broker_loader is None:
            from execution.regime_blended_sizer import _load_broker_positions_usd
            broker_loader = _load_broker_positions_usd
        raw_broker = broker_loader() or {}
        broker: dict[str, float] = {}
        for tkr, usd in raw_broker.items():
            try:
                f = float(usd)
            except (TypeError, ValueError):
                continue
            if f == 0.0:
                continue
            broker[_normalize_broker_symbol(tkr)] = f

        logger.info(
            'run_reconcile: today=%s approved=%d broker=%d dry_run=%s',
            today, len(target), len(broker), dry_run,
        )

        # 4. Classify (Task 4). Sign-only targets — magnitude carries direction only.
        emissions = _classify_position_deltas(target, broker, ticker_meta={})

        # ── FLATTEN DECISION (must precede the close loop) ──────────────────
        # With an EMPTY target, _classify_position_deltas emits the ENTIRE book
        # as orphan_close — indistinguishable in *kind* from a partial drop. So
        # drop-vs-flatten is decided HERE from the target/broker shape, NOT from
        # emission kind, and the whole flatten case is gated before we submit a
        # single order.
        is_flatten = (not target) and bool(broker)

        if is_flatten:
            health_ok = _eod_health_green(cur, today)
            gate_ok = gate_ran if gate_ran is not None else _gate_ran_today(cur, today)
            if not (health_ok and gate_ok):
                # CRITICAL SAFETY: do NOT enter the close loop. Preserve every
                # position + loud alert. gate-not-ran is the dangerous case
                # (crashed gate ⇒ zero APPROVED ⇒ would look like total drop).
                logger.error(
                    'run_reconcile: REFUSING FLATTEN of %d positions — '
                    'health_green=%s gate_ran=%s (today=%s). Preserving book. '
                    'Zero-APPROVED with an unhealthy compute or a gate that did '
                    'not run is treated as a pipeline failure, NOT a legitimate '
                    'flat day. NOT promoting COMPUTED→APPROVED here (Task 8b).',
                    len(broker), health_ok, gate_ok, today,
                )
                counts['holds'] = len(broker)
                _maybe_commit(conn, own_conn)
                return counts
            logger.warning(
                'run_reconcile: FLATTEN authorized — zero APPROVED + healthy + '
                'gate-ran; closing all %d broker positions at the open (today=%s)',
                len(broker), today,
            )

        # 5. Act ONLY on closes: orphan_close + flip_close. IGNORE delta + flip_open.
        closes = [(t, u, k) for (t, u, k) in emissions
                  if k in ('orphan_close', 'flip_close')]
        ignored = len(emissions) - len(closes)
        if ignored:
            logger.info(
                'run_reconcile: ignoring %d delta/flip_open emission(s) — 3:55 '
                'sizer territory (resize-downs + opens fill into close[T+1])',
                ignored,
            )

        if not closes:
            # Nothing to close at the open. Holds = positions left as-is.
            counts['holds'] = len(broker)
            logger.info('run_reconcile: no open-window closes; %d holds', counts['holds'])
            _maybe_commit(conn, own_conn)
            return counts

        # 6. Submit each close at the open + ledger-close on fill.
        ae = None
        for tkr, signed_usd, kind in closes:
            current_usd = broker.get(tkr, signed_usd if signed_usd else 0.0)
            held_rows = _held_signal_rows(cur, tkr)
            # close_reason mirrors what the helper writes: flatten→'flattened';
            # both orphan_close and flip_close use drop_signal_close→'signal_dropped'
            # (a flip's OLD leg is a live-only drop in the strategy ledger).
            reason = 'flattened' if is_flatten else 'signal_dropped'
            close_helper = flatten_signal_close if is_flatten else drop_signal_close
            strategy_id = '__flip_close__' if (kind == 'flip_close' and not is_flatten) \
                else '__close_orphan__'
            # Side to flatten: a LONG (current_usd>0) is closed with a SELL.
            side = 'sell' if current_usd > 0 else 'buy'

            if dry_run:
                logger.info(
                    'run_reconcile DRY: would %s %s (%s) current≈$%.0f side=%s '
                    'ledger_rows=%d reason=%s',
                    'FLATTEN' if is_flatten else 'CLOSE', tkr, kind,
                    current_usd, side, len(held_rows), reason,
                )
                # Count the intended action without side effects.
                if is_flatten:
                    counts['flattens'] += 1
                else:
                    counts['drops'] += 1
                continue

            if ae is None:
                from execution import alpaca_executor as ae  # noqa: F811

            order = {
                'ticker': tkr,
                'side': side,
                'direction': 'short' if side == 'sell' else 'long',
                'close_only': True,
                'is_dropped': True,
                'current_usd': current_usd,
                'notional_usd': abs(current_usd),
                'strategy_id': strategy_id,
            }
            try:
                # equity is unused on the close_only path; pass the broker gross
                # as a harmless positive scalar. run_date as YYYY-MM-DD string to
                # match the executor's coid builder (it calls .replace('-','')).
                result = ae.execute_single(
                    None, abs(current_usd) or 1.0, order, today.isoformat()
                )
            except Exception as exc:  # noqa: BLE001
                logger.error('run_reconcile: execute_single raised for %s: %s', tkr, exc)
                counts['errors'] += 1
                continue

            fill_price = _resolve_fill_price(result)
            if fill_price is None:
                logger.error(
                    'run_reconcile: %s close NOT confirmed (status=%s) — no ledger '
                    'write, counting as error (Task 10 OPG poll-to-terminal will '
                    'retry/sweep unfilled)',
                    tkr, (result or {}).get('status') if isinstance(result, dict) else result,
                )
                counts['errors'] += 1
                continue

            # Confirmed close → ledger-close every held row for this ticker.
            if held_rows:
                for sig_id, _dir in held_rows:
                    try:
                        close_helper(cur, sig_id, tkr, fill_price)
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            'run_reconcile: ledger-close failed for %s sig=%s: %s',
                            tkr, sig_id, exc,
                        )
                        counts['errors'] += 1
            else:
                # No matching ledger row (e.g. a flatten of a position with no
                # signal). Still closed the broker position; ledger-close skipped.
                logger.warning(
                    'run_reconcile: %s closed at open but no held ledger row — '
                    'broker position closed, ledger-close skipped (signal_id None)',
                    tkr,
                )

            if is_flatten:
                counts['flattens'] += 1
            else:
                counts['drops'] += 1

        # Holds = broker positions we did NOT close at the open.
        counts['holds'] = max(0, len(broker) - counts['drops'] - counts['flattens'])

        if not dry_run:
            _maybe_commit(conn, own_conn)
        return counts
    finally:
        if own_conn and conn is not None:
            conn.close()


def _maybe_commit(conn, own_conn: bool) -> None:
    """Commit iff we own the connection (production). When the caller supplied the
    conn (tests), leave the txn open so rollback isolation works — mirrors
    premarket_gate.run_gate's caller contract."""
    if own_conn and conn is not None:
        conn.commit()


def _sweep_unfilled(dry_run: bool = False) -> dict:
    """STUB (Task 8b): the 9:31 day-sweep that re-polls OPG-unfilled drop orders
    and re-closes them with tif=day. The real poll-to-terminal + cancel/resubmit
    is built in Task 10 (OPG dual-path). For now this logs its intent only and
    performs no broker action — clearly marked so it is not mistaken for a working
    sweep. Returns a no-op count dict."""
    logger.warning(
        '--sweep is a STUB in Task 8b — the 9:31 OPG day-sweep (re-poll unfilled '
        'tif=opg drop orders → cancel + resubmit tif=day) is implemented in Task 10 '
        '(OPG dual-path). No broker action taken. dry_run=%s', dry_run,
    )
    return {'swept': 0, 'resubmitted': 0, 'errors': 0}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description='SP-6 Task 8b: 9:30 ET open-window reconcile (drops/flatten).')
    parser.add_argument('--dry-run', action='store_true',
                        help='Log planned drops/flatten; submit nothing; no ledger writes.')
    parser.add_argument('--sweep', action='store_true',
                        help='9:31 day-sweep of OPG-unfilled drop orders (STUB — see Task 10).')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )

    if args.sweep:
        res = _sweep_unfilled(dry_run=args.dry_run)
        logger.info('run_reconcile --sweep result: %s', res)
        return

    res = run_reconcile(dry_run=args.dry_run)
    logger.info('run_reconcile result: %s', res)


if __name__ == '__main__':
    main()
