"""
S15 — Opportunistic Insider Short
SHORT-only standalone strategy. Filters insider sell-clusters through an
opportunistic-vs-routine classifier (Cohen-Malloy-Pomorski 2012).
Independent of S12_insider — separate file, ID, params, cooldown.

Spec: docs/superpowers/specs/2026-05-28-s15-insider-opportunistic-short-design.md
"""

from __future__ import annotations
import pandas as pd


# ── Stage 2: opportunistic-vs-routine classifier ────────────────────────────

def classify_insider(history: list[dict], as_of: pd.Timestamp) -> str:
    """Classify an insider as 'opportunistic' or 'routine'.

    Window: t-15 to t-3 months from as_of (12-month window with 3-month
    look-ahead gap). Buckets qualifying sales by calendar quarter.

    - >=3 distinct quarters with sales → 'routine'
    - <=2 distinct quarters in window → 'opportunistic'
    - 0 qualifying sales in window → 'opportunistic' (new insider default)

    Only counts S-Sale / S transaction types (see _qualifying_sales rules).
    Other txn types (M-Exempt, F-InKind, etc.) are mechanical, not informational.
    """
    if not history:
        return 'opportunistic'

    window_start = as_of - pd.DateOffset(months=15)
    window_end = as_of - pd.DateOffset(months=3)

    quarters = set()
    for t in history:
        ttype = (t.get('transactionType') or '').upper()
        if ttype not in ('S-SALE', 'S'):
            continue
        try:
            txn_date = pd.to_datetime(t.get('transactionDate'))
        except (TypeError, ValueError):
            continue
        if txn_date < window_start or txn_date > window_end:
            continue
        # Calendar quarter bucket: (year, quarter_num 1-4)
        quarters.add((txn_date.year, (txn_date.month - 1) // 3 + 1))

    if len(quarters) >= 3:
        return 'routine'
    return 'opportunistic'
