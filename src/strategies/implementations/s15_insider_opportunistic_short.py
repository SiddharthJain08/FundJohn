"""
S15 — Opportunistic Insider Short
SHORT-only standalone strategy. Filters insider sell-clusters through an
opportunistic-vs-routine classifier (Cohen-Malloy-Pomorski 2012).
Independent of S12_insider — separate file, ID, params, cooldown.

Spec: docs/superpowers/specs/2026-05-28-s15-insider-opportunistic-short-design.md
"""

from __future__ import annotations
from typing import Iterable
import os
import pandas as pd
from ..base import BaseStrategy, Signal


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


# ── Stage 3: conviction filter ──────────────────────────────────────────────

_C_SUITE_KEYWORDS = ('CEO', 'CFO', 'COO', 'CHAIR', 'CHAIRMAN',
                     'CHIEF EXECUTIVE', 'CHIEF FINANCIAL', 'CHIEF OPERATING')


def _is_c_suite(role: str | None) -> bool:
    if not role:
        return False
    upper = role.upper()
    return any(kw in upper for kw in _C_SUITE_KEYWORDS)


def _personal_stake_pct(sales_by_name: dict, name: str) -> float | None:
    """Compute (shares_sold / prior_holdings) for one insider's sales.

    prior_holdings = max(sharesOwnedAfter across this insider's txns) +
                     sum(shares sold across this insider's txns)
    Returns None if no usable sharesOwnedAfter.
    """
    seller_txns = sales_by_name.get(name, [])
    shares_sold_total = 0.0
    max_after = None
    for t in seller_txns:
        shares = float(t.get('shares') or 0.0)
        shares_sold_total += shares
        after = t.get('sharesOwnedAfter')
        if after is not None:
            after_f = float(after)
            if max_after is None or after_f > max_after:
                max_after = after_f
    if max_after is None:
        return None
    prior_holdings = max_after + shares_sold_total
    if prior_holdings <= 0:
        return None
    return shares_sold_total / prior_holdings


def conviction_filter(
    sales: list[dict],
    min_personal_stake_pct: float = 0.10,
) -> tuple[bool, dict]:
    """Stage 3: does this cluster carry conviction signal?

    Passes if ANY of:
      (1) Personal stake: any single seller sold >= min_personal_stake_pct of
          their prior personal holdings.
      (2) C-suite: any seller's role contains CEO/CFO/COO/Chair/Chairman.

    Note: Company-stake sub-test from the spec (>=1.5% of shares outstanding)
    is skipped — we don't have a reliable shares_outstanding feed in the
    aux loader. Spec authorized graceful skip of this sub-test.

    Returns (passes, metadata).
    """
    sales_by_name: dict = {}
    for s in sales:
        name = (s.get('reportingName') or '').strip()
        if not name:
            continue
        sales_by_name.setdefault(name, []).append(s)

    top_pct = 0.0
    for name in sales_by_name:
        pct = _personal_stake_pct(sales_by_name, name)
        if pct is not None and pct > top_pct:
            top_pct = pct

    c_suite_present = any(_is_c_suite(s.get('role')) for s in sales)

    meta = {
        'top_seller_pct_of_holdings': top_pct,
        'c_suite_present':            c_suite_present,
    }

    if top_pct >= float(min_personal_stake_pct):
        return True, meta
    if c_suite_present:
        return True, meta
    return False, meta


# ── Strategy class ──────────────────────────────────────────────────────────

class OpportunisticInsiderShort(BaseStrategy):
    id                = 'S15_insider_opportunistic_short'
    name              = 'Opportunistic Insider Cluster Short'
    description       = ("SHORT clusters of insider sales where opportunistic "
                         "sellers dominate and conviction filter passes "
                         "(>=10% personal stake or C-suite present).")
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 20
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    def default_parameters(self) -> dict:
        return {
            # Stage 1 (cluster gate)
            'min_insiders':              3,
            'min_net_sell_value':        5_000_000,
            # Stage 2 (opportunistic classifier)
            'min_opportunistic_count':   2,
            # Stage 3 (conviction filter)
            'min_personal_stake_pct':    0.10,
            # Position management
            'base_size_pct':             0.015,
            'max_concurrent_positions':  20,
            'wide_stop_pct':             0.15,
            'cooldown_after_stop_days':  30,
            # Window for Stage 1 (calendar days)
            'short_lookback_days':       30,
        }

    def generate_signals(self, prices, regime, universe, aux_data=None) -> list:
        # Gate check (default OFF)
        if os.environ.get('OPENCLAW_S15_INSIDER_OPPORTUNISTIC') != '1':
            return []

        # Regime check
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        # No data → no signals
        if prices is None or prices.empty:
            return []

        # Stub: full Stage 1/2/3 wiring comes in Task 7
        return []
