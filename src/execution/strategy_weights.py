"""strategy_weights.py — per-(strategy, regime) weight engine.

Implements the formulas from
docs/superpowers/specs/2026-05-14-position-sizing-rewrite-design.md.

Per regime R, for each active strategy s with R ∈ eligible_regimes:
    effective_sharpe = (bt_n × bt_sharpe + live_n × live_sharpe)
                       / (bt_n + live_n)
    w(s, R)      = effective_sharpe / Σ_{s' positive} effective_sharpe(s')
    daily_weight = w(s, R) / cadence_days(s)

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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

import psycopg2
import psycopg2.extras

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
        out.append({
            'strategy_id': sid,
            'eligible_regimes': eligible,
            'signal_frequency': sig_freq,
            'cadence_days': cadence_days(sig_freq),
        })
    return out


def _load_backtest_sharpe(conn, strategy_ids: list[str]) -> dict[tuple[str, str], dict]:
    """Most-recent per-(strategy, regime) row from strategy_regime_backtests,
    with a strategy_registry fallback for strategies absent from the
    per-regime table.

    Two-tier source:
      1. strategy_regime_backtests — per-(strategy, regime) sharpe from the
         regime-partitioned backtester. Preferred.
      2. strategy_registry.backtest_sharpe — single overall sharpe across
         the strategy's entire backtest history. Applied to every regime
         the strategy is declared eligible for (strategy_regime_params).
         Used only for strategies absent from tier 1, so promoted
         strategies that haven't been regime-partition-backtested yet
         still get a sharpe and aren't auto-demoted out of the active
         stack the moment they're promoted.
    """
    if not strategy_ids:
        return {}
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute('''
        SELECT DISTINCT ON (strategy_id, regime_state)
               strategy_id, regime_state, sharpe, trade_count
        FROM strategy_regime_backtests
        WHERE strategy_id = ANY(%s) AND sharpe IS NOT NULL
        ORDER BY strategy_id, regime_state, run_at DESC NULLS LAST
    ''', (strategy_ids,))
    out = {}
    seen_strats = set()
    for r in cur:
        out[(r['strategy_id'], r['regime_state'])] = {
            'bt_sharpe': float(r['sharpe']) if r['sharpe'] is not None else None,
            'bt_n':      int(r['trade_count']) if r['trade_count'] is not None else None,
        }
        seen_strats.add(r['strategy_id'])

    missing = [s for s in strategy_ids if s not in seen_strats]
    if missing:
        cur.execute('''
            SELECT srp.strategy_id, srp.regime_state,
                   sr.backtest_sharpe, sr.backtest_trade_count
            FROM strategy_regime_params srp
            JOIN strategy_registry sr ON sr.id = srp.strategy_id
            WHERE srp.strategy_id = ANY(%s)
              AND srp.eligible = TRUE
              AND sr.backtest_sharpe IS NOT NULL
        ''', (missing,))
        for r in cur:
            out[(r['strategy_id'], r['regime_state'])] = {
                'bt_sharpe': float(r['backtest_sharpe']),
                'bt_n':      int(r['backtest_trade_count']) if r['backtest_trade_count'] is not None else None,
            }
    return out


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
        regime_by_date = _load_regime_by_date()
        live = _load_live_sharpe(conn, sids, regime_by_date)

        if verbose:
            print(f'active strategies: {len(active)}, backtest rows: {len(bt)}, live buckets: {len(live)}')

        per_regime_positives: dict[str, list[dict]] = {}
        for s in active:
            for R in s['eligible_regimes']:
                eff, bt_s, bt_n, lv_s, lv_n = _effective_sharpe(bt.get((s['strategy_id'], R)),
                                                                live.get((s['strategy_id'], R)))
                if eff is None or eff <= 0:
                    continue
                per_regime_positives.setdefault(R, []).append({
                    'strategy_id': s['strategy_id'],
                    'cadence_days': s['cadence_days'],
                    'bt_sharpe': bt_s, 'bt_n': bt_n,
                    'live_sharpe': lv_s, 'live_n': lv_n,
                    'effective_sharpe': eff,
                })

        rows: list[StrategyWeightRow] = []
        for R, entries in per_regime_positives.items():
            denom = sum(e['effective_sharpe'] for e in entries)
            if denom <= 0:
                continue
            for e in entries:
                w = e['effective_sharpe'] / denom
                w_daily = w / max(1, e['cadence_days'])
                rows.append(StrategyWeightRow(
                    strategy_id=e['strategy_id'], regime_state=R,
                    cadence_days=e['cadence_days'],
                    bt_sharpe=e['bt_sharpe'], bt_n=e['bt_n'],
                    live_sharpe=e['live_sharpe'], live_n=e['live_n'],
                    effective_sharpe=e['effective_sharpe'],
                    weight=w, daily_weight=w_daily,
                ))

        cur = conn.cursor()
        cur.execute('UPDATE strategy_weights_by_regime SET is_current = FALSE WHERE is_current')
        for r in rows:
            cur.execute('''
                INSERT INTO strategy_weights_by_regime
                  (strategy_id, regime_state, cadence_days,
                   bt_sharpe, bt_n, live_sharpe, live_n,
                   effective_sharpe, weight, daily_weight, trigger, is_current)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, TRUE)
            ''', (r.strategy_id, r.regime_state, r.cadence_days,
                  r.bt_sharpe, r.bt_n, r.live_sharpe, r.live_n,
                  r.effective_sharpe, r.weight, r.daily_weight, trigger))
        conn.commit()

        if verbose:
            for R, entries in per_regime_positives.items():
                print(f'  {R}: {len(entries)} strategies, Σ weight = 1.0 (by construction)')

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


def find_negative_across_all_eligible(conn=None) -> list[str]:
    """Strategies whose effective_sharpe ≤ 0 across EVERY eligible regime
    AND that have ZERO closed positions to date.

    Two requirements:
      1. No positive-Sharpe row in the current strategy_weights_by_regime
         snapshot — the strategy has no regime where the formula would
         deploy it.
      2. No closed signals in signal_performance — the strategy has no
         real-world track record. Strategies WITH closed positions
         (positive or negative) stay live regardless of Sharpe; their
         disposition is for MasterMind's weekly review + the operator
         to decide, not the hardcoded engine.

    Auto-demoted strategies are moved live→candidate via
    auto_demote_negative_sharpe in src/strategies/lifecycle.py. A strategy
    positive in any one of its eligible regimes stays live (excluded
    only from the bad regimes via weight = 0 rows the engine doesn't
    write).
    """
    own = conn is None
    if own:
        conn = _db()
    try:
        manifest = json.loads((ROOT / 'src' / 'strategies' / 'manifest.json').read_text())
        active_ids = [sid for sid, e in manifest.get('strategies', {}).items()
                      if e.get('state') in ('live', 'monitoring')]
        if not active_ids:
            return []
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
        # Demote-eligible = active AND (not in A) AND (not in B)
        return [s for s in active_ids
                if s not in with_any_positive and s not in with_closed_history]
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
