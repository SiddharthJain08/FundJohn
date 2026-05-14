"""cadence.py — strategy cadence helpers shared by sizer + weights engine.

Maps the strategy class attribute `signal_frequency` to trading-day count.
This is the single source of truth: any new frequency string must be added
to CADENCE_DAYS here so both the weights engine and the sizer agree.

Conventions match `src/execution/signal_cadence_gate.py` (the existing
gating logic) — trading days, not calendar days.
"""
from __future__ import annotations

CADENCE_DAYS: dict[str, int] = {
    'daily':   1,
    'weekly':  5,
    'monthly': 21,
}


def cadence_days(signal_frequency: str | None) -> int:
    """Return cadence in trading days. Defaults to 1 (daily) on unknown."""
    if signal_frequency is None:
        return 1
    key = str(signal_frequency).strip().lower()
    return CADENCE_DAYS.get(key, 1)
