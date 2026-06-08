#!/usr/bin/env python3
"""regime_liquidator.py — flatten OpenClaw positions on operator demand.

Hook: manual operator entry — invoked by scripts/run_forced_liquidation.sh
on operator demand. Auto-triggers were removed 2026-05-19 in Phase 1 of the
redeploy-not-liquidate plan (intraday/daily regime transitions now spawn a
delta-based pipeline redeploy via scripts/redeploy_pipeline.py instead of
blowing out the book).

When invoked this module:
  1. Refuses to run outside RTH — operator must rerun during regular
     trading hours. Pre-/post-market submission paths (OPG queueing) were
     removed 2026-05-19 because the 2026-05-18 incident showed OPG fills
     paper at unfillable prices and the audit `result_status` overloaded
     submission-ack with actual-fill.
  2. Cancels every open OpenClaw order (parent + bracket legs). The COID
     prefix 'AX' is the OpenClaw signature (see alpaca_executor.py:283).
  3. Submits a market close per OpenClaw symbol still showing a non-zero
     position at the broker via `alpaca position close`.
  4. Polls each submitted close to a terminal broker state (filled,
     expired, canceled, rejected, done_for_day, replaced, failed) with a
     90s budget. The audit row's `result_status` records the REAL outcome
     (filled / partial / pending / rejected / submit_error) — not the
     submission ack.
  5. Audits every action into the alpaca_liquidations table.
  6. Sets a Redis idempotency sentinel so a same-day re-run is a no-op.
  7. Posts a summary to #trade-reports with the outcome breakdown.

Default mode is DRY-RUN (logs the intended plan, no broker calls). Set
OPENCLAW_ALPACA_LIVE_LIQUIDATE=1 to enable. Pattern mirrors
src/execution/alpaca_replace_stop.py.

The 10 AM trading cycle is intentionally NOT blocked: after liquidation
the system is free to rotate into fresh signals sized for the new
regime within the same morning. (Per user direction.)

Trigger semantics (manual force path):
- Operator must pass `--force` (sets force_override=True). The natural
  regime-change branch remains in code for completeness but is no longer
  reachable from cron — see Phase 1 strip in cron-schedule.js +
  run_intraday_market_state.py.
- `force_transition_tag` exists for sentinel-key disambiguation but is
  expected to be unused now that intraday auto-fire is gone (Phase 1).
- A corrupt regime_latest.json (incomplete write) is tolerated under
  --force: state defaults to UNKNOWN, prior to MANUAL_FORCE.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
ALPACA_CLI = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')
REGIME_LATEST_FILE = ROOT / '.agents' / 'market-state' / 'regime_latest.json'
COID_PREFIX = 'AX'
SENTINEL_TTL = 86400  # 24h
COOLDOWN_TTL  = 3600  # 60 min — prevents back-to-back liquidations after intraday fire
COOLDOWN_KEY_TEMPLATE = 'liquidate:cooldown:{date}'


# ── Helpers ─────────────────────────────────────────────────────────────────

def _is_live() -> bool:
    return os.environ.get('OPENCLAW_ALPACA_LIVE_LIQUIDATE') == '1'


def _run_cli(args, timeout=30):
    """Mirror of alpaca_replace_stop._run_cli for consistent error shapes."""
    proc = subprocess.run(
        [ALPACA_CLI, *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode == 0:
        try:
            return True, json.loads(proc.stdout), None
        except json.JSONDecodeError:
            return True, proc.stdout, None
    err = {'exit_code': proc.returncode, 'raw_stderr': proc.stderr,
           'status': None, 'error': proc.stderr.strip()}
    try:
        ej = json.loads(proc.stderr)
        err.update({'status': ej.get('status'),
                    'error': ej.get('error') or err['error'],
                    'code': ej.get('code'), 'error_json': ej})
    except json.JSONDecodeError:
        pass
    return False, None, err


def _market_is_open() -> bool:
    """True if `alpaca clock` reports RTH. Default to False on any failure
    — safer because the manual flatten path refuses to submit outside RTH
    (see liquidate_on_regime_change RTH guard, Phase 4 2026-05-19)."""
    ok, payload, _err = _run_cli(['clock'], timeout=5)
    if not ok or not isinstance(payload, dict):
        return False
    return bool(payload.get('is_open'))


def _redis():
    """Best-effort Redis client. Returns None if redis-py missing or the
    server is unreachable — caller treats that as 'no idempotency'."""
    try:
        import redis
        url = os.environ.get('REDIS_URL', 'redis://localhost:6379')
        r = redis.from_url(url, socket_connect_timeout=3, decode_responses=True)
        r.ping()
        return r
    except Exception as e:
        logger.warning('[liquidate] redis unavailable: %s', e)
        return None


def _post_to_discord(channel: str, msg: str) -> bool:
    """Look up the persisted webhook for `channel` in agent_registry and
    POST `msg`. Mirrors pipeline_orchestrator.post_channel() but standalone
    so we don't drag in that whole module's top-level state."""
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        return False
    try:
        import psycopg2
        import requests
    except ImportError:
        return False
    url = None
    try:
        conn = psycopg2.connect(uri, connect_timeout=5)
        cur = conn.cursor()
        cur.execute(
            "SELECT webhook_urls FROM agent_registry WHERE webhook_urls IS NOT NULL"
        )
        for (hooks,) in cur.fetchall():
            if hooks and channel in hooks:
                url = hooks[channel]
                break
        cur.close()
        conn.close()
    except Exception as e:
        logger.warning('[liquidate] webhook lookup failed: %s', e)
        return False
    if not url:
        logger.info('[liquidate] no webhook for #%s', channel)
        return False
    try:
        r = requests.post(url, json={'content': msg[:1900]}, timeout=10)
        return bool(r.ok)
    except Exception as e:
        logger.warning('[liquidate] webhook post failed: %s', e)
        return False


def _load_regime() -> dict | None:
    """Read regime_latest.json. None on missing/corrupt file."""
    if not REGIME_LATEST_FILE.exists():
        return None
    try:
        with open(REGIME_LATEST_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _load_open_openclaw_submissions(conn) -> list[dict]:
    """OpenClaw submissions still considered on-book per alpaca_reconcile's
    broker_status. Filtering by COID prefix is the OpenClaw signature."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT client_order_id, alpaca_order_id, ticker, direction, qty
          FROM alpaca_submissions
         WHERE submitted_at > NOW() - INTERVAL '30 days'
           AND broker_status IN ('filled', 'partial')
           AND client_order_id LIKE %s
        """,
        (f'{COID_PREFIX}%',),
    )
    rows = [
        {'client_order_id': cid, 'alpaca_order_id': aoid,
         'ticker': tkr, 'direction': dirn, 'qty': qty}
        for cid, aoid, tkr, dirn, qty in cur.fetchall()
    ]
    cur.close()
    return rows


def _load_broker_positions() -> dict:
    """Map symbol → {qty, side, market_value}. Empty on CLI failure."""
    ok, payload, err = _run_cli(['position', 'list'], timeout=15)
    if not ok or not isinstance(payload, list):
        logger.warning('[liquidate] position list failed: %s',
                       (err or {}).get('error'))
        return {}
    out = {}
    for p in payload:
        if not isinstance(p, dict):
            continue
        sym = p.get('symbol')
        if not sym:
            continue
        try:
            qty = float(p.get('qty') or 0)
        except (TypeError, ValueError):
            qty = 0.0
        out[sym] = {'qty': qty, 'side': p.get('side'),
                    'market_value': p.get('market_value')}
    return out


def _load_open_orders() -> list[dict]:
    """All open orders on the account, fetched with --nested so legs[] is
    populated. We later filter by COID prefix."""
    ok, payload, err = _run_cli(
        ['order', 'list', '--status', 'open', '--nested', '--limit', '500'],
        timeout=15,
    )
    if not ok or not isinstance(payload, list):
        logger.warning('[liquidate] order list failed: %s',
                       (err or {}).get('error'))
        return []
    return payload


def _collect_openclaw_orders_to_cancel(open_orders: list[dict]) -> list[dict]:
    """Return every open order (parent or leg) attributable to OpenClaw
    via the AX COID prefix. Bracket parents and their legs each carry
    distinct order_ids; both must be cancelled."""
    out = []
    for ord_ in open_orders:
        if not isinstance(ord_, dict):
            continue
        coid = ord_.get('client_order_id') or ''
        parent_is_oc = coid.startswith(COID_PREFIX)
        if parent_is_oc:
            out.append({'order_id': ord_.get('id'),
                        'client_order_id': coid,
                        'symbol': ord_.get('symbol'),
                        'role': 'parent'})
        for leg in (ord_.get('legs') or []):
            if not isinstance(leg, dict):
                continue
            leg_coid = leg.get('client_order_id') or coid
            if parent_is_oc or leg_coid.startswith(COID_PREFIX):
                out.append({'order_id': leg.get('id'),
                            'client_order_id': leg_coid,
                            'symbol': leg.get('symbol') or ord_.get('symbol'),
                            'role': 'leg'})
    seen, deduped = set(), []
    for o in out:
        if o['order_id'] and o['order_id'] not in seen:
            seen.add(o['order_id'])
            deduped.append(o)
    return deduped


def _close_symbol(symbol: str, qty: float,
                  market_open: bool | None = None) -> tuple[bool, dict]:
    """Submit a market close for `symbol` via the RTH close path.

    Uses `alpaca position close --symbol-or-asset-id <SYM>`. The OPG /
    pre-market `order submit` branch was removed 2026-05-19 — the public
    entry (`liquidate_on_regime_change`) refuses to run outside RTH, so
    this helper assumes RTH.

    `qty` is retained for caller-side bookkeeping (audit row + outcome
    classification) but is not passed to the CLI: `position close`
    flattens the broker's current position fully.

    `market_open` is an IGNORED backward-compat shim for external
    callers (notably `position_circuit_breaker.py`, which gates its own
    invocation on `_market_is_open()` already). Behaviour no longer
    depends on it — there is only one (RTH) close path. New callers
    should omit it.

    Cancel-then-close (2026-06-08): a position whose qty is fully reserved
    by a resting protective order (e.g. a GTC OCO take-profit on a short)
    makes `position close` 403 with code 40310000 "insufficient qty
    available". We try the close first and only on THAT error cancel the
    symbol's open orders and retry once — so a position that closes cleanly
    keeps its brackets, and we strip protection only when it actually
    blocks the flatten (which is the deliberate intent of a flatten).
    """
    _ = market_open  # intentionally unused; see docstring
    ok, payload, err = _run_cli(
        ['position', 'close', '--symbol-or-asset-id', symbol], timeout=15,
    )
    if ok:
        return True, payload

    detail = err or {}
    if _is_insufficient_qty(detail) and _cancel_symbol_orders(symbol) > 0:
        ok2, payload2, err2 = _run_cli(
            ['position', 'close', '--symbol-or-asset-id', symbol], timeout=15,
        )
        return ok2, (payload2 if ok2 else (err2 or {}))
    return False, detail


def _is_insufficient_qty(detail: dict) -> bool:
    """True if a close failure means the position qty is held by resting
    orders (Alpaca code 40310000 / "insufficient qty available")."""
    if not isinstance(detail, dict):
        return False
    if detail.get('code') == 40310000:
        return True
    return 'insufficient qty' in str(detail.get('error') or '').lower()


def _cancel_symbol_orders(symbol: str) -> int:
    """Cancel every OPEN order resting on `symbol`, releasing qty held by a
    protective GTC/OCO so a subsequent `position close` isn't 403'd. Returns
    the count successfully cancelled. Best-effort: per-order CLI failures are
    counted out, not raised — the close attempt + its audit are the real
    outcome signal."""
    n = 0
    for o in _load_open_orders():
        if not isinstance(o, dict) or o.get('symbol') != symbol:
            continue
        oid = o.get('id')
        if not oid:
            continue
        ok, _res = _cancel_order(oid)
        if ok:
            n += 1
    return n


# Alpaca order statuses considered terminal for polling purposes. Note that
# `partially_filled` is NOT terminal — the broker can still fill more, so
# we keep polling until one of these is observed or the budget elapses.
_TERMINAL_ORDER_STATUSES = frozenset({
    'filled', 'expired', 'canceled', 'cancelled',
    'rejected', 'done_for_day', 'replaced', 'failed',
})


def _poll_to_terminal(order_id: str,
                      timeout_s: int = 90,
                      interval_s: int = 3) -> dict | None:
    """Poll `alpaca order get --order-id <id>` until the order reaches a
    terminal status or until `timeout_s` elapses. Returns the final order
    dict, or None if every poll failed (CLI errors / non-dict payloads).

    Terminal statuses: filled, expired, canceled/cancelled, rejected,
    done_for_day, replaced, failed. `partially_filled` is non-terminal
    and we keep polling — the partial classification is computed from
    filled_qty vs requested_qty by `_classify_outcome`, not the status
    string.
    """
    if not order_id:
        return None
    deadline = time.monotonic() + max(1, timeout_s)
    last_dict: dict | None = None
    while True:
        ok, payload, _err = _run_cli(
            ['order', 'get', '--order-id', order_id], timeout=10,
        )
        if ok and isinstance(payload, dict):
            last_dict = payload
            status = str(payload.get('status') or '').lower()
            if status in _TERMINAL_ORDER_STATUSES:
                return payload
        if time.monotonic() >= deadline:
            return last_dict
        time.sleep(max(1, interval_s))


def _classify_outcome(submit_ok: bool,
                      final_order: dict | None,
                      qty: float) -> str:
    """Classify the real outcome of a close attempt for audit purposes.

    Returns one of:
      - 'submit_error' — CLI returned non-zero on submit; never got an id.
      - 'filled'       — all requested qty filled (status='filled' OR
                         filled_qty >= requested_qty).
      - 'partial'      — some qty filled, then terminal (or timed out
                         with a non-zero filled_qty).
      - 'pending'      — never reached a terminal state within the
                         polling budget (and no fills observed).
      - 'rejected'     — terminal but zero fills (expired, rejected,
                         canceled, done_for_day, replaced, failed).
    """
    if not submit_ok:
        return 'submit_error'
    if not isinstance(final_order, dict):
        return 'pending'

    requested = abs(float(qty)) if qty is not None else 0.0
    try:
        filled = abs(float(final_order.get('filled_qty') or 0))
    except (TypeError, ValueError):
        filled = 0.0
    status = str(final_order.get('status') or '').lower()

    if status == 'filled' or (requested > 0 and filled >= requested):
        return 'filled'

    is_terminal = status in _TERMINAL_ORDER_STATUSES
    if filled > 0:
        # Partial fill — either explicitly terminal with some fills, or
        # poll-timed-out with some fills already booked.
        return 'partial'
    if is_terminal:
        return 'rejected'
    return 'pending'


def _cancel_order(order_id: str) -> tuple[bool, dict]:
    ok, payload, err = _run_cli(
        ['order', 'cancel', '--order-id', order_id], timeout=10,
    )
    return ok, (payload if ok else (err or {}))


def _record_audit(conn, run_date, regime_from, regime_to,
                  symbol, qty, side_closed, parent_coids,
                  cancel_results, close_result, status, error_msg):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO alpaca_liquidations
          (run_date, regime_from, regime_to, symbol, qty, side_closed,
           parent_client_order_ids, cancel_results, close_result,
           result_status, error_msg)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (run_date, regime_from, regime_to, symbol, qty, side_closed,
         parent_coids, json.dumps(cancel_results),
         json.dumps(close_result) if close_result is not None else None,
         status, error_msg),
    )
    cur.close()


# ── Public entry ────────────────────────────────────────────────────────────

def liquidate_on_regime_change(run_date: str | None = None,
                                force_dry_run: bool = False,
                                force_override: bool = False,
                                force_transition_tag: str | None = None) -> dict:
    """Public entry. Returns a dict describing what happened. See module
    docstring for the contract.

    `force_override=True` bypasses both the missing-state and same-state
    early returns. Used for one-shot operator-initiated flattens (e.g.
    pre-regime-transition cleanup, post-fix portfolio reset). The audit
    rows tag `regime_from='MANUAL_FORCE'` and the Redis sentinel uses a
    unique transition key (`MANUAL_FORCE->{state}`) so a forced flatten
    on the same date as a natural regime change doesn't silently mask
    one another's idempotency check.

    `force_transition_tag` (when set, implies force_override=True) lets
    callers supply their own transition key — e.g. the intraday HMM
    detector passes `INTRADAY_HMM_LOW_VOL_HIGH_VOL`. Audit + sentinel
    use the supplied tag so daily, manual, and intraday fires are all
    distinguishable in `alpaca_liquidations`.

    Cooldown gate: `liquidate:cooldown:{date}` Redis key (TTL 60 min,
    set after every successful live fire) blocks BOTH the daily 9 AM
    cron and the intraday detector. Prevents back-to-back flattens on
    whipsaws. The transition-keyed sentinel (`liquidate:fired:...`)
    handles same-day-same-transition idempotency at 24h TTL.
    """
    regime = _load_regime()
    if regime is None:
        logger.warning('[liquidate] regime_latest.json missing or corrupt — abort')
        return {'action': 'error', 'reason': 'regime_unreadable'}

    state = regime.get('state')
    prior = regime.get('prior_state')
    date_str = (run_date or regime.get('date')
                or datetime.now(timezone.utc).strftime('%Y-%m-%d'))
    # A non-empty force_transition_tag implies force_override; callers
    # don't need to pass both. Mirrors the way --force on the CLI implies
    # the override.
    if force_transition_tag and not force_override:
        force_override = True
    if force_override:
        if force_transition_tag:
            transition_key = force_transition_tag
            # Surface a "from" string for audit columns; if the tag has
            # the standard `<from>_<to>` shape we pull it apart, else we
            # tag the whole string as the from-state.
            prior = force_transition_tag
            state = state or 'UNKNOWN'
        else:
            # Synthesise a transition key + audit fields. `state` is
            # whatever the current regime is (informational); `prior`
            # becomes the MANUAL_FORCE sentinel value. If state itself
            # is missing we still proceed — flattening doesn't depend
            # on the regime read.
            prior = 'MANUAL_FORCE'
            state = state or 'UNKNOWN'
            transition_key = f'{prior}->{state}'
        sentinel_key = f'liquidate:fired:{date_str}:{transition_key}'
    else:
        if not state or not prior:
            return {'action': 'noop', 'reason': 'missing_state_or_prior',
                    'state': state, 'prior_state': prior}
        if state == prior:
            return {'action': 'noop', 'state': state, 'prior_state': prior}

        transition_key = f'{prior}->{state}'
        sentinel_key = f'liquidate:fired:{date_str}:{transition_key}'

    cooldown_key = COOLDOWN_KEY_TEMPLATE.format(date=date_str)
    rcli = _redis()
    if rcli is not None:
        try:
            # Cooldown supersedes the per-transition sentinel — when set,
            # NO liquidation fires regardless of transition novelty.
            if rcli.get(cooldown_key):
                return {'action': 'noop', 'reason': 'cooldown_active',
                        'transition': transition_key,
                        'cooldown_key': cooldown_key}
            if rcli.get(sentinel_key):
                return {'action': 'already_fired', 'transition': transition_key}
        except Exception:
            pass

    live = _is_live() and not force_dry_run

    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        return {'action': 'error', 'reason': 'POSTGRES_URI not set'}
    try:
        import psycopg2
    except ImportError:
        return {'action': 'error', 'reason': 'psycopg2 not installed'}

    conn = psycopg2.connect(uri, connect_timeout=10)
    try:
        oc_subs = _load_open_openclaw_submissions(conn)
    except Exception as e:
        conn.close()
        return {'action': 'error', 'reason': f'submissions query failed: {e}'}

    oc_symbols = sorted({r['ticker'] for r in oc_subs})
    coids_by_symbol: dict[str, list[str]] = {}
    for r in oc_subs:
        coids_by_symbol.setdefault(r['ticker'], []).append(r['client_order_id'])

    # RTH-only guard: refuse to run outside regular trading hours. Applies
    # to BOTH the natural (regime-change) and forced (`--force`) paths.
    # The OPG submission branch was removed 2026-05-19 after the
    # 2026-05-18 paper-fill incident; the only safe close path is the RTH
    # `position close` call. We placed this AFTER the cooldown/sentinel
    # gates (those are soft early-outs) and AFTER the DB read so the
    # error payload can surface the symbols that would have been closed.
    market_open = _market_is_open()
    if not market_open:
        conn.close()
        logger.error(
            '[liquidate] market is not currently open '
            '(manual flatten requires RTH) — aborting; rerun after 9:30 ET'
        )
        return {'action': 'error', 'reason': 'not_rth',
                'symbols_skipped': oc_symbols}

    plan = {'transition': transition_key, 'date': date_str,
            'market_open': market_open, 'oc_symbols': oc_symbols}

    if not live:
        # Read-only broker calls so the operator sees intended-vs-actual.
        positions = _load_broker_positions()
        open_orders = _load_open_orders()
        cancels = _collect_openclaw_orders_to_cancel(open_orders)
        plan['cancel'] = cancels
        plan['close'] = [
            {'symbol': s, 'qty': positions.get(s, {}).get('qty', 0),
             'tif': 'day'}
            for s in oc_symbols if positions.get(s, {}).get('qty')
        ]
        conn.close()
        logger.info('[liquidate] DRY-RUN plan: %s', json.dumps(plan)[:1000])
        _post_to_discord(
            'trade-reports',
            f':warning: **Regime change** {prior} → {state} (DRY-RUN). '
            f'Would cancel {len(plan["cancel"])} orders, '
            f'flatten {len(plan["close"])} symbols '
            f'({", ".join(oc_symbols[:8])}).',
        )
        return {'action': 'dry_run', 'plan': plan, 'live': False}

    # ── LIVE PATH ─────────────────────────────────────────────────────────
    open_orders = _load_open_orders()
    cancels = _collect_openclaw_orders_to_cancel(open_orders)
    cancel_results: dict[str, dict] = {}
    for entry in cancels:
        oid = entry.get('order_id')
        if not oid:
            continue
        ok, payload = _cancel_order(oid)
        cancel_results[oid] = {
            'ok': ok, 'symbol': entry.get('symbol'),
            'role': entry.get('role'),
            'detail': (payload.get('error') if isinstance(payload, dict) else None),
        }

    # Brief pause so the broker's order state catches up before close calls.
    time.sleep(0.5)

    positions = _load_broker_positions()
    close_results = []
    for sym in oc_symbols:
        pos = positions.get(sym)
        if not pos or not pos.get('qty'):
            continue
        qty = pos['qty']
        ok, payload = _close_symbol(sym, qty)
        # The submit response carries the broker-assigned order id; we
        # then poll that id to a terminal state so the audit row reflects
        # the REAL outcome rather than the submission ack. The audit
        # `close_result` JSONB column stores the final polled order (not
        # the initial pending_new payload) so operators can inspect the
        # fill timeline directly from the table.
        close_id = payload.get('id') if (ok and isinstance(payload, dict)) else None
        final_order = _poll_to_terminal(close_id) if (ok and close_id) else None
        outcome = _classify_outcome(ok, final_order, qty=qty)
        # Prefer the polled final order for the audit payload; fall back
        # to the initial submit payload if polling never returned a dict.
        audit_payload = final_order if isinstance(final_order, dict) else (
            payload if ok else None
        )
        close_results.append({'symbol': sym, 'ok': ok, 'qty': qty,
                              'tif': 'day',
                              'outcome': outcome,
                              'order_id': close_id,
                              'final_order': final_order,
                              'response': payload})
        try:
            _record_audit(
                conn, date_str, prior, state, sym, qty,
                'long_close' if qty > 0 else 'short_close',
                coids_by_symbol.get(sym, []),
                {oid: r for oid, r in cancel_results.items()
                 if r.get('symbol') == sym},
                audit_payload,
                outcome,
                None if outcome == 'filled' else json.dumps(payload)[:500],
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning('[liquidate] audit insert failed for %s: %s', sym, e)

    conn.close()

    n_cancel_ok = sum(1 for r in cancel_results.values() if r.get('ok'))
    # n_close_ok now counts TERMINAL FILLS, not submission-acks (Phase 4
    # poll-to-terminal change). Anything not filled — partial, pending,
    # rejected, submit_error — counts toward n_close_err for the May-9
    # sentinel guard. This makes a zero-fill run leave the sentinel +
    # cooldown unset, so an operator retry isn't silently masked.
    n_close_ok = sum(1 for r in close_results
                     if r.get('outcome') == 'filled')
    n_close_err = len(close_results) - n_close_ok
    n_partial   = sum(1 for r in close_results if r.get('outcome') == 'partial')
    n_pending   = sum(1 for r in close_results if r.get('outcome') == 'pending')
    n_rejected  = sum(1 for r in close_results if r.get('outcome') == 'rejected')
    n_submit_err = sum(1 for r in close_results
                       if r.get('outcome') == 'submit_error')
    # A run is "successful enough" to record the sentinel + cooldown only
    # when at least one close succeeded, or when there was nothing to
    # close (already flat). If every close errored, we treat the run as
    # failed and do NOT seal the sentinel/cooldown — that way the next
    # cron tick (or operator retry) can attempt again instead of being
    # silently masked. Without this, May 9 2026's CLI-flag regression
    # marked `liquidated` despite zero successful closes.
    fire_succeeded = (n_close_ok > 0) or (len(close_results) == 0)

    if rcli is not None and fire_succeeded:
        try:
            rcli.set(sentinel_key, '1', ex=SENTINEL_TTL)
            rcli.set(cooldown_key, '1', ex=COOLDOWN_TTL)
            # Day-1-of-regime force-fire flag: regime_blended_sizer
            # consumes this in the next cycle to bypass the cadence gate,
            # ensuring every eligible strategy fires fresh under the new regime.
            rcli.set('regime:transition:fresh', state or 'UNKNOWN', ex=24 * 3600)
            logger.info('[liquidate] set regime:transition:fresh ttl=24h state=%s', state)
        except Exception:
            pass

    closed_syms = ", ".join(
        r["symbol"] for r in close_results if r.get("outcome") == 'filled'
    )[:200]
    # Up to 10 non-filled symbols with their outcome, for operator triage.
    non_filled = [r for r in close_results if r.get('outcome') != 'filled']
    non_filled_lines = "\n".join(
        f'    - {r["symbol"]}: {r.get("outcome", "?")}'
        for r in non_filled[:10]
    )
    if non_filled and len(non_filled) > 10:
        non_filled_lines += f'\n    - … and {len(non_filled) - 10} more'

    breakdown = (
        f'filled={n_close_ok} / partial={n_partial} / '
        f'pending={n_pending} / rejected={n_rejected} / '
        f'submit_error={n_submit_err}'
    )

    if not fire_succeeded:
        summary = (
            f':x: **Regime liquidation FAILED** {prior} → {state}\n'
            f'• Cancelled: {n_cancel_ok}/{len(cancel_results)} orders\n'
            f'• Closed:    {n_close_ok}/{len(close_results)} symbols\n'
            f'• Breakdown: {breakdown}\n'
            f'• Errors:    {n_close_err} (sentinel + cooldown NOT set — retry-eligible)'
        )
    else:
        summary = (
            f':rotating_light: **Regime liquidation** {prior} → {state}\n'
            f'• Cancelled: {n_cancel_ok}/{len(cancel_results)} orders\n'
            f'• Closed:    {n_close_ok}/{len(close_results)} symbols ({closed_syms})\n'
            f'• Breakdown: {breakdown}\n'
            f'• Errors:    {n_close_err}'
        )
    if non_filled_lines:
        summary += f'\n• Non-filled:\n{non_filled_lines}'
    _post_to_discord('trade-reports', summary)

    if not fire_succeeded:
        action = 'failed'
    elif n_close_err > 0:
        action = 'liquidated_partial'
    else:
        action = 'liquidated'
    return {'action': action, 'transition': transition_key,
            'cancel_results': cancel_results,
            'close_results': close_results, 'live': True}


def _main():
    """CLI entry for the cron and operator. Exit codes:
      0 — normal (noop, dry_run, already_fired, or live success)
      1 — partial errors during live close
      2 — unrecoverable (regime unreadable, env/imports missing)
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env')
    except (ImportError, PermissionError, OSError):
        pass

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [LIQUIDATE] %(message)s')

    ap = argparse.ArgumentParser()
    ap.add_argument('--date', help='Override run_date (YYYY-MM-DD)')
    ap.add_argument('--dry-run', action='store_true',
                    help='Force dry-run even if env flag is set')
    ap.add_argument('--force', action='store_true',
                    help='Bypass the same-state veto. Use for one-shot '
                         'operator-initiated flattens (synthetic '
                         'MANUAL_FORCE transition; unique sentinel key).')
    args = ap.parse_args()

    result = liquidate_on_regime_change(run_date=args.date,
                                         force_dry_run=args.dry_run,
                                         force_override=args.force)
    print(json.dumps(result, default=str))
    if result.get('action') == 'error':
        sys.exit(2)
    if result.get('action') == 'failed':
        sys.exit(1)
    if result.get('action') in ('liquidated', 'liquidated_partial'):
        # Anything not 'filled' is bad for exit-code purposes (partial,
        # pending, rejected, submit_error all warrant a non-zero exit so
        # the wrapper script surfaces it). Mirrors n_close_err semantics.
        bad = sum(1 for r in result.get('close_results', [])
                  if r.get('outcome') != 'filled')
        if bad:
            sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    _main()


