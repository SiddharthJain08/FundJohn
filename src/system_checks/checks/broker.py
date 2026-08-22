"""Broker checks — Alpaca session, account, positions sanity."""
from __future__ import annotations

import os

import psycopg2

from ..registry import check
from ..types import Status


def _session():
    """Use the production session factory so we exercise the same path as
    execution code."""
    import sys
    sys.path.insert(0, '/root/openclaw/src')
    from execution.alpaca_trader import _alpaca_session
    return _alpaca_session()


@check(name='alpaca_session_authed', tags=['broker'], requires=['broker'])
def _alpaca_session_authed():
    """Real session can fetch /v2/account — i.e., _base attribute set and keys valid.
    Regression for the 2026-05-13 3× `_base` AttributeError bug."""
    sess = _session()
    r = sess.get(f'{sess._base}/v2/account', timeout=10)
    if r.status_code == 401:
        return Status.FAIL, 'Alpaca returned 401 — rotate keys'
    if not r.ok:
        return Status.FAIL, f'/v2/account → {r.status_code}'
    equity = float(r.json().get('equity') or 0)
    if equity <= 0:
        return Status.WARN, f'account equity = {equity}'
    return Status.PASS, f'equity ${equity:,.2f}'


@check(name='alpaca_get_positions_works', tags=['broker'], requires=['broker'])
def _alpaca_get_positions():
    """get_positions() round-trip — regression for 2026-05-13 KeyError 'mark' /
    field-name mismatch between alpaca_trader output and circuit-breaker input."""
    import sys
    sys.path.insert(0, '/root/openclaw/src')
    from execution.alpaca_trader import _alpaca_session, get_positions
    sess = _alpaca_session()
    positions = get_positions(sess)
    if positions is None:
        return Status.FAIL, 'get_positions returned None'
    # Shape check: dict with expected keys
    if positions:
        first = positions[0]
        required = {'symbol', 'qty', 'avg_entry_price', 'current_price'}
        missing = required - set(first.keys())
        if missing:
            return Status.FAIL, f'position dict missing keys: {missing}'
    return Status.PASS, f'{len(positions)} open positions'


@check(name='live_positions_have_stops', tags=['broker'], requires=['broker'])
def _live_positions_have_stops():
    """Every open equity position must have an active GTC stop covering it.

    Bracket orders submit with tif=day; Alpaca's OCO logic cancels the stop
    leg at EOD when either child terminates. Without a daily reattach step
    every position is naked overnight. Sat 2026-05-29 audit: 26 positions,
    0 active stops, 91% of intended stops historically expired/canceled
    before firing. stop_reattach.py pipeline step + this check are the
    paired prevention.

    Crypto positions excluded — they have a separate resting-stop path in
    alpaca_executor._submit_crypto_stop with its own coverage semantics."""
    import json
    import subprocess
    cli = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')
    # Positions
    pr = subprocess.run([cli, 'position', 'list'], capture_output=True, text=True, timeout=15)
    if pr.returncode != 0:
        return Status.FAIL, f'position list failed: {pr.stderr.strip()[:200]}'
    try:
        positions = json.loads(pr.stdout) or []
    except json.JSONDecodeError:
        return Status.FAIL, 'position list stdout not JSON'
    equity_pos = [p for p in positions if (p.get('asset_class') or '') == 'us_equity']
    if not equity_pos:
        return Status.SKIP, 'no equity positions'
    # Open stops. --nested is load-bearing: a flat order list HIDES the stop
    # legs of OCO/bracket orders, so an OCO-protected book reads as 100%
    # naked (2026-07-21 maintenance false-alarmed "12/13 naked" on a book
    # where 11/12 carried full OCOs; same trap as the 07-16 "3 stops vs 9"
    # dashboard incident).
    # --limit is load-bearing too: `order list` returns 50 rows by DEFAULT and
    # caps --limit at 500 (2026-08-12: a 183-order book truncated to 50 rows
    # read ~140 OCO-protected positions as naked). Keyset-paginate with
    # --after-order-id in --direction asc until a short page, mirroring
    # stop_reattach._fetch_open_orders, so the check stays honest on any book.
    orders, cursor = [], None
    for _page in range(40):
        args = [cli, 'order', 'list', '--status', 'open', '--nested',
                '--limit', '500', '--direction', 'asc', '--quiet']
        if cursor:
            args += ['--after-order-id', cursor]
        or_ = subprocess.run(args, capture_output=True, text=True, timeout=15)
        if or_.returncode != 0:
            return Status.FAIL, f'order list failed: {or_.stderr.strip()[:200]}'
        try:
            page = json.loads(or_.stdout) or []
        except json.JSONDecodeError:
            return Status.FAIL, 'order list stdout not JSON'
        orders.extend(page)
        if len(page) < 500:
            break
        cursor = page[-1].get('id')
        if not cursor:
            break
    _DEAD = {'canceled', 'filled', 'expired', 'rejected', 'replaced',
             'done_for_day', 'stopped'}

    def _count_stop(o, parent_qty, covered):
        t = (o.get('type') or o.get('order_type') or '').lower()
        if t not in ('stop', 'stop_limit', 'trailing_stop') or not o.get('stop_price'):
            return
        if (o.get('status') or '').lower() in _DEAD:
            return
        sym = o.get('symbol')
        try:
            q = float(o.get('qty') or parent_qty or 0)
        except (TypeError, ValueError):
            q = 0.0
        if sym and q > 0:
            covered[sym] = covered.get(sym, 0.0) + q

    covered: dict[str, float] = {}
    for o in orders:
        _count_stop(o, None, covered)
        for leg in (o.get('legs') or []):
            if not leg.get('symbol'):
                leg = {**leg, 'symbol': o.get('symbol')}
            _count_stop(leg, o.get('qty'), covered)
    naked = []
    for p in equity_pos:
        sym = p.get('symbol') or ''
        try:
            qty = abs(float(p.get('qty') or 0))
        except (TypeError, ValueError):
            qty = 0.0
        if qty > 0 and covered.get(sym, 0) + 0.01 < qty:
            naked.append(f'{sym} ({int(qty)})')
    if naked:
        return Status.FAIL, (f'{len(naked)}/{len(equity_pos)} equity positions without a covering stop: '
                             + ', '.join(naked[:8]))
    return Status.PASS, f'all {len(equity_pos)} equity positions covered by active stops'


