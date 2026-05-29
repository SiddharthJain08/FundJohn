"""Pipeline-tagged check: SP-5.1a option-routing sanity.

With OPENCLAW_OPTION_EXEC=1 (and OPENCLAW_INSTRUMENT_CLASS_ROUTING=1
upstream), synthesize a single-leg option order and confirm
_route_option_order produces a well-shaped result dict:
    - instrument_class='option'
    - status ∈ {'submitted', 'skipped'}
    - non-empty client_order_id

Gate OFF → SKIP with detail 'OPENCLAW_OPTION_EXEC=0'. requires=['fs'] so
the runner doesn't pre-skip on missing broker/db creds (we need the body
to run to emit the gate-detail SKIP that operators grep for).

NOTE: when the gate is ON this probe calls _route_option_order, which can
submit a live option order against the connected Alpaca account. Heavier
than a read-only check — exercised only under Task 13's authorized live
smoke test.
"""
from __future__ import annotations

import os

from ..registry import check
from ..types import Status


@check(name='option_routing', tags=['pipeline'], requires=['fs'])
def _option_routing():
    if os.environ.get('OPENCLAW_OPTION_EXEC') != '1':
        return Status.SKIP, 'OPENCLAW_OPTION_EXEC=0'

    # Imports are deferred so the gate-off SKIP path doesn't pay for module
    # load (and so module-discovery doesn't transitively pull execution code
    # on import).
    from execution.alpaca_executor import _route_option_order
    from strategies.base import OptionSpec

    spec = OptionSpec(
        underlying='SPY', right='call', strike_rule='atm',
        target_delta=0.30, dte_target=30, structure='single',
    )
    order = {
        'ticker': 'SPY',
        'strategy_id': 'sys_check',
        'direction': 'long',
        'instrument_class': 'option',
        'contracts': 1.0,
        'notional_usd': 2000,
        'option_spec': spec,
    }
    res = _route_option_order(order, equity=100000, coid='syscheck-1')
    if res is None:
        return Status.FAIL, 'helper returned None for option order'
    if res.get('instrument_class') != 'option':
        return Status.FAIL, f'instrument_class={res.get("instrument_class")!r}'
    if res.get('status') not in ('submitted', 'skipped'):
        return Status.FAIL, f'unexpected status={res.get("status")!r}'
    if not res.get('client_order_id'):
        return Status.FAIL, 'client_order_id missing or empty'
    # Plan used 'OK'; framework canonical PASS state is Status.PASS.
    return Status.PASS, f'status={res.get("status")} reason={res.get("reason", "-")}'