# ── close_subset: additive extended-hours partial flatten ────────────────────
# These helpers exist alongside the existing _close_symbol / liquidate_on_regime_change
# path and do NOT touch it. The existing _load_broker_positions (dict) is
# intentionally left unchanged — this new helper returns a list[dict] and uses
# a distinct name to avoid any collision.


def _load_broker_positions_list() -> list[dict]:
    """Shell out to `alpaca position list` and return the raw list-of-dicts.
    Returns [] on any CLI error. Distinct from the existing _load_broker_positions
    (which returns a dict) to avoid collision with liquidate_on_regime_change."""
    ok, payload, _err = _run_cli(['position', 'list'], timeout=15)
    if not ok or not isinstance(payload, list):
        return []
    return payload


def _submit_extended_hours_close(order: dict) -> dict:
    """Fetch a marketable limit price and submit a single pre-market limit close
    via `alpaca order submit --type limit --extended-hours`.

    The `order` dict must contain: symbol, side ('buy'|'sell'), qty.

    Raises RuntimeError if:
      - _pick_limit_price returns None (no usable quote — halted/quoteless ticker)
      - the alpaca CLI call returns non-zero

    `_pick_limit_price` is called here (not in the caller) so that mocking
    `_submit_extended_hours_close` in tests bypasses the quote-fetch entirely."""
    from src.execution.alpaca_executor import _pick_limit_price
    symbol = order['symbol']
    side = order['side']
    limit_px = _pick_limit_price(symbol, side)
    if limit_px is None:
        raise RuntimeError(
            f'_pick_limit_price returned None for {symbol} — '
            'no usable quote or trade available (halted/quoteless?)'
        )
    cmd = [
        'order', 'submit',
        '--symbol', symbol,
        '--side', side,
        '--type', 'limit',
        '--time-in-force', 'day',
        '--extended-hours',
        '--qty', str(order['qty']),
        '--limit-price', f'{limit_px:.2f}',
    ]
    ok, payload, err = _run_cli(cmd, timeout=15)
    if not ok:
        raise RuntimeError(f'alpaca CLI: {err}')
    return {
        'status': (payload.get('status', 'pending') if isinstance(payload, dict) else 'pending'),
        'filled_qty': float((payload.get('filled_qty', 0) or 0) if isinstance(payload, dict) else 0),
        'avg_fill_price': float((payload.get('filled_avg_price', 0) or 0) if isinstance(payload, dict) else 0),
        'order_id': (payload.get('id') if isinstance(payload, dict) else None),
    }


