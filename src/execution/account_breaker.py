#!/usr/bin/env python3
"""Account-level risk breaker (spec 2026-09-12 §3 C1, operator ruling R2).

Two rules, evaluated every 5 minutes during RTH by the SAME cron that runs
position_circuit_breaker.py (src/engine/cron-schedule.js) — no new thread:

    drawdown    alpha_nav / rolling_peak(alpha_nav) - 1   <= -0.10
    daily loss  (equity - opening_equity) / opening_equity <= -0.03

alpha_nav = broker equity - the market value of every BENCHMARK-sleeve ticker,
so an SPY-sleeve drawdown never trips the alpha drawdown rule; the daily rule
deliberately measures the WHOLE book. The asymmetry is ruling R2 as written —
do not harmonize the two.

ALL FOUR REGIMES. Nothing in this module branches on the trading regime.

Flag: OPENCLAW_ACCOUNT_BREAKER=1 arms the ACTION. Unset = SHADOW — the state
row and the `[account_breaker] shadow ...` line are still written every tick,
no order is submitted, and every circuit_breaker_fires row carries
close_result_json.dry_run=true so the sizer's risk-exit cooldown
(_load_recent_risk_exits) ignores it.

Re-arm is operator-only: OPENCLAW_ACCOUNT_BREAKER_REARM=<breached_at iso> in
.env clears exactly that halt (a stale token cannot clear a later breach) and
resets the rolling peak to the current alpha NAV.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

logger = logging.getLogger(__name__)

ARM_ENV = 'OPENCLAW_ACCOUNT_BREAKER'
REARM_ENV = 'OPENCLAW_ACCOUNT_BREAKER_REARM'
NAV_OHLC_PATH_ENV = 'OPENCLAW_PNL_OHLC_PATH'
DEFAULT_NAV_OHLC_PATH = ROOT / 'logs' / 'pnl_daily_ohlc.json'

DD_LIMIT = -0.10        # alpha-sleeve drawdown from the rolling peak
DAILY_LIMIT = -0.03     # total-equity loss vs the session's opening equity
BENCH_LOOKBACK_DAYS = 30

# Float-equality tolerance for the boundary comparisons below: a ratio that is
# mathematically exactly -0.10 (e.g. 90_000 / 100_000 - 1) lands on
# -0.09999999999999998 in IEEE-754 double precision, which is NOT <= -0.10
# and would silently fail to trip the rule at its own documented threshold.
# This epsilon is far smaller than any real gap between "at the limit" and
# "inside the limit" (the latter differs by >= tens of dollars of NAV in
# practice), so it never masks a genuine non-breach.
_EPS = 1e-9


def armed() -> bool:
    """True iff the operator has armed the ACTION. Read at call time so a
    .env edit takes effect on the next 5-minute tick without a restart."""
    return os.environ.get(ARM_ENV) == '1'


def nav_ohlc_path() -> Path:
    return Path(os.environ.get(NAV_OHLC_PATH_ENV) or DEFAULT_NAV_OHLC_PATH)


def alpha_nav(equity: float, positions: dict, bench_tickers) -> tuple[float, float]:
    """(alpha_nav, benchmark_market_value).

    `positions` is regime_liquidator._load_broker_positions() shape:
    {symbol: {'qty': float, 'side': str, 'market_value': str}}. A short
    benchmark leg has a negative market value and correctly RAISES alpha NAV.
    Unparseable market values are skipped (logged by the caller's line)."""
    bench_tickers = set(bench_tickers or ())
    bench_mv = 0.0
    for sym, p in (positions or {}).items():
        if sym not in bench_tickers:
            continue
        try:
            bench_mv += float((p or {}).get('market_value') or 0.0)
        except (TypeError, ValueError):
            continue
    return float(equity) - bench_mv, bench_mv


def evaluate(alpha: float, peak, equity: float, opening_equity) -> dict:
    """Pure rule evaluation. Returns
    {'peak', 'dd', 'daily', 'rule', 'breach'}.

    `peak` None (first ever tick) seeds from the current alpha NAV, so the
    breaker can never fire on its own first observation. `opening_equity`
    None/0 disables the daily rule for that tick (reported as daily=None)
    rather than inventing a denominator."""
    alpha = float(alpha)
    peak = alpha if peak is None else max(float(peak), alpha)
    dd = (alpha / peak - 1.0) if peak > 0 else 0.0

    daily = None
    if opening_equity not in (None, 0) and float(opening_equity) > 0:
        daily = float(equity) / float(opening_equity) - 1.0

    rules = []
    if dd <= DD_LIMIT + _EPS:
        rules.append('drawdown')
    if daily is not None and daily <= DAILY_LIMIT + _EPS:
        rules.append('daily_loss')

    return {'peak': peak, 'dd': dd, 'daily': daily,
            'rule': '+'.join(rules) if rules else 'none',
            'breach': bool(rules)}
