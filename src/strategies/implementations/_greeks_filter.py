"""SP-1 defensive layer for Alpaca options chain.

Some contracts return greeks={0,0,0,0,0} from `alpaca data option chain`:
  - 0-DTE / expired contracts (degenerate at expiry)
  - Deep-ITM contracts with zero recent volume (no recent trades to price greeks)

A consuming strategy must filter these out before using greeks for sizing.
If a contract that SHOULD have greeks returns zero, that's a data-source
regression — log + alert via #data-alerts.

Usage:
    from ._greeks_filter import filter_valid_greeks

    chain = [c for c in alpaca_chain if filter_valid_greeks(c) is not None]
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

log = logging.getLogger(__name__)


def _is_zero_greeks(snapshot: Any) -> bool:
    # rho excluded: near-zero on equity options by convention; not used for sizing
    g = snapshot.greeks
    return (
        getattr(g, 'delta', 0) == 0
        and getattr(g, 'gamma', 0) == 0
        and getattr(g, 'theta', 0) == 0
        and getattr(g, 'vega', 0) == 0
    )


def _alert_zero_greeks_anomaly(snapshot: Any) -> None:
    """Discord #data-alerts post + data_provider_health error counter increment.

    Concrete impl wired via src/channels/discord/notifications.js. Stubbed
    here so unit tests don't require Discord. The real wiring is added in
    Task 11 (doctor) — until then this just logs.
    """
    log.warning(
        'sp1.greeks_anomaly contract=%s expiry=%s volume=%s — zero greeks on tradable contract',
        snapshot.contract_symbol, snapshot.expiry, getattr(snapshot, 'volume', '?'),
    )


def filter_valid_greeks(
    snapshot: Any,
    *,
    allow_zero_greeks: bool = False,
    alert: bool = True,
) -> Any | None:
    """Return snapshot if greeks are usable, else None.

    Args:
        snapshot: object with .contract_symbol, .expiry, .volume, .greeks.{delta,...}
        allow_zero_greeks: if True, return snapshot even when greeks are all zero
                           (for 0-DTE-trading strategies that explicitly want them)
        alert: if True, fire anomaly alert when zero greeks appear on a
               contract that should not have them.

    Returns:
        snapshot if usable, None otherwise.
    """
    if allow_zero_greeks:
        return snapshot

    if not _is_zero_greeks(snapshot):
        return snapshot

    # Zero greeks — figure out if expected or anomalous.
    today = date.today()
    expiry = snapshot.expiry
    if isinstance(expiry, str):
        expiry = date.fromisoformat(expiry)
    dte = (expiry - today).days if expiry else None
    volume = getattr(snapshot, 'volume', 0) or 0

    if dte is not None and dte <= 0:
        return None  # 0-DTE / expired: expected
    if volume == 0:
        return None  # No flow: expected

    # Meaningful contract returned zero greeks → anomaly.
    if alert:
        _alert_zero_greeks_anomaly(snapshot)
    return None
