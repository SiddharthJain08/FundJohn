"""strategy_weights.py — per-(strategy, regime) weight engine.

Implements the formulas from
docs/superpowers/specs/2026-05-14-position-sizing-rewrite-design.md.

Per regime R, for each active strategy s with R ∈ eligible_regimes:
    effective_sharpe = bt_sharpe        # BACKTEST ONLY — see below
    weight        = effective_sharpe                  # no OUE multiplier (removed 2026-05-29)
    daily_weight  = weight / sqrt(cadence_days(s))

effective_sharpe was a sample-size blend of backtest and LIVE Sharpe until
2026-07-16 (`(bt_n×bt_sharpe + live_n×live_sharpe)/(bt_n+live_n)`). Retired by
operator directive: a per-strategy LIVE Sharpe is not a real measurement here.
The book takes AGGREGATED positions — many strategies signal one ticker, the
broker holds ONE position, and it exits through ONE Sharpe-weighted stacked
bracket — so every contributing strategy is booked that shared blended exit and
none of their own stops/targets ever ran. (The units differed too: bt_sharpe is
an annualized daily-return Sharpe, live_sharpe a raw per-trade ratio.) The blend
could let live data RESCUE a failing backtest: bt −0.40 + live 3.00 → +2.81, sized.
live_sharpe/live_n are still computed and persisted for observability and a future
PORTFOLIO-level Sharpe, which is measurable because it needs no per-strategy
attribution. Measuring strategies live would require a separate paper account
where each fires its own brackets. See _effective_sharpe.

`weight` is the raw effective Sharpe — NOT a normalised fraction.
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
    cadence_days: float
    bt_sharpe: float | None
    bt_n: int | None
    live_sharpe: float | None
    live_n: int | None
    effective_sharpe: float
    weight: float
    daily_weight: float
    # OUE health (per-regime closed-trade classification) for the dashboard
    # + comprehensive review + operator audit. oue_multiplier /
    # oue_adjusted_sharpe are retained as columns but are now always NULL —
    # the OUE multiplier was removed from sizing 2026-05-29 (weight =
    # effective_sharpe; corroboration governs gating; the per-(strategy,regime)
    # size_scalar governs allocation when OPENCLAW_STRATEGY_SIZE_SCALAR=1
    # (applied downstream in the sizer, not baked here)).
    oue_over: int | None = None
    oue_under: int | None = None
    oue_expected: int | None = None
    oue_multiplier: float | None = None
    oue_adjusted_sharpe: float | None = None


# Time-decay constant for the OUE *count* loader. The OUE multiplier was
# removed from sizing 2026-05-29 (corroboration + position-recs now govern
# strategy sizing), but the O/U/E counts are still loaded + persisted for
# the dashboard / weekly-review audit columns — this τ weights a close
# made `age_days` ago by exp(-age_days / OUE_TAU_DAYS).
OUE_TAU_DAYS = 45.0      # half-life ≈ 31 days; trade @ 90d ago weighted 0.14


def _regime_weight(effective_sharpe: float, cadence_days: float) -> tuple[float, float]:
    """Per-(strategy, regime) sizing weight from effective Sharpe.

    weight       = effective_sharpe                       (no OUE multiplier)
    daily_weight = effective_sharpe / sqrt(cadence_days)   (Sharpe scales as
                   σ·sqrt(T), so a T-day holder's per-cycle contribution is
                   w/sqrt(T)). cadence is floored at 1 day.

    The OUE multiplier was removed here 2026-05-29 (operator decision):
    strategy sizing is governed by cross-sector corroboration + the
    approved per-(strategy,regime) size_scalar (applied in the sizer when
    its gate is ON), so an additional OUE-derived scaling here was
    redundant. OUE classification is still loaded for the audit columns —
    it just no longer scales size."""
    w = effective_sharpe
    w_daily = w / math.sqrt(max(1, cadence_days))
    return w, w_daily


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

    # Backtest avg_holding_days is the CANONICAL cadence source for every
    # strategy (operator directive 2026-05-29). The static signal_frequency
    # label ('daily'/'weekly'/'monthly') is a placeholder — actual cadence
    # comes from the backtest's measured holding period (primary_window=true
    # unified_backtest result). live_avg becomes a fallback for the edge case
    # where a strategy is live but has no primary_window backtest; static_cad
    # is the final fallback (should not occur for properly-promoted strats).
    cur.execute('''
        SELECT DISTINCT ON (strategy_id) strategy_id, avg_holding_days
          FROM strategy_backtest_runs
         WHERE strategy_id = ANY(%s)
           AND primary_window = TRUE
           AND avg_holding_days IS NOT NULL
         ORDER BY strategy_id, run_at DESC
    ''', (active_ids,))
    backtest_avg_by_strat: dict[str, float] = {}
    for r in cur:
        v = r['avg_holding_days']
        if v is not None:
            try:
                backtest_avg_by_strat[r['strategy_id']] = float(v)
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
        backtest_avg = backtest_avg_by_strat.get(sid)
        # Backtest is canonical. Falls back to live_avg only if backtest is
        # absent, and to static_cad only if neither measured source exists.
        # static_cad is NOT applied as a max-floor — a strategy declared
        # 'monthly' whose backtest shows 4-day holds is sized as 4-day.
        # EXACT value — no ceil/round (operator directive 2026-05-29; DB column
        # was widened to NUMERIC(10,4) in migration 122 to preserve precision).
        if backtest_avg and backtest_avg > 0:
            chosen_avg, chosen_src = backtest_avg, 'backtest'
        elif live_avg and live_avg > 0:
            chosen_avg, chosen_src = live_avg, 'live'
        else:
            chosen_avg, chosen_src = 0.0, 'static_fallback'
        effective_cad = float(chosen_avg) if chosen_avg > 0 else float(static_cad)
        if chosen_src != 'static_fallback':
            logger.info('strategy_weights: %s cadence_days %.4f (%s; declared "%s"→%d)',
                        sid, effective_cad, chosen_src, sig_freq or 'unknown', static_cad)
        out.append({
            'strategy_id': sid,
            'eligible_regimes': eligible,
            'signal_frequency': sig_freq,
            'cadence_days': effective_cad,
        })
    return out


def _get_bt_sharpe_cap(cur) -> float:
    """Read bt_sharpe_plausibility_cap from pipeline_config; default 3.0.
    Interim guard (§7 metric recon, 2026-07-01) for the backtest-Sharpe
    methodology defect (flat P&L smearing understates vol -> inflated,
    overlap-dependent |Sharpe|). Set the key very high (e.g. 999) to disable.
    Fail-safe: any read error OR a non-positive value keeps the guard ON at the
    3.0 default (the clamp [-cap,+cap] is only sign-preserving for cap>0; a
    negative cap would flip an excluded negative Sharpe to positive -> fundable,
    so a misconfig falls back to the safe default rather than breaking the
    invariant)."""
    try:
        cur.execute("SELECT value FROM pipeline_config WHERE key='bt_sharpe_plausibility_cap'")
        row = cur.fetchone()
        if row:
            v = float(row[0])
            if v > 0:
                return v
    except Exception:
        pass
    return 3.0


def _clamp_bt_sharpes(out: dict, cap: float) -> list:
    """Clamp each entry's bt_sharpe to [-cap, +cap] IN PLACE (sign-preserving,
    so no strategy changes inclusion/exclusion state). Leaves None / NaN / Inf
    untouched (the _is_sizeable_sharpe guard drops those). Returns the list of
    (key, before, after) tuples for entries actually clamped."""
    clamped = []
    for key, v in out.items():
        s = v.get('bt_sharpe')
        if s is not None and math.isfinite(s):
            c = max(-cap, min(cap, s))
            if c != s:
                clamped.append((key, s, c))
                v['bt_sharpe'] = c
    return clamped


def _load_backtest_sharpe(conn, strategy_ids: list[str]) -> dict[tuple[str, str], dict]:
    """Most-recent per-(strategy, regime) sharpe + trade_count, with a
    two-tier fallback so newly promoted strategies aren't immediately
    auto-demoted for lack of a backtest snapshot.

    Source priority (2026-05-14, post-unified-backtest cutover):
      1. strategy_backtest_regimes (joined to the latest primary_window=true
         run per strategy). This is the canonical source written by
         unified_backtest.py.
      2. strategy_regime_backtests — legacy regime-partitioned backtester
         (auto_backtest backfill path). Kept as fallback until every live
         strategy has a unified_backtest row.

    The former Tier 3 (strategy_registry.backtest_sharpe × eligible regimes)
    was RETIRED 2026-07-12 (Option B follow-up): the registry mirror stopped
    being written 2026-07-05, so that fallback could only serve frozen,
    §7-inflated pre-correction values. Strategies absent from both tiers now
    get NO bt entry (they ride live sharpe in _effective_sharpe or are
    excluded) and are logged loudly instead of silently mis-weighted.
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

    # Former Tier 3 (strategy_registry.backtest_sharpe mirror) RETIRED
    # 2026-07-12 — the mirror is no longer written (Option B, 2026-07-05), so
    # it could only serve stale §7-inflated values. Strategies with no rows in
    # either tier get no bt entry; log the gap loudly instead.
    still_missing = [s for s in strategy_ids if s not in seen_strats]
    if still_missing:
        logger.warning(
            'strategy_weights: %d strategies have NO canonical/legacy backtest '
            'rows (no bt term; live-sharpe only or excluded): %s',
            len(still_missing), ', '.join(sorted(still_missing)))
    cap = _get_bt_sharpe_cap(cur)
    clamped = _clamp_bt_sharpes(out, cap)
    if clamped:
        logger.info('bt_sharpe plausibility clamp: %d/%d entries clamped to ±%s (e.g. %s %.2f->%.2f)',
                    len(clamped), len(out), cap, clamped[0][0], clamped[0][1], clamped[0][2])
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
    """effective_sharpe = bt_sharpe. live_sharpe is RECORDED, never blended.

    Returns (effective_sharpe, bt_sharpe, bt_n, live_sharpe, live_n).

    This was a sample-size blend
    `(bt_n*bt_sharpe + live_n*live_sharpe)/(bt_n+live_n)` until 2026-07-16.
    Retired by operator directive, for two independent reasons:

    1. ATTRIBUTION. The book takes AGGREGATED positions: many strategies signal
       the same ticker and the broker holds ONE position exiting through ONE
       Sharpe-weighted stacked bracket. If S1 wants stop -5%/target +3% and S2
       wants -8%/+6%, the live exit fires at the blended level and BOTH are
       booked that shared outcome — neither strategy's own rule ever ran. So a
       per-strategy live pnl_pct measures the blend, not the strategy. Measuring
       a strategy live would need a separate paper account where each fires its
       own brackets independently; we do not have one.
    2. UNITS. bt_sharpe is an ANNUALIZED daily-return Sharpe
       ((mean-rf)/std*sqrt(252)); live_sharpe is a RAW per-trade ratio
       (mu/sigma over pnl_pct) with no time basis. The blend averaged two
       different quantities as though they were one.

    Concretely, the blend let live data RESCUE a failing backtest: bt -0.40 with
    live 3.00 over 500 trades produced +2.81 and got sized. Now it stays -0.40
    and is excluded.

    live_sharpe/live_n remain computed and persisted: as active portfolio days
    accumulate they can feed a PORTFOLIO-level Sharpe, which IS measurable
    precisely because it needs no per-strategy attribution.

    effective_sharpe is None unless a backtest Sharpe exists — a live-only
    strategy is NOT sizeable, deliberately (sizing it would size on the blend).
    Measured at the change: 0 of 70 current rows were live-only, so nothing
    deactivated.
    """
    bt_s = bt['bt_sharpe'] if (bt and bt['bt_sharpe'] is not None) else None
    bt_n = bt['bt_n']      if (bt and bt['bt_n']      is not None) else None
    lv_s = live['live_sharpe'] if (live and live['live_sharpe'] is not None) else None
    lv_n = live['live_n']      if (live and live['live_n']      is not None) else None

    eff = bt_s if (bt_n and bt_s is not None) else None
    return eff, bt_s, bt_n, lv_s, lv_n


