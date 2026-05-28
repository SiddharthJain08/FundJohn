"""strategy_weights.py — per-(strategy, regime) weight engine.

Implements the formulas from
docs/superpowers/specs/2026-05-14-position-sizing-rewrite-design.md.

Per regime R, for each active strategy s with R ∈ eligible_regimes:
    effective_sharpe = (bt_n × bt_sharpe + live_n × live_sharpe)
                       / (bt_n + live_n)
    weight        = oue_adjusted_sharpe(s, R)
    daily_weight  = weight / sqrt(cadence_days(s))

`weight` is the raw OUE-adjusted Sharpe — NOT a normalised fraction.
The sizer's downstream renormalisation (scale = λ·NAV / Σ|ticker_w|)
makes the absolute scale invariant for sizing; keeping weight in
Sharpe units means both the cum-sharpe gate (Σ effective_sharpe ×
direction) and the per-ticker allocation (Σ daily_weight × direction)
speak the same language — Sharpe.

Why sqrt(cadence_days) and not cadence_days? Sharpe scales with sqrt(time)
under iid returns (σ_T = σ_1·sqrt(T)), so a strategy whose effective_sharpe
is computed over T-day holding windows should be down-weighted by sqrt(T)
to convert to a per-cycle-equivalent contribution — dividing by T itself
double-counts the time-window and unfairly penalises multi-day holders.

Σ_s w(s, R) = 1.0 within each regime by construction. Strategies with
effective_sharpe ≤ 0 are excluded from that regime entirely (never
contribute signals while regime is active).

Triggers:
  - weekly_cron      — Sunday 06:00 ET via src/agent/curators/weekly_live_sharpe.js
  - lifecycle_change — strategy state transitions (lifecycle.py hook)
  - manual           — python3 -m execution.strategy_weights --rebuild
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

import psycopg2
import psycopg2.extras

import math

from execution.cadence import cadence_days

logger = logging.getLogger(__name__)


@dataclass
class StrategyWeightRow:
    strategy_id: str
    regime_state: str
    cadence_days: int
    bt_sharpe: float | None
    bt_n: int | None
    live_sharpe: float | None
    live_n: int | None
    effective_sharpe: float
    weight: float
    daily_weight: float
    # OUE health (per-regime closed-trade classification) and the
    # multiplier we applied. Persisted so the dashboard + comprehensive
    # review + operator audit can see exactly what the cron did.
    oue_over: int | None = None
    oue_under: int | None = None
    oue_expected: int | None = None
    oue_multiplier: float | None = None
    oue_adjusted_sharpe: float | None = None


# OUE multiplier gating constants. Operator-approved 2026-05-16; refined
# 2026-05-16 to use time-decayed counts (τ=45d) instead of lifetime so the
# multiplier responds to recent behavior within ~6 weeks. A strategy that
# was disappointing 4 months ago but has delivered the last 6 weeks will
# now see its multiplier rise on the next Sunday cron, not stay pinned to
# a stale history.
OUE_TAU_DAYS = 45.0      # half-life ≈ 31 days; trade @ 90d ago weighted 0.14
OUE_MIN_TOTAL = 30       # time-weighted closes in regime (was 100 lifetime)
OUE_MIN_OUTLIERS = 5     # time-weighted over+under (was 20 lifetime)
OUE_FLOOR = 0.2          # never zero a strategy out (preserves rotation)
OUE_CEIL = 2.0           # never more than 2× boost (prevents over-fitting to luck)

# Lifetime-sign guardrail (operator-set 2026-05-16): cumulative
# overperformers (lifetime O > U with enough sample) can be BOOSTED by
# recent OUE but NEVER discounted below 1.0; cumulative underperformers
# can be DISCOUNTED but never boosted above 1.0. Prevents weights from
# drifting downward on a transient bad streak for an otherwise-proven
# strategy, and prevents temporary luck from inflating a structurally
# underperforming strategy.
OUE_LIFETIME_MIN_TOTAL    = 50   # need this many lifetime closes to trust the sign
OUE_LIFETIME_MIN_OUTLIERS = 10   # and this many lifetime outliers


def _oue_multiplier(over: float, under: float, expected: float,
                    lifetime_over: int = 0, lifetime_under: int = 0,
                    lifetime_expected: int = 0) -> float:
    """OUE-derived multiplier in [OUE_FLOOR, OUE_CEIL].

    Two-stage computation:
      1. Recent bias from TIME-DECAYED counts (over, under, expected) →
         a multiplier that responds to recent behavior.
      2. Lifetime-sign GUARDRAIL: if cumulative O > U with enough
         sample, the multiplier is floored at 1.0 (overperformers can
         only be boosted, never discounted). Mirror case for U > O.

    Returns 1.0 (no adjustment) on insufficient recent sample.
    """
    total = (over or 0.0) + (under or 0.0) + (expected or 0.0)
    outliers = (over or 0.0) + (under or 0.0)
    if total < OUE_MIN_TOTAL or outliers < OUE_MIN_OUTLIERS:
        return 1.0
    bias = ((over or 0.0) - (under or 0.0)) / outliers
    mult = max(OUE_FLOOR, min(OUE_CEIL, 1.0 + bias))

    # Lifetime-sign guardrail. Only applies once cumulative sample is
    # large enough to trust the direction; small-history strategies
    # remain governed purely by their recent-window bias.
    lt_outliers = (lifetime_over or 0) + (lifetime_under or 0)
    lt_total = lt_outliers + (lifetime_expected or 0)
    if lt_total >= OUE_LIFETIME_MIN_TOTAL and lt_outliers >= OUE_LIFETIME_MIN_OUTLIERS:
        if lifetime_over > lifetime_under:
            # Cumulative OVER → never discount below 1.0
            mult = max(1.0, mult)
        elif lifetime_under > lifetime_over:
            # Cumulative UNDER → never boost above 1.0
            mult = min(1.0, mult)
        # Exact tie → no sign constraint

    return mult


def _load_oue_by_strategy_regime(conn, strategy_ids: list[str]) -> dict[tuple[str, str], dict]:
    """Per-(strategy, regime) closed-trade OUE counts with time-decay.

    Returns {(sid, regime): {
       over, under, expected           ← TIME-WEIGHTED (floats)
       lifetime_over, lifetime_under, lifetime_expected ← INTEGER lifetime
    }}. The multiplier uses the time-weighted counts; the lifetime
    counts get persisted to strategy_weights_by_regime.oue_over/under/
    expected so the dashboard's #O/U/E column stays historically
    meaningful.

    Per-regime (not lifetime) because a strategy's calibration varies
    by regime — discount applies only where the bad behavior happened.

    Time-decay weight per trade: exp(-age_days / OUE_TAU_DAYS), where
    age is days since pnl_date. A trade closed today contributes 1.0;
    one from 45 days ago contributes 0.37; one from 90 days ago, 0.14.
    Smoothly aging out old behavior without a hard edge-of-window
    discontinuity.
    """
    if not strategy_ids:
        return {}
    out: dict[tuple[str, str], dict] = {}
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            """
            SELECT es.strategy_id, es.regime_state, es.oue_kind,
                   COUNT(*)::int AS lifetime_n,
                   COALESCE(SUM(
                     EXP(
                       -LEAST(
                          EXTRACT(EPOCH FROM (NOW() - sp.pnl_date::timestamp)) / 86400.0,
                          365.0
                       ) / %s
                     )
                   ), 0.0)::numeric AS weighted_n
              FROM execution_signals es
              JOIN signal_pnl sp ON sp.signal_id = es.id
                                 AND sp.status = 'closed'
                                 AND sp.realized_pnl_pct IS NOT NULL
             WHERE es.strategy_id = ANY(%s)
               AND es.regime_state IS NOT NULL
               AND es.oue_kind IS NOT NULL
             GROUP BY es.strategy_id, es.regime_state, es.oue_kind
            """,
            (OUE_TAU_DAYS, strategy_ids),
        )
        for r in cur:
            key = (r['strategy_id'], r['regime_state'])
            entry = out.setdefault(key, {
                'over': 0.0, 'under': 0.0, 'expected': 0.0,
                'lifetime_over': 0, 'lifetime_under': 0, 'lifetime_expected': 0,
            })
            entry[r['oue_kind']] = float(r['weighted_n'])
            entry['lifetime_' + r['oue_kind']] = int(r['lifetime_n'])
    return out


def _db():
    return psycopg2.connect(os.environ['POSTGRES_URI'])


def _load_active_strategies(conn) -> list[dict]:
    """Read live/monitoring strategies from manifest.json + their per-regime
    eligibility from the strategy_regime_params table (the same source the
    runtime regime_gate reads). Active stack membership = manifest state.
    Per-regime eligibility = `eligible` column (boolean).

    Each output row: strategy_id, eligible_regimes (list), signal_frequency
    (parsed from implementation file), cadence_days.
    """
    manifest = json.loads((ROOT / 'src' / 'strategies' / 'manifest.json').read_text())
    impl_dir = ROOT / 'src' / 'strategies' / 'implementations'

    active_ids = [sid for sid, e in manifest.get('strategies', {}).items()
                  if e.get('state') in ('live', 'monitoring')]
    if not active_ids:
        return []

    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute('''
        SELECT strategy_id, regime_state
        FROM strategy_regime_params
        WHERE eligible = TRUE AND strategy_id = ANY(%s)
    ''', (active_ids,))
    eligible_by_strat: dict[str, list[str]] = {}
    for r in cur:
        eligible_by_strat.setdefault(r['strategy_id'], []).append(r['regime_state'])

    # Live-observed avg_holding_days per strategy. The static signal_frequency
    # declaration is a lower bound — many strategies declare 'daily' but
    # actually hold positions for 3-15 days because stops/targets don't hit
    # within one bar. cadence_days drives both the sizer's active-window
    # lookback AND daily_weight = w / cadence_days; using the static value
    # for a strategy that holds 4 days both over-counts old signals AND
    # over-weights it relative to a true-daily strategy.
    #
    # Resolution: cadence_days = max(static_declared, ceil(live_avg)). The
    # max() keeps the static declaration as a floor (a strategy declared
    # monthly that happens to close fast doesn't get bumped down).
    cur.execute('SELECT strategy_id, avg_holding_days FROM strategy_state WHERE strategy_id = ANY(%s)',
                (active_ids,))
    live_avg_by_strat: dict[str, float] = {}
    for r in cur:
        v = r['avg_holding_days']
        if v is not None:
            try:
                live_avg_by_strat[r['strategy_id']] = float(v)
            except (TypeError, ValueError):
                pass

    out: list[dict] = []
    for sid in active_ids:
        eligible = eligible_by_strat.get(sid, [])
        if not eligible:
            # Strategy is live but has no eligible regime in the params
            # table — out-of-sync; skip + warn.
            logger.warning('strategy_weights: %s is live but has no eligible regimes in strategy_regime_params; skipping', sid)
            continue
        meta = manifest['strategies'][sid].get('metadata', {}) or {}
        impl_path = impl_dir / (meta.get('canonical_file') or '')
        sig_freq = None
        if impl_path.exists():
            try:
                for line in impl_path.read_text().splitlines():
                    stripped = line.strip()
                    if stripped.startswith('signal_frequency'):
                        sig_freq = stripped.split('=', 1)[1].strip().strip("'\"").strip()
                        break
            except Exception as e:
                logger.warning('strategy_weights: could not read %s: %s', impl_path, e)
        static_cad = cadence_days(sig_freq)
        live_avg = live_avg_by_strat.get(sid)
        live_cad = max(1, math.ceil(live_avg)) if live_avg and live_avg > 0 else 0
        effective_cad = max(static_cad, live_cad)
        if live_cad > static_cad:
            logger.info('strategy_weights: %s cadence_days %d → %d (live avg=%.2f overrides static "%s")',
                        sid, static_cad, effective_cad, live_avg, sig_freq or 'unknown')
        out.append({
            'strategy_id': sid,
            'eligible_regimes': eligible,
            'signal_frequency': sig_freq,
            'cadence_days': effective_cad,
        })
    return out


def _load_backtest_sharpe(conn, strategy_ids: list[str]) -> dict[tuple[str, str], dict]:
    """Most-recent per-(strategy, regime) sharpe + trade_count, with a
    three-tier fallback so newly promoted strategies aren't immediately
    auto-demoted for lack of a backtest snapshot.

    Source priority (2026-05-14, post-unified-backtest cutover):
      1. strategy_backtest_regimes (joined to the latest primary_window=true
         run per strategy). This is the canonical source written by
         unified_backtest.py.
      2. strategy_regime_backtests — legacy regime-partitioned backtester
         (auto_backtest backfill path). Kept as fallback until every live
         strategy has a unified_backtest row.
      3. strategy_registry.backtest_sharpe × strategy_regime_params eligible
         regimes — single-overall-sharpe spread across declared regimes for
         strategies absent from both per-regime tables.
    """
    if not strategy_ids:
        return {}
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # Tier 1: unified_backtest (canonical).
    # `strategy_backtest_regimes` is keyed by run_id only — strategy_id lives
    # on `strategy_backtest_runs`. Earlier code selected br.strategy_id which
    # silently errored every Sunday with "column br.strategy_id does not
    # exist", forcing the entire weekly weights rebuild down to Tier 3.
    out = {}
    seen_strats = set()
    try:
        cur.execute('''
            SELECT latest.strategy_id, br.regime_state, br.sharpe, br.trade_count
            FROM strategy_backtest_regimes br
            JOIN (
                SELECT DISTINCT ON (strategy_id) strategy_id, run_id
                FROM strategy_backtest_runs
                WHERE strategy_id = ANY(%s) AND primary_window = TRUE
                ORDER BY strategy_id, run_at DESC
            ) latest ON latest.run_id = br.run_id
            WHERE br.sharpe IS NOT NULL
        ''', (strategy_ids,))
        for r in cur:
            out[(r['strategy_id'], r['regime_state'])] = {
                'bt_sharpe': float(r['sharpe']),
                'bt_n':      int(r['trade_count']) if r['trade_count'] is not None else None,
            }
            seen_strats.add(r['strategy_id'])
    except Exception as e:
        # Tables not yet migrated / DB hiccup — fall through to legacy sources.
        # Roll back the failed transaction so subsequent tier queries on the
        # same connection don't crash with InFailedSqlTransaction (2026-05-16
        # found this had been silently breaking the Sunday cron whenever
        # Tier 1 errored — Tier 2 + Tier 3 never ran).
        logger.warning('strategy_weights tier-1 backtest fetch failed (%s); falling back', e)
        conn.rollback()

    # Tier 2: legacy strategy_regime_backtests (only for strategies with no
    # unified_backtest row yet).
    missing = [s for s in strategy_ids if s not in seen_strats]
    if missing:
        cur.execute('''
            SELECT DISTINCT ON (strategy_id, regime_state)
                   strategy_id, regime_state, sharpe, trade_count
            FROM strategy_regime_backtests
            WHERE strategy_id = ANY(%s) AND sharpe IS NOT NULL
            ORDER BY strategy_id, regime_state, run_at DESC NULLS LAST
        ''', (missing,))
        for r in cur:
            out[(r['strategy_id'], r['regime_state'])] = {
                'bt_sharpe': float(r['sharpe']) if r['sharpe'] is not None else None,
                'bt_n':      int(r['trade_count']) if r['trade_count'] is not None else None,
            }
            seen_strats.add(r['strategy_id'])

    # Tier 3: strategy_registry overall sharpe ⨯ eligible regimes.
    still_missing = [s for s in strategy_ids if s not in seen_strats]
    if still_missing:
        cur.execute('''
            SELECT srp.strategy_id, srp.regime_state,
                   sr.backtest_sharpe, sr.backtest_trade_count
            FROM strategy_regime_params srp
            JOIN strategy_registry sr ON sr.id = srp.strategy_id
            WHERE srp.strategy_id = ANY(%s)
              AND srp.eligible = TRUE
              AND sr.backtest_sharpe IS NOT NULL
        ''', (still_missing,))
        for r in cur:
            out[(r['strategy_id'], r['regime_state'])] = {
                'bt_sharpe': float(r['backtest_sharpe']),
                'bt_n':      int(r['backtest_trade_count']) if r['backtest_trade_count'] is not None else None,
            }
    return out


def _apply_regime_agnostic_override(
    conn,
    bt: dict,
    active: list[dict],
) -> None:
    """Override per-regime bt_sharpe with overall bt_sharpe for strategies
    flagged `metadata.regime_agnostic_sharpe = True` in manifest.

    Some strategies (insider movements per operator directive 2026-05-28)
    have alpha that does not split usefully across regimes — the per-regime
    Sharpe from unified_backtest reflects trade-distribution noise more than
    real regime sensitivity. For those, use the OVERALL bt_sharpe across all
    eligible regimes so weights are uniform.

    Mutates `bt` in place. Idempotent. Safe to call after every backtest
    refresh because it reads the latest primary_window run each time.
    """
    manifest = json.loads((ROOT / 'src' / 'strategies' / 'manifest.json').read_text())
    agnostic_sids = {
        sid for sid, e in manifest.get('strategies', {}).items()
        if (e.get('metadata') or {}).get('regime_agnostic_sharpe') is True
    }
    if not agnostic_sids:
        return
    active_sids = {s['strategy_id'] for s in active}
    target_sids = list(agnostic_sids & active_sids)
    if not target_sids:
        return

    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute('''
        SELECT DISTINCT ON (strategy_id) strategy_id, total_sharpe, total_trades
          FROM strategy_backtest_runs
         WHERE strategy_id = ANY(%s) AND primary_window = TRUE
           AND total_sharpe IS NOT NULL
         ORDER BY strategy_id, run_at DESC
    ''', (target_sids,))
    overall = {r['strategy_id']: {
        'bt_sharpe': float(r['total_sharpe']),
        'bt_n':      int(r['total_trades']) if r['total_trades'] is not None else None,
    } for r in cur}

    for s in active:
        sid = s['strategy_id']
        if sid not in overall:
            continue
        for R in s['eligible_regimes']:
            bt[(sid, R)] = dict(overall[sid])  # copy so callers can't mutate the source
        logger.info('strategy_weights: %s regime-agnostic override → bt_sharpe=%.4f across %s',
                    sid, overall[sid]['bt_sharpe'], s['eligible_regimes'])


def _load_regime_by_date() -> dict:
    """Read historical_regimes.parquet → { 'YYYY-MM-DD': regime_state }.

    Used to attribute each closed trade to the regime that was in force
    on its close date. The parquet ships with ~2,500 trading days back
    to 2016 — comfortably covers our entire closed-trade window.
    """
    try:
        import pandas as pd
        df = pd.read_parquet(ROOT / 'data' / 'master' / 'historical_regimes.parquet')
        out = {}
        for _, row in df.iterrows():
            d = str(row['date'])
            if 'T' in d:
                d = d.split('T', 1)[0]
            out[d] = row['regime']
        return out
    except Exception as e:
        logger.warning('strategy_weights: historical_regimes.parquet unavailable: %s', e)
        return {}


def _load_live_sharpe(conn, strategy_ids: list[str], regime_by_date: dict) -> dict[tuple[str, str], dict]:
    """Live per-(strategy, regime) Sharpe from closed trades.

    For each closed trade we look up the regime on `closed_at` via the
    parquet map; trades whose date isn't in the map are skipped.
    """
    if not strategy_ids or not regime_by_date:
        return {}
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute('''
        SELECT es.strategy_id, sp.closed_at, sp.pnl_pct
        FROM signal_performance sp
        JOIN execution_signals es ON es.id = sp.signal_id
        WHERE sp.status = 'closed'
          AND es.strategy_id = ANY(%s)
          AND sp.pnl_pct IS NOT NULL
    ''', (strategy_ids,))

    # Bucket pnl values by (strategy, regime)
    from collections import defaultdict
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in cur:
        if r['closed_at'] is None:
            continue
        d = str(r['closed_at'])
        if 'T' in d:
            d = d.split('T', 1)[0]
        regime = regime_by_date.get(d)
        if not regime:
            continue
        buckets[(r['strategy_id'], regime)].append(float(r['pnl_pct']))

    out: dict[tuple[str, str], dict] = {}
    for key, vals in buckets.items():
        n = len(vals)
        if n < 1:
            continue
        mu = sum(vals) / n
        if n >= 2:
            # Sample variance
            var = sum((v - mu) ** 2 for v in vals) / (n - 1)
            sigma = var ** 0.5
        else:
            sigma = 0.0
        live_sharpe = (mu / sigma) if sigma > 0 else None
        out[key] = {'live_sharpe': live_sharpe, 'live_n': n}
    return out


def _effective_sharpe(bt: dict | None, live: dict | None):
    """Sample-size weighted blend.

    Returns (effective_sharpe, bt_sharpe, bt_n, live_sharpe, live_n).
    Each element may be None if data is absent. effective_sharpe is None
    iff neither bt nor live yields a usable Sharpe.
    """
    bt_s = bt['bt_sharpe'] if (bt and bt['bt_sharpe'] is not None) else None
    bt_n = bt['bt_n']      if (bt and bt['bt_n']      is not None) else None
    lv_s = live['live_sharpe'] if (live and live['live_sharpe'] is not None) else None
    lv_n = live['live_n']      if (live and live['live_n']      is not None) else None

    if bt_n and bt_s is not None and lv_n and lv_s is not None:
        eff = (bt_n * bt_s + lv_n * lv_s) / (bt_n + lv_n)
    elif bt_n and bt_s is not None:
        eff = bt_s
    elif lv_n and lv_s is not None and lv_n > 0:
        eff = lv_s
    else:
        eff = None
    return eff, bt_s, bt_n, lv_s, lv_n


def rebuild(trigger: str = 'manual', verbose: bool = False) -> list[StrategyWeightRow]:
    """Recompute every (strategy, regime) weight and persist."""
    conn = _db()
    try:
        active = _load_active_strategies(conn)
        sids = [s['strategy_id'] for s in active]
        bt   = _load_backtest_sharpe(conn, sids)
        # Insider strategies (S12 BUY + S15 SHORT, 2026-05-28) flag
        # metadata.regime_agnostic_sharpe — override per-regime bt_sharpe
        # with the overall so weekly unified_backtest re-runs don't
        # restore regime-split values.
        _apply_regime_agnostic_override(conn, bt, active)
        regime_by_date = _load_regime_by_date()
        live = _load_live_sharpe(conn, sids, regime_by_date)
        oue  = _load_oue_by_strategy_regime(conn, sids)

        if verbose:
            print(f'active strategies: {len(active)}, backtest rows: {len(bt)}, '
                  f'live buckets: {len(live)}, oue buckets: {len(oue)}')

        # weight column = oue_adjusted_sharpe directly (2026-05-19, operator
        # spec "make sure weight itself is strategy sharpe"). Normalisation
        # by Σ sharpe was a representation choice that obscured the absolute
        # conviction level; the sizer's λ·NAV renormalisation downstream
        # already handles per-cycle allocation, so this change is sizing-
        # invariant but makes the table's `weight` column directly
        # comparable across strategies as a Sharpe magnitude.
        per_regime_positives: dict[str, list[dict]] = {}
        for s in active:
            for R in s['eligible_regimes']:
                eff, bt_s, bt_n, lv_s, lv_n = _effective_sharpe(bt.get((s['strategy_id'], R)),
                                                                live.get((s['strategy_id'], R)))
                if eff is None or eff <= 0:
                    continue
                # OUE multiplier (per regime, time-decayed). Multiplies
                # effective_sharpe before normalization so the bucket is
                # judged on recent OUE-adjusted edge — a strategy that
                # was bad 4 months ago but is delivering now gets its
                # multiplier back as old trades age out (τ=45d).
                oue_row = oue.get((s['strategy_id'], R), {
                    'over': 0.0, 'under': 0.0, 'expected': 0.0,
                    'lifetime_over': 0, 'lifetime_under': 0, 'lifetime_expected': 0,
                })
                mult = _oue_multiplier(
                    oue_row['over'], oue_row['under'], oue_row['expected'],
                    lifetime_over=oue_row['lifetime_over'],
                    lifetime_under=oue_row['lifetime_under'],
                    lifetime_expected=oue_row['lifetime_expected'],
                )
                eff_adj = eff * mult
                per_regime_positives.setdefault(R, []).append({
                    'strategy_id': s['strategy_id'],
                    'cadence_days': s['cadence_days'],
                    'bt_sharpe': bt_s, 'bt_n': bt_n,
                    'live_sharpe': lv_s, 'live_n': lv_n,
                    'effective_sharpe': eff,
                    # Lifetime integer counts get persisted (matches the
                    # dashboard's #O/U/E column semantics + audit trail).
                    'oue_over':            oue_row['lifetime_over'],
                    'oue_under':           oue_row['lifetime_under'],
                    'oue_expected':        oue_row['lifetime_expected'],
                    'oue_multiplier':      mult,
                    'oue_adjusted_sharpe': eff_adj,
                })

        rows: list[StrategyWeightRow] = []
        for R, entries in per_regime_positives.items():
            for e in entries:
                # weight = raw OUE-adjusted Sharpe (operator spec). The
                # sizer downstream re-normalises absolute scale via
                # scale = λ·NAV / Σ|ticker_w|, so per-cycle allocation
                # is invariant to scale.
                w = e['oue_adjusted_sharpe']
                # Sharpe scales as σ·sqrt(T): a T-day holder's per-cycle
                # equivalent contribution is w/sqrt(T), not w/T.
                w_daily = w / math.sqrt(max(1, e['cadence_days']))
                rows.append(StrategyWeightRow(
                    strategy_id=e['strategy_id'], regime_state=R,
                    cadence_days=e['cadence_days'],
                    bt_sharpe=e['bt_sharpe'], bt_n=e['bt_n'],
                    live_sharpe=e['live_sharpe'], live_n=e['live_n'],
                    effective_sharpe=e['effective_sharpe'],
                    weight=w, daily_weight=w_daily,
                    oue_over=e['oue_over'], oue_under=e['oue_under'],
                    oue_expected=e['oue_expected'],
                    oue_multiplier=e['oue_multiplier'],
                    oue_adjusted_sharpe=e['oue_adjusted_sharpe'],
                ))

        cur = conn.cursor()
        cur.execute('UPDATE strategy_weights_by_regime SET is_current = FALSE WHERE is_current')
        for r in rows:
            cur.execute('''
                INSERT INTO strategy_weights_by_regime
                  (strategy_id, regime_state, cadence_days,
                   bt_sharpe, bt_n, live_sharpe, live_n,
                   effective_sharpe, weight, daily_weight, trigger, is_current,
                   oue_over, oue_under, oue_expected,
                   oue_multiplier, oue_adjusted_sharpe)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, TRUE, %s,%s,%s,%s,%s)
            ''', (r.strategy_id, r.regime_state, r.cadence_days,
                  r.bt_sharpe, r.bt_n, r.live_sharpe, r.live_n,
                  r.effective_sharpe, r.weight, r.daily_weight, trigger,
                  r.oue_over, r.oue_under, r.oue_expected,
                  r.oue_multiplier, r.oue_adjusted_sharpe))
        conn.commit()

        if verbose:
            for R, entries in per_regime_positives.items():
                s_sum = sum(e['oue_adjusted_sharpe'] for e in entries)
                print(f'  {R}: {len(entries)} strategies, Σ weight (=Σ oue_adj_sharpe) = {s_sum:.3f}')

        # Auto-demote chain — gated by env flag so the first sweep is
        # operator-initiated, not implicit. Set OPENCLAW_AUTO_DEMOTE=1 in
        # the weekly cron's environment (or for a manual --rebuild) once
        # the operator has reviewed which strategies the engine would
        # demote (python3 -m execution.strategy_weights --show-negative).
        if os.environ.get('OPENCLAW_AUTO_DEMOTE') == '1':
            try:
                from strategies.lifecycle import auto_demote_negative_sharpe
                demoted = auto_demote_negative_sharpe(reason=f'auto_demote_after_{trigger}')
                if demoted and verbose:
                    print(f'auto-demoted {len(demoted)} strategies: {demoted}')
            except ImportError:
                pass
            except Exception as e:
                logger.warning('auto_demote chain failed: %s', e)
        elif verbose:
            # Dry-run signal so the operator sees what *would* happen
            from collections import Counter
            try:
                candidates = find_negative_across_all_eligible()
                if candidates:
                    print(f'auto_demote dry-run: {len(candidates)} candidate strategies for demotion (set OPENCLAW_AUTO_DEMOTE=1 to apply)')
                    for sid in candidates[:10]:
                        print(f'  - {sid}')
                    if len(candidates) > 10:
                        print(f'  ... and {len(candidates) - 10} more')
            except Exception:
                pass

        return rows
    finally:
        conn.close()


def load_current(regime_state: str) -> list[dict]:
    """Read current weights for a regime; used by the sizer."""
    conn = _db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute('''
            SELECT strategy_id, cadence_days, effective_sharpe, weight, daily_weight
            FROM strategy_weights_by_regime
            WHERE regime_state = %s AND is_current
        ''', (regime_state,))
        return [dict(r) for r in cur]
    finally:
        conn.close()


GRACE_PERIOD_DAYS_DEFAULT = 30


def _strategies_in_grace_period(manifest: dict, grace_days: int) -> set[str]:
    """Strategies whose most recent transition INTO live/monitoring is more
    recent than `grace_days` ago. Exempt from auto-demote so freshly-
    approved strategies have time to accumulate closed trades before the
    Sharpe-across-all-regimes guard fires.

    Reads the manifest history (no DB query). A strategy WITHOUT a
    transition record into live/monitoring (e.g. registered directly in
    that state by a migration) gets the benefit of the doubt — included
    in the exempt set, since we can't tell when it became active.

    2026-05-14 incident: S_transitioning_overbought_revert was promoted
    candidate→live at 04:34:27 and auto_demote_after_lifecycle_change
    fired 6 minutes later, before signal_performance had recorded the
    5 closed trades the strategy had completed. The 30-day window
    matches MasterMindJohn's weekly review cadence — operator + brain
    decide demote, not the hardcoded engine.
    """
    if grace_days <= 0:
        return set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=grace_days)
    in_grace: set[str] = set()
    for sid, entry in manifest.get('strategies', {}).items():
        if entry.get('state') not in ('live', 'monitoring'):
            continue
        history = entry.get('history') or []
        # Find most recent transition INTO live or monitoring
        most_recent_into_active = None
        for h in reversed(history):
            if h.get('to_state') in ('live', 'monitoring'):
                ts_str = h.get('timestamp')
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    most_recent_into_active = ts
                    break
                except (ValueError, TypeError):
                    continue
        if most_recent_into_active is None:
            # No tracked transition — give benefit of the doubt
            in_grace.add(sid)
            continue
        if most_recent_into_active > cutoff:
            in_grace.add(sid)
    return in_grace


def find_negative_across_all_eligible(conn=None, grace_days: int | None = None) -> list[str]:
    """Strategies whose effective_sharpe ≤ 0 across EVERY eligible regime
    AND that have ZERO closed positions to date AND have been live for
    longer than the grace period.

    Three requirements:
      1. No positive-Sharpe row in the current strategy_weights_by_regime
         snapshot — the strategy has no regime where the formula would
         deploy it.
      2. No closed signals in signal_performance — the strategy has no
         real-world track record. Strategies WITH closed positions
         (positive or negative) stay live regardless of Sharpe; their
         disposition is for MasterMind's weekly review + the operator
         to decide, not the hardcoded engine.
      3. Out of grace period — the strategy entered live/monitoring
         more than `grace_days` ago (default 30, override via
         OPENCLAW_AUTO_DEMOTE_GRACE_DAYS env var). Newly-approved
         strategies are exempt so they have time to trade.

    Auto-demoted strategies are moved live→candidate via
    auto_demote_negative_sharpe in src/strategies/lifecycle.py.
    """
    if grace_days is None:
        try:
            grace_days = int(os.environ.get('OPENCLAW_AUTO_DEMOTE_GRACE_DAYS',
                                            GRACE_PERIOD_DAYS_DEFAULT))
        except (TypeError, ValueError):
            grace_days = GRACE_PERIOD_DAYS_DEFAULT
    own = conn is None
    if own:
        conn = _db()
    try:
        manifest = json.loads((ROOT / 'src' / 'strategies' / 'manifest.json').read_text())
        active_ids = [sid for sid, e in manifest.get('strategies', {}).items()
                      if e.get('state') in ('live', 'monitoring')]
        if not active_ids:
            return []
        in_grace = _strategies_in_grace_period(manifest, grace_days)
        cur = conn.cursor()
        # Set A: strategies with at least one positive-Sharpe regime
        cur.execute('''
            SELECT DISTINCT strategy_id FROM strategy_weights_by_regime
            WHERE is_current AND strategy_id = ANY(%s)
        ''', (active_ids,))
        with_any_positive = {r[0] for r in cur}
        # Set B: strategies that have any closed signal
        cur.execute('''
            SELECT DISTINCT strategy_id FROM signal_performance
            WHERE status = 'closed' AND strategy_id = ANY(%s)
        ''', (active_ids,))
        with_closed_history = {r[0] for r in cur}
        # Demote-eligible = active AND (not in A) AND (not in B) AND (not in grace)
        return [s for s in active_ids
                if s not in with_any_positive
                and s not in with_closed_history
                and s not in in_grace]
    finally:
        if own:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--rebuild', action='store_true')
    ap.add_argument('--trigger', default='manual')
    ap.add_argument('--verbose', action='store_true')
    ap.add_argument('--show-negative', action='store_true')
    args = ap.parse_args(argv)
    if args.rebuild:
        rows = rebuild(trigger=args.trigger, verbose=args.verbose)
        print(f'persisted {len(rows)} rows ({args.trigger})')
    if args.show_negative:
        neg = find_negative_across_all_eligible()
        print(f'{len(neg)} strategy/strategies negative across all eligible regimes:')
        for s in neg:
            print('  ', s)
    return 0


if __name__ == '__main__':
    sys.exit(main())
