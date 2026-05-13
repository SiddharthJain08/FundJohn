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


@check(name='alpaca_clock_reachable', tags=['broker'], requires=['broker'])
def _alpaca_clock_reachable():
    """/v2/clock works — sanity-check the auth headers + base URL."""
    sess = _session()
    r = sess.get(f'{sess._base}/v2/clock', timeout=10)
    if not r.ok:
        return Status.FAIL, f'/v2/clock → {r.status_code}'
    is_open = r.json().get('is_open')
    return Status.PASS, f'is_open={is_open}'