def _write_liquidation_audit(row: dict) -> str | None:
    """INSERT into alpaca_liquidations using the actual table schema and
    return the auto-generated UUID (as string) or None on failure.
    id is UUID with DEFAULT gen_random_uuid() — NOT specified in the INSERT.

    The row dict must contain keys: run_date, regime_from, regime_to,
    ticker, direction, qty, result_status.
    Optional: market_value_usd, filled_qty, avg_fill_price.
    These are persisted in close_result JSONB so no schema migration is needed.

    Note: alpaca_liquidations uses 'symbol' (not 'ticker') and 'side_closed'
    (not 'direction') — the close_subset-specific field names in the row dict
    are mapped to the actual schema here."""
    try:
        import psycopg2
    except ImportError:
        logger.warning('[close_subset] psycopg2 not available — audit skipped')
        return None
    dsn = os.environ.get('POSTGRES_URI')
    if not dsn:
        logger.warning('[close_subset] POSTGRES_URI not set — audit skipped')
        return None
    # Pack fill details into close_result JSONB (existing column) to avoid
    # schema migration — alpaca_liquidations has no ticker/direction/market_value_usd
    # columns; those are close_subset-internal labels stored here for traceability.
    close_result_payload = json.dumps({
        'source': 'close_subset',
        'ticker': row.get('ticker'),
        'direction': row.get('direction'),
        'market_value_usd': row.get('market_value_usd'),
        'filled_qty': row.get('filled_qty'),
        'avg_fill_price': row.get('avg_fill_price'),
    })
    side_closed = ('long_close' if row.get('direction') == 'long' else 'short_close')
    try:
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO alpaca_liquidations
                    (run_date, regime_from, regime_to, symbol, qty,
                     side_closed, close_result, result_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    row['run_date'], row['regime_from'], row['regime_to'],
                    row.get('ticker'), row.get('qty'),
                    side_closed,
                    close_result_payload,
                    row['result_status'],
                ),
            )
            new_id = cur.fetchone()[0]
            conn.commit()
        return str(new_id) if new_id else None
    except Exception as e:
        logger.warning('[close_subset] audit INSERT failed: %s', e)
        return None


