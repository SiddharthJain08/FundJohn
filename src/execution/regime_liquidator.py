#!/usr/bin/env python3
"""regime_liquidator.py — flatten OpenClaw positions on regime transitions.

Hook: invoked by src/engine/cron-schedule.js right after the 9:00 AM ET
regime refresh writes /root/openclaw/.agents/market-state/regime_latest.json.
When today's `state` differs from `prior_state` this module:
  1. Cancels every open OpenClaw order (parent + bracket legs). The COID
     prefix 'AX' is the OpenClaw signature (see alpaca_executor.py:283).
  2. Submits a market close per OpenClaw symbol still showing a non-zero
     position at the broker. RTH uses `alpaca position close`; pre-/post-
     market uses an OPG market order on the opposite side. 9 AM ET is
     pre-market, so OPG is the default.
  3. Audits every action into the alpaca_liquidations table.
  4. Sets a Redis idempotency sentinel so a same-day re-run is a no-op.
  5. Posts a summary to #trade-reports.

Default mode is DRY-RUN (logs the intended plan, no broker calls). Set
OPENCLAW_ALPACA_LIVE_LIQUIDATE=1 to enable. Pattern mirrors
src/execution/alpaca_replace_stop.py.

The 10 AM trading cycle is intentionally NOT blocked: after liquidation
the system is free to rotate into fresh signals sized for the new
regime within the same morning. (Per user direction.)

Trigger semantics:
- Only `prior_state != state` triggers. The broader `regime_change_alert`
  flag also fires on confidence dips and stress spikes; firing
  liquidation on those would be surprising behaviour.
- The persisted `state` is the effective_state from run_market_state.py
  (confidence-override already applied). prior_state is pulled from the
  previous file's `state`. Comparison is therefore effective-to-effective
  and needs no special-casing.
- A corrupt regime_latest.json (incomplete write) returns cleanly with
  no broker calls.
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
    — safer because OPG queues, day rejects pre-market."""
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
                  market_open: bool) -> tuple[bool, dict]:
    """Submit a market close for `symbol`.
    RTH:           `alpaca position close --symbol-or-asset-id <SYM>`
    Pre/post-mkt:  `alpaca order submit ... --type market --time-in-force opg`
    """
    if market_open:
        ok, payload, err = _run_cli(
            ['position', 'close', '--symbol-or-asset-id', symbol], timeout=15,
        )
        return ok, (payload if ok else (err or {}))

    abs_qty = abs(qty)
    qty_str = str(int(abs_qty)) if float(abs_qty).is_integer() else f'{abs_qty}'
    side = 'sell' if qty > 0 else 'buy'
    coid = f'AXLIQ_{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")}_{symbol}'[:128]
    # Alpaca CLI flag is `--symbol` for `order submit` (`--symbol-or-asset-id`
    # only exists on `position close`). 2026-05-14 fix: the closed-market
    # path was rejected wholesale with "unknown flag: --symbol-or-asset-id"
    # before this rename.
    ok, payload, err = _run_cli(
        ['order', 'submit',
         '--symbol', symbol,
         '--side', side,
         '--qty', qty_str,
         '--type', 'market',
         '--time-in-force', 'opg',
         '--client-order-id', coid],
        timeout=15,
    )
    return ok, (payload if ok else (err or {}))


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
    market_open = _market_is_open()

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
             'tif': 'day' if market_open else 'opg'}
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
        ok, payload = _close_symbol(sym, qty, market_open)
        close_results.append({'symbol': sym, 'ok': ok, 'qty': qty,
                              'tif': 'day' if market_open else 'opg',
                              'response': payload})
        try:
            _record_audit(
                conn, date_str, prior, state, sym, qty,
                'long_close' if qty > 0 else 'short_close',
                coids_by_symbol.get(sym, []),
                {oid: r for oid, r in cancel_results.items()
                 if r.get('symbol') == sym},
                payload if ok else None,
                'closed' if ok else 'error',
                None if ok else json.dumps(payload)[:500],
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning('[liquidate] audit insert failed for %s: %s', sym, e)

    conn.close()

    n_cancel_ok = sum(1 for r in cancel_results.values() if r.get('ok'))
    n_close_ok = sum(1 for r in close_results if r.get('ok'))
    n_close_err = len(close_results) - n_close_ok
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
            # Day-1-of-regime force-fire flag: the new
            # OPENCLAW_SHARPE_CADENCE_SIZER path consumes this in the
            # next cycle to bypass the cadence gate, ensuring every
            # eligible strategy fires fresh under the new regime.
            rcli.set('regime:transition:fresh', state or 'UNKNOWN', ex=24 * 3600)
            logger.info('[liquidate] set regime:transition:fresh ttl=24h state=%s', state)
        except Exception:
            pass

    closed_syms = ", ".join(r["symbol"] for r in close_results if r["ok"])[:200]
    if not fire_succeeded:
        summary = (
            f':x: **Regime liquidation FAILED** {prior} → {state}\n'
            f'• Cancelled: {n_cancel_ok}/{len(cancel_results)} orders\n'
            f'• Closed:    {n_close_ok}/{len(close_results)} symbols\n'
            f'• Errors:    {n_close_err} (sentinel + cooldown NOT set — retry-eligible)'
        )
    else:
        summary = (
            f':rotating_light: **Regime liquidation** {prior} → {state}\n'
            f'• Cancelled: {n_cancel_ok}/{len(cancel_results)} orders\n'
            f'• Closed:    {n_close_ok}/{len(close_results)} symbols ({closed_syms})\n'
            f'• Errors:    {n_close_err}'
        )
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
        bad = sum(1 for r in result.get('close_results', []) if not r.get('ok'))
        if bad:
            sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    _main()
