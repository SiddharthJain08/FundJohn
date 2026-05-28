"""
S15 — Opportunistic Insider Short
SHORT-only standalone strategy. Filters insider sell-clusters through an
opportunistic-vs-routine classifier (Cohen-Malloy-Pomorski 2012).
Independent of S12_insider — separate file, ID, params, cooldown.

Spec: docs/superpowers/specs/2026-05-28-s15-insider-opportunistic-short-design.md
"""

from __future__ import annotations
from typing import Iterable
import math
import os
import pandas as pd
from ..base import BaseStrategy, Signal


# ── Transaction-type filter ─────────────────────────────────────────────────

_QUALIFYING_SALE_TYPES = {'S-SALE', 'S'}
_QUALIFYING_BUY_TYPES = {'P-PURCHASE', 'P'}


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
    require_both: bool = False,
) -> tuple[bool, dict]:
    """Stage 3: does this cluster carry conviction signal?

    Default (require_both=False) passes if ANY of:
      (1) Personal stake: any single seller sold >= min_personal_stake_pct of
          their prior personal holdings.
      (2) C-suite: any seller's role contains CEO/CFO/COO/Chair/Chairman.

    When require_both=True (v9/v10), BOTH (1) AND (2) must hold.

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
    stake_pass = top_pct >= float(min_personal_stake_pct)

    meta = {
        'top_seller_pct_of_holdings': top_pct,
        'c_suite_present':            c_suite_present,
    }

    if require_both:
        return (stake_pass and c_suite_present), meta
    if stake_pass or c_suite_present:
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
    # Active in HIGH_VOL (unlike S12 BUY) because the SHORT thesis on
    # opportunistic insider sales is regime-agnostic per Cohen-Malloy-Pomorski
    # 2012 — the signal's informational content does not decay in higher-vol
    # regimes. CRISIS is excluded to avoid squeeze risk during panics.
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    def default_parameters(self) -> dict:
        return {
            # Stage 1 (cluster gate) — v6 revert to v4 local optimum (4 / $10M).
            # v5 push to 5 / $20M overshot: Sharpe collapsed +0.28 → -1.39.
            'min_insiders':              4,
            'min_net_sell_value':        10_000_000,
            # Stage 2 (opportunistic classifier)
            'min_opportunistic_count':   3,  # v9: 2→3 (tighten Stage 2)
            # Stage 3 (conviction filter)
            'min_personal_stake_pct':    0.10,
            'stage3_require_both':       True,  # v9: False→True (AND logic)
            # Position management — v6 cuts concurrent cap 20 → 8 so the
            # ranking score (opp_count × log10(net_sell_value)) actually bites
            # on high-fire days. v4 avg 5.8 fires/day, cap=20 rarely bound.
            'base_size_pct':             0.015,
            'max_concurrent_positions':  20,  # v9: revert cap to baseline (isolate Stage 3)
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

        if prices is None or prices.empty:
            return []

        p = self.parameters
        scale = self.position_scale(regime_state)

        # Reference date: last bar in prices
        ref_date = prices.index[-1]
        if isinstance(ref_date, str):
            ref_date = pd.to_datetime(ref_date)

        # Stage 1 cluster window
        short_lookback = int(p['short_lookback_days'])
        stage1_cutoff = ref_date - pd.Timedelta(days=short_lookback)

        # Aux data slices
        short_txns_all = (aux_data or {}).get('insider_txns', {}) or {}
        long_history_all = (aux_data or {}).get('insider_history_long', {}) or {}

        # Cooldown
        recent_stops = (aux_data or {}).get('recent_stop_outs') or {}
        cooldown_days = int(p['cooldown_after_stop_days'])

        # Disable Stage 2 only via ablation env var (Task 9 introduces this).
        # Read here so Task 9 doesn't need to re-touch this function.
        ablate_classifier = (
            os.environ.get('OPENCLAW_S15_DISABLE_OPPORTUNISTIC_CLASSIFIER') == '1'
        )

        candidates: list = []

        for ticker in universe:
            if ticker not in prices.columns:
                continue

            # Cooldown gate
            last_stop = recent_stops.get(ticker)
            if last_stop is not None:
                try:
                    last_stop_ts = pd.to_datetime(last_stop)
                    if (ref_date - last_stop_ts).days < cooldown_days:
                        continue
                except (TypeError, ValueError):
                    pass

            # Fast-fail: no insider data
            ticker_txns = short_txns_all.get(ticker, [])
            if not ticker_txns:
                continue

            # Filter Stage 1 window
            window_txns = []
            for t in ticker_txns:
                td_raw = t.get('transactionDate') or t.get('transaction_date')
                if td_raw is None:
                    continue
                try:
                    td = pd.to_datetime(td_raw)
                except (TypeError, ValueError):
                    continue
                if td < stage1_cutoff or td > ref_date:
                    continue
                window_txns.append(t)

            # Split into sales / buys (qualifying types)
            sales = qualifying_sales(window_txns)
            buys = [
                t for t in window_txns
                if (t.get('transactionType') or '').upper() in _QUALIFYING_BUY_TYPES
            ]

            # Stage 1
            ok1, meta1 = cluster_gate(
                sales, buys,
                min_insiders=p['min_insiders'],
                min_net_value=p['min_net_sell_value'],
            )
            if not ok1:
                continue

            # Stage 2: opportunistic classifier
            seller_names = {(s.get('reportingName') or '').strip()
                            for s in sales
                            if (s.get('reportingName') or '').strip()}
            seller_history = long_history_all.get(ticker, [])

            opp_count = 0
            routine_count = 0
            for name in seller_names:
                this_seller_history = [
                    h for h in seller_history
                    if (h.get('reportingName') or '').strip() == name
                ]
                kind = classify_insider(this_seller_history, ref_date)
                if kind == 'opportunistic':
                    opp_count += 1
                else:
                    routine_count += 1

            if not ablate_classifier:
                if opp_count < int(p['min_opportunistic_count']):
                    continue

            # Stage 3
            ok3, meta3 = conviction_filter(
                sales,
                min_personal_stake_pct=p['min_personal_stake_pct'],
                require_both=bool(p.get('stage3_require_both', False)),
            )
            if not ok3:
                continue

            # Build the signal
            ts = prices[ticker].dropna()
            if len(ts) < self.min_lookback:
                continue
            current_price = float(ts.iloc[-1])
            if current_price <= 0:
                continue

            stops = self.compute_stops_and_targets(
                ts, 'SHORT', current_price, regime_state=regime_state
            )
            wide_stop_floor = current_price * (1.0 + float(p['wide_stop_pct']))
            stop_value = max(stops.get('stop', wide_stop_floor), wide_stop_floor)

            candidates.append(Signal(
                ticker            = ticker,
                direction         = 'SHORT',
                entry_price       = current_price,
                stop_loss         = stop_value,
                target_1          = None,
                target_2          = None,
                target_3          = None,
                position_size_pct = round(float(p['base_size_pct']) * scale, 4),
                confidence        = 'HIGH',
                signal_params     = {
                    'distinct_insiders':           meta1['distinct_insiders'],
                    'opportunistic_count':         opp_count,
                    'routine_count':               routine_count,
                    'net_sell_value':              round(meta1['net_sell_value'], 0),
                    'top_seller_pct_of_holdings':  round(meta3['top_seller_pct_of_holdings'], 4),
                    'c_suite_present':             bool(meta3['c_suite_present']),
                    'lookback_days':               short_lookback,
                    'cluster_kind':                'SELL_OPPORTUNISTIC',
                },
            ))

        # Rank by (opportunistic_count * log10(max(net_sell_value, 1))), desc
        def _score(sig):
            v = max(float(sig.signal_params.get('net_sell_value') or 1.0), 1.0)
            opp = int(sig.signal_params.get('opportunistic_count') or 0)
            return opp * math.log10(v)

        candidates.sort(key=_score, reverse=True)
        return candidates[:int(p['max_concurrent_positions'])]