def _is_sizeable_sharpe(eff: Optional[float]) -> bool:
    """A (strategy, regime) is sized ONLY when its effective Sharpe is a
    finite positive number.

    Non-finite (NaN/Inf) MUST be excluded: a regime's per-regime backtest
    Sharpe can be NaN when its daily-return series compounds below -100%
    (cumprod(1+r) <= 0 — reachable for high-frequency strategies in a single
    regime, e.g. several live strategies in HIGH_VOL). The old guard
    `eff is None or eff <= 0` did NOT catch it — `nan <= 0` is False — so a
    NaN weight would slip through and poison the Σ-effective-sharpe gate and
    the per-strategy daily_weight. `math.isfinite` rejects NaN and ±Inf."""
    return eff is not None and math.isfinite(eff) and eff > 0


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
                if not _is_sizeable_sharpe(eff):
                    continue
                # OUE counts are still loaded + persisted for the dashboard
                # #O/U/E column + audit trail, but they NO LONGER scale the
                # weight (multiplier removed 2026-05-29 — corroboration +
                # position-recs govern sizing). oue_multiplier /
                # oue_adjusted_sharpe are persisted as NULL to mark that the
                # multiplier is no longer applied.
                oue_row = oue.get((s['strategy_id'], R), {
                    'over': 0.0, 'under': 0.0, 'expected': 0.0,
                    'lifetime_over': 0, 'lifetime_under': 0, 'lifetime_expected': 0,
                })
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
                    'oue_multiplier':      None,
                    'oue_adjusted_sharpe': None,
                })

        rows: list[StrategyWeightRow] = []
        for R, entries in per_regime_positives.items():
            for e in entries:
                # weight = effective Sharpe directly (no OUE multiplier). The
                # sizer downstream re-normalises absolute scale via
                # scale = λ·NAV / Σ|ticker_w|, so per-cycle allocation
                # is invariant to scale.
                w, w_daily = _regime_weight(e['effective_sharpe'], e['cadence_days'])
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
                s_sum = sum(e['effective_sharpe'] for e in entries)
                print(f'  {R}: {len(entries)} strategies, Σ weight (=Σ effective_sharpe) = {s_sum:.3f}')

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
        # bt_n = per-regime backtest trade count; feeds the sizer's trade-count
        # weight factor (√(ln n / ln anchor)), which replaced the √(ln N) breadth
        # factor 2026-07-16. NULL/missing → factor 1.0 in the sizer (neutral).
        cur.execute('''
            SELECT strategy_id, cadence_days, effective_sharpe, weight, daily_weight, bt_n
            FROM strategy_weights_by_regime
            WHERE regime_state = %s AND is_current
        ''', (regime_state,))
        return [dict(r) for r in cur]
    finally:
        conn.close()


def load_universe_sizes() -> dict[str, int]:
    """Per-strategy universe size N (strategy_universe_sizes, migration 141),
    regime-independent. Feeds the sizer's √(ln N) breadth weight factor. Missing
    strategies simply aren't in the dict (breadth factor then defaults to 1.0)."""
    conn = _db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT strategy_id, universe_size FROM strategy_universe_sizes')
        return {r[0]: int(r[1]) for r in cur.fetchall()}
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