# Backlog severity thresholds. The 2026-06-17/06-18 incident left 15-17 stuck
# rows PER DAY (17/19 then 15/17), so a single day's worth of un-reconciled
# ext-hours fills already trips FAIL — that's the signal we want surfaced loud.
# A handful (1..9) is a WARN: small enough to be a one-off lag that the next
# `--sweep-stale` pass clears, but still worth flagging.
_BACKLOG_WARN_AT = 1   # >=1 stuck prior-day row → WARN
_BACKLOG_FAIL_AT = 10  # >=10 → FAIL (a whole day's ext-hours fills went silent)


@check(name='unreconciled_submissions_backlog', tags=['broker', 'pipeline'], requires=['db'])
def _unreconciled_submissions_backlog():
    """alpaca_submissions rows from PRIOR trading days that were sent to the
    broker but never got reconciled (broker_status IS NULL).

    The reconcile step only looks at the CURRENT cycle's run_date and polls
    in-flight orders for ~30s. Ext-hours fills lag Alpaca's activity API by
    more than that, so those rows are left broker_status=NULL and are NEVER
    re-examined on a later day — the broker actually FILLED them, but the DB
    never learns. 2026-06-17 had 17/19 and 2026-06-18 had 15/17 rows stuck
    this way, silently. This check is the regression probe; the fix is
    `alpaca_reconcile.py --sweep-stale`.

    Scope (must match the sweep + the alpaca_submissions_unreconciled_idx
    partial index): broker_status IS NULL AND alpaca_order_id IS NOT NULL.
    Rows with alpaca_order_id IS NULL are submit errors that were never sent
    to the broker — nothing can reconcile them, so counting them would make
    this check FAIL forever on rows nothing can fix.

    WARN at >=1, FAIL at >=10, PASS at 0. Detail names the count + oldest
    affected run_date."""
    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        return Status.SKIP, 'POSTGRES_URI not set'
    try:
        with psycopg2.connect(uri, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*), MIN(run_date)
                      FROM alpaca_submissions
                     WHERE broker_status IS NULL
                       AND alpaca_order_id IS NOT NULL
                       AND run_date < CURRENT_DATE
                    """
                )
                count, oldest = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        return Status.FAIL, f'backlog query failed: {type(exc).__name__}: {exc}'[:200]

    count = int(count or 0)
    if count == 0:
        return Status.PASS, 'no unreconciled prior-day submissions (0)'
    sev = Status.FAIL if count >= _BACKLOG_FAIL_AT else Status.WARN
    return sev, (f'{count} prior-day submission(s) stuck broker_status=NULL '
                 f'(oldest run_date={oldest}); run alpaca_reconcile.py --sweep-stale')


@check(name='alpaca_clock_reachable', tags=['broker'], requires=['broker'])
def _alpaca_clock_reachable():
    """/v2/clock works — sanity-check the auth headers + base URL."""
    sess = _session()
    r = sess.get(f'{sess._base}/v2/clock', timeout=10)
    if not r.ok:
        return Status.FAIL, f'/v2/clock → {r.status_code}'
    is_open = r.json().get('is_open')
    return Status.PASS, f'is_open={is_open}'