def close_subset(tickers: list[str], reason: str) -> list[dict]:
    """Flatten a SUBSET of broker equity positions using extended-hours limit orders.

    Does NOT touch the existing liquidate_on_regime_change / _close_symbol path.
    Filters broker positions to us_equity asset_class only (crypto/options skipped).

    Returns a list of per-ticker result dicts with keys:
      ticker, status — always present
      qty, filled_qty, avg_fill_price, liquidation_id — on success
      error — on submit_error

    status values: 'filled' | 'pending' | <broker status> | 'submit_error' | 'no_position'
    """
    if not tickers:
        return []
    from datetime import date as _date
    positions = _load_broker_positions_list()
    by_sym = {
        p['symbol']: p
        for p in positions
        if isinstance(p, dict) and p.get('asset_class') == 'us_equity'
    }

    results: list[dict] = []
    run_date = _date.today()
    for ticker in tickers:
        pos = by_sym.get(ticker)
        if pos is None:
            results.append({'ticker': ticker, 'status': 'no_position'})
            continue

        try:
            signed_qty = float(pos['qty'])
        except (TypeError, ValueError):
            results.append({'ticker': ticker, 'status': 'no_position'})
            continue

        abs_qty = abs(signed_qty)
        side = 'sell' if signed_qty > 0 else 'buy'

        try:
            outcome = _submit_extended_hours_close({
                'symbol': ticker, 'side': side, 'qty': abs_qty,
            })
            audit_id = _write_liquidation_audit({
                'run_date': run_date,
                'regime_from': reason,
                'regime_to': 'FLAT',
                'ticker': ticker,
                'direction': 'long' if signed_qty > 0 else 'short',
                'qty': abs_qty,
                'market_value_usd': float(pos.get('market_value') or 0),
                'result_status': outcome['status'],
                'filled_qty': outcome['filled_qty'],
                'avg_fill_price': outcome['avg_fill_price'],
            })
            results.append({
                'ticker': ticker,
                'status': outcome['status'],
                'qty': abs_qty,
                'filled_qty': outcome['filled_qty'],
                'avg_fill_price': outcome['avg_fill_price'],
                'liquidation_id': audit_id,
            })
        except Exception as e:
            logger.warning('[close_subset] %s: %s', ticker, e)
            _write_liquidation_audit({
                'run_date': run_date,
                'regime_from': reason,
                'regime_to': 'FLAT',
                'ticker': ticker,
                'direction': 'long' if signed_qty > 0 else 'short',
                'qty': abs_qty,
                'market_value_usd': float(pos.get('market_value') or 0),
                'result_status': 'submit_error',
            })
            results.append({'ticker': ticker, 'status': 'submit_error', 'error': str(e)})
    return results
