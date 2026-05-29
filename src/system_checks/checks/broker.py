"""Broker checks — Alpaca session, account, positions sanity."""
from __future__ import annotations

import os

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
    # Open stops
    or_ = subprocess.run([cli, 'order', 'list', '--status', 'open'],
                         capture_output=True, text=True, timeout=15)
    if or_.returncode != 0:
        return Status.FAIL, f'order list failed: {or_.stderr.strip()[:200]}'
    try:
        orders = json.loads(or_.stdout) or []
    except json.JSONDecodeError:
        return Status.FAIL, 'order list stdout not JSON'
    covered: dict[str, float] = {}
    for o in orders:
        t = (o.get('type') or o.get('order_type') or '').lower()
        if t in ('stop', 'stop_limit') and o.get('stop_price'):
            sym = o.get('symbol')
            try:
                q = float(o.get('qty') or 0)
            except (TypeError, ValueError):
                q = 0.0
            if sym and q > 0:
                covered[sym] = covered.get(sym, 0.0) + q
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


@check(name='alpaca_clock_reachable', tags=['broker'], requires=['broker'])
def _alpaca_clock_reachable():
    """/v2/clock works — sanity-check the auth headers + base URL."""
    sess = _session()
    r = sess.get(f'{sess._base}/v2/clock', timeout=10)
    if not r.ok:
        return Status.FAIL, f'/v2/clock → {r.status_code}'
    is_open = r.json().get('is_open')
    return Status.PASS, f'is_open={is_open}'
