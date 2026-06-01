"""Pipeline-tagged check: SP-5.1a option-routing sanity.

With OPENCLAW_OPTION_EXEC=1 (and OPENCLAW_INSTRUMENT_CLASS_ROUTING=1
upstream), route a single-leg option order through _route_option_order and
confirm it produces a well-shaped result dict:
    - instrument_class='option'
    - status in {'submitted', 'skipped'}
    - non-empty client_order_id

SENTINEL, NOT A REAL ORDER: the probe passes `limit_price_override=1.00` on a
long-call buy. $1.00 is a valid tick and sits far below any ATM SPY call
(~$10+), so the order RESTS at the broker and never fills; we cancel it
immediately and cross-check that no position was opened. This exercises the
full routing path (resolve expiry/strike/OCC + live quote + broker submit)
without the marketable fill the old probe placed (which, after the
latest-quotes quote fix, would buy a real ~$1k call and orphan it).

Gate OFF -> SKIP with detail 'OPENCLAW_OPTION_EXEC=0'. requires=['fs'] so the
runner doesn't pre-skip on missing broker/db creds (we need the body to run to
emit the gate-detail SKIP that operators grep for).
"""
from __future__ import annotations

import os
import time

from ..registry import check
from ..types import Status


@check(name='option_routing', tags=['pipeline'], requires=['fs'])
def _option_routing():
    if os.environ.get('OPENCLAW_OPTION_EXEC') != '1':
        return Status.SKIP, 'OPENCLAW_OPTION_EXEC=0'

    # Imports are deferred so the gate-off SKIP path doesn't pay for module
    # load (and so module-discovery doesn't transitively pull execution code
    # on import).
    from execution.alpaca_executor import (
        _route_option_order, _run_alpaca_cli, _options_current_qty,
    )
    from strategies.base import OptionSpec

    spec = OptionSpec(
        underlying='SPY', right='call', strike_rule='atm',
        target_delta=0.30, dte_target=30, structure='single',
    )
    # Non-marketable sentinel: long call buy at $1.00 (<< ATM SPY call) -> rests.
    order = {
        'ticker': 'SPY',
        'strategy_id': 'sys_check',
        'direction': 'long',
        'instrument_class': 'option',
        'contracts': 1.0,
        'notional_usd': 100,
        'option_spec': spec,
        'limit_price_override': 1.00,
    }
    coid = f'syscheck-opt-{int(time.time())}'
    res = _route_option_order(order, equity=100000, coid=coid)

    if res is None:
        return Status.FAIL, 'helper returned None for option order'
    if res.get('instrument_class') != 'option':
        return Status.FAIL, f'instrument_class={res.get("instrument_class")!r}'
    if res.get('status') not in ('submitted', 'skipped'):
        return Status.FAIL, f'unexpected status={res.get("status")!r}'
    if not res.get('client_order_id'):
        return Status.FAIL, 'client_order_id missing or empty'

    detail = f'status={res.get("status")} reason={res.get("reason", "-")}'

    # Self-clean: cancel the resting sentinel + confirm flat (never trust the
    # submit ack). A skipped status (e.g. market closed -> RTH gate) leaves
    # nothing to cancel.
    order_id = res.get('order_id')
    occ = res.get('ticker')
    if res.get('status') == 'submitted' and order_id:
        _run_alpaca_cli(['order', 'cancel', '--order-id', order_id])
        residual = _options_current_qty(occ) if occ else 0.0
        if residual != 0:
            # Sentinel unexpectedly FILLED -> the routing placed a marketable
            # order. Flatten and FAIL loudly; this is the footgun the refactor
            # exists to prevent.
            _run_alpaca_cli(['position', 'close', '--symbol-or-asset-id', occ])
            return Status.FAIL, (f'sentinel FILLED (qty={residual} {occ}) — '
                                 f'flattened; routing placed a marketable order')
        detail += f' sentinel=canceled flat={occ}'

    return Status.PASS, detail
