"""
S15 — Opportunistic Insider Short
SHORT-only standalone strategy. Filters insider sell-clusters through an
opportunistic-vs-routine classifier (Cohen-Malloy-Pomorski 2012).
Independent of S12_insider — separate file, ID, params, cooldown.

Spec: docs/superpowers/specs/2026-05-28-s15-insider-opportunistic-short-design.md
"""

from __future__ import annotations
from typing import Iterable
import pandas as pd


# ── Transaction-type filter ─────────────────────────────────────────────────

_QUALIFYING_SALE_TYPES = {'S-SALE', 'S'}


def qualifying_sales(txns: Iterable[dict]) -> list[dict]:
    """Keep only S-Sale and S transaction types (open-market sales).

    Drops M-Exempt (option exercise), F-InKind (tax withholding), G-Gift,
    D (return to issuer), A-Award (RSU grant), J-Other, P-Purchase (buy).
    These are mechanical or non-informational and would dilute the signal.
    """
    out = []
    for t in txns:
        ttype = t.get('transactionType')
        if ttype is None:
            continue
        if str(ttype).upper() in _QUALIFYING_SALE_TYPES:
            out.append(t)
    return out


# ── Stage 2: opportunistic-vs-routine classifier ────────────────────────────

def classify_insider(history: list[dict], as_of: pd.Timestamp) -> str:
    """Classify an insider as 'opportunistic' or 'routine'.

    Window: t-15 to t-3 months from as_of (12-month window with 3-month
    look-ahead gap). Buckets qualifying sales by calendar quarter.

    - >=3 distinct quarters with sales → 'routine'
    - <=2 distinct quarters in window → 'opportunistic'
    - 0 qualifying sales in window → 'opportunistic' (new insider default)
    """
    sales = qualifying_sales(history or [])
    if not sales:
        return 'opportunistic'

    window_start = as_of - pd.DateOffset(months=15)
    window_end = as_of - pd.DateOffset(months=3)

    quarters = set()
    for t in sales:
        try:
            txn_date = pd.to_datetime(t.get('transactionDate'))
        except (TypeError, ValueError):
            continue
        if txn_date < window_start or txn_date > window_end:
            continue
        quarters.add((txn_date.year, (txn_date.month - 1) // 3 + 1))

    if len(quarters) >= 3:
        return 'routine'
    return 'opportunistic'


# ── Stage 1: cluster gate ───────────────────────────────────────────────────

def cluster_gate(
    sales: list[dict],
    buys: list[dict],
    min_insiders: int = 3,
    min_net_value: float = 5_000_000,
) -> tuple[bool, dict]:
    """Stage 1: does this cluster of sales meet the threshold gate?

    Conditions (all required):
      - distinct insiders in `sales` >= min_insiders
      - sum(value) over `sales` >= min_net_value
      - `buys` is empty (require_zero_buys hard-coded True; aligns with spec)

    Returns (passes, metadata) where metadata always includes the computed
    stats so caller can log/rank even when ok=False.
    """
    distinct_insiders = len({
        (s.get('reportingName') or '').strip() for s in sales
        if (s.get('reportingName') or '').strip()
    })
    net_sell_value = sum(float(s.get('value') or 0.0) for s in sales)
    buy_count = len(buys)

    meta = {
        'distinct_insiders': distinct_insiders,
        'net_sell_value':    net_sell_value,
        'sell_count':        len(sales),
        'buy_count':         buy_count,
    }

    if buy_count > 0:
        return False, meta
    if distinct_insiders < int(min_insiders):
        return False, meta
    if net_sell_value < float(min_net_value):
        return False, meta
    return True, meta
