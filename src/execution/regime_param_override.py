"""Apply a per-(strategy, regime) stop/target override to a signal bracket.

Single shared implementation imported by both the backtest (unified_backtest)
and live execution (engine). Gated by OPENCLAW_BACKTEST_COUPLED_RECS: when the
gate is OFF, resolve_override() always returns None and brackets are untouched
(byte-identical to pre-coupling behaviour).

Override semantics: ABSOLUTE-REPLACE. stop_pct / target_pct are flat distances
from entry (fractions). Long: stop=entry*(1-stop_pct), target=entry*(1+target_pct);
mirrored for short.
"""
from __future__ import annotations
import os
from typing import Optional

GATE_ENV = 'OPENCLAW_BACKTEST_COUPLED_RECS'


def gate_on() -> bool:
    return os.environ.get(GATE_ENV) == '1'


def resolve_override(strategy_id: str, regime_state: str, *,
                     injected: Optional[dict] = None) -> Optional[dict]:
    """Return {'stop_pct': x?, 'target_pct': y?} or None.
    - gate OFF → None.
    - injected map ({regime: {stop_pct, target_pct}}) → use it, ignore DB.
    - injected None → read persisted strategy_regime_params via regime_param_resolver.
    """
    if not gate_on():
        return None
    if injected is not None:
        row = injected.get(regime_state)
        return dict(row) if row else None
    from execution import regime_param_resolver as rpr
    stop = rpr.stop_pct_override(strategy_id, regime_state)
    target = rpr.target_pct_override(strategy_id, regime_state)
    if stop is None and target is None:
        return None
    out = {}
    if stop is not None:
        out['stop_pct'] = stop
    if target is not None:
        out['target_pct'] = target
    return out


def apply_override(*, entry_price: float, direction: int,
                   stop_loss: float, target_1: float,
                   override: Optional[dict]) -> tuple[float, float]:
    """Return (stop_loss, target_1) after applying the override (or unchanged
    when override is None / a leg absent). direction: +1 long, -1 short."""
    if not override:
        return stop_loss, target_1
    s, t = stop_loss, target_1
    sp = override.get('stop_pct')
    tp = override.get('target_pct')
    if sp is not None:
        s = entry_price * (1 - sp) if direction > 0 else entry_price * (1 + sp)
    if tp is not None:
        t = entry_price * (1 + tp) if direction > 0 else entry_price * (1 - tp)
    return s, t
