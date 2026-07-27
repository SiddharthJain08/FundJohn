#!/usr/bin/env python3
"""universe_grid_cli.py — SP-2 Phase C Task 0.5 / SP-7 Phase B Task 7.

Runs a resolver→regime-blended 8-metric backtest grid for a given strategy +
candidate universe predicate. Designed to be consumed by the Opus curator.

Usage (legacy mode):
  python3 -m backtest.universe_grid_cli \\
    --strategy momentum_12_1 \\
    --start 2023-01-01 \\
    --end 2023-12-31 \\
    --resolver-override sp500 \\
    --metrics-json \\
    --seed 42

Usage (tier mode, SP-7 Phase B):
  python3 -m backtest.universe_grid_cli \\
    --strategy momentum_12_1 \\
    --start 2023-01-01 \\
    --end 2023-12-31 \\
    --membership-artifact data/universe_tier_membership_<run_id>.parquet \\
    --tier sp500 \\
    --metrics-json

Exit codes:
  0  success (JSON printed to stdout)
  1  backtest error
  2  bad arguments (unknown candidate / missing args / mixed modes)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))

from strategies.universe_default import CANDIDATE_PREDICATES
from strategies.universe_resolver import MockResolver
from strategies._db_adapters import PostgresMetadataDB, ParquetCoverage
from strategies.base import CANONICAL_REGIMES
from backtest.regime_blended_backtest import regime_day_frequency

REGIMES_PARQUET = ROOT / 'data' / 'master' / 'historical_regimes.parquet'
MANIFEST_PATH = ROOT / 'src' / 'strategies' / 'manifest.json'


def trade_sha(trades: list[dict]) -> str:
    """Deterministic SHA-256 over the trade list (order-independent).
    Used by the ladder driver's extremes-first degenerate detection."""
    lines = sorted(
        f"{t['ticker']}|{t['entry_date']}|{t['direction']}|{t.get('exit_date')}"
        for t in trades)
    return hashlib.sha256('\n'.join(lines).encode()).hexdigest()


def _manifest_loader():
    with open(MANIFEST_PATH) as f:
        return json.load(f)


def blend_metrics(
    per_regime: dict[str, dict],
    day_freq: dict[str, float],
    mean_universe_size: Optional[float] = None,
) -> dict:
    """Blend per-regime metrics into a single 8-key dict per the Phase C spec.

    Blend rules:
      - sharpe, sortino, calmar : day-frequency-weighted; skip None regimes +
                                   renormalize over contributing regimes.
      - max_dd_pct               : max across regimes.
      - trades_n                 : sum of per-regime trade_count.
      - win_rate, mean_holding_days : trade-count-weighted across regimes.
      - mean_universe_size       : passed as kwarg (caller computes).

    For ratio metrics (sharpe/sortino/calmar): if all contributing regimes are
    None, return None for that metric.
    """

    # ── Helper: day-freq-weighted blend over non-None values ────────────
    def _day_freq_weighted(key: str) -> Optional[float]:
        contributors = [
            (r, per_regime[r][key], day_freq.get(r, 0.0))
            for r in CANONICAL_REGIMES
            if per_regime[r].get(key) is not None
        ]
        if not contributors:
            return None
        total_weight = sum(w for _, _, w in contributors)
        if total_weight < 1e-12:
            # Fall back to equal weight
            total_weight = len(contributors)
            return sum(v for _, v, _ in contributors) / total_weight
        return sum(v * w for _, v, w in contributors) / total_weight

    # ── trades_n = sum ───────────────────────────────────────────────────
    trades_n = sum(
        int(per_regime[r].get('trade_count', 0) or 0)
        for r in CANONICAL_REGIMES
    )

    # ── max_dd_pct = max ─────────────────────────────────────────────────
    max_dd_pct = max(
        float(per_regime[r].get('max_dd_pct', 0.0) or 0.0)
        for r in CANONICAL_REGIMES
    )

    # ── day-freq-weighted ratio metrics ──────────────────────────────────
    sharpe = _day_freq_weighted('sharpe')
    sortino = _day_freq_weighted('sortino')
    calmar = _day_freq_weighted('calmar')

    # ── trade-count-weighted: win_rate, mean_holding_days ────────────────
    win_rate: Optional[float] = None
    mean_holding_days: Optional[float] = None
    if trades_n > 0:
        win_contributors = [
            (per_regime[r].get('hit_rate'), int(per_regime[r].get('trade_count', 0) or 0))
            for r in CANONICAL_REGIMES
            if per_regime[r].get('hit_rate') is not None
               and int(per_regime[r].get('trade_count', 0) or 0) > 0
        ]
        hold_contributors = [
            (per_regime[r].get('avg_holding_days'), int(per_regime[r].get('trade_count', 0) or 0))
            for r in CANONICAL_REGIMES
            if per_regime[r].get('avg_holding_days') is not None
               and int(per_regime[r].get('trade_count', 0) or 0) > 0
        ]
        if win_contributors:
            total_tc = sum(tc for _, tc in win_contributors)
            if total_tc > 0:
                win_rate = sum(v * tc for v, tc in win_contributors) / total_tc
        if hold_contributors:
            total_tc = sum(tc for _, tc in hold_contributors)
            if total_tc > 0:
                mean_holding_days = sum(v * tc for v, tc in hold_contributors) / total_tc

    result = {
        'sharpe': None if sharpe is None else round(float(sharpe), 4),
        'max_dd_pct': round(float(max_dd_pct), 4),
        'win_rate': None if win_rate is None else round(float(win_rate), 4),
        'mean_universe_size': mean_universe_size,
        'trades_n': int(trades_n),
        'sortino': None if sortino is None else round(float(sortino), 4),
        'calmar': None if calmar is None else round(float(calmar), 4),
        'mean_holding_days': None if mean_holding_days is None else round(float(mean_holding_days), 2),
    }
    return result


def _simulate_grid(
    strategy_id: str,
    start_date: str,
    end_date: str,
    resolver,
) -> tuple[dict, Optional[float], str]:
    """Run the simulation core (no DB writes) and return (per_regime, mean_universe_size, trade_sha).

    Delegates to ``_per_bar_simulate`` from ``backtest.unified_backtest`` —
    the single source of truth for the per-bar loop. Grid runs never write
    to Postgres; ``run_backtest`` adds DB persistence on top of the same loop.
    """
    import pandas as pd
    from backtest.unified_backtest import (
        load_prices_panels, load_regimes, load_strategy_class,
        find_strategy_file, aggregate_per_regime, _log, _per_bar_simulate,
        resolve_cost_model_bps, _resolve_instrument_class,
    )

    filepath = find_strategy_file(strategy_id)
    if not filepath:
        raise FileNotFoundError(f'no implementation file for {strategy_id}')
    strategy_cls = load_strategy_class(filepath)
    instance = strategy_cls()
    instance.active_in_regimes = list(CANONICAL_REGIMES)

    close_wide, bars_by_ticker = load_prices_panels()
    regimes = load_regimes()

    # §7-corrected cost model: mirror run_backtest's slippage resolution EXACTLY
    # so grid metrics are comparable to production (true-MTM is already on; this
    # closes the only remaining gap — always-adverse slippage). Same env-var name
    # and same default semantics (unset → ON, via the '1' default) as
    # unified_backtest.run_backtest:857 — must match EXACTLY or the grid diverges.
    _instrument_class = _resolve_instrument_class(strategy_id, filepath=filepath)
    _cost_bps = resolve_cost_model_bps(_instrument_class)
    _slippage_on = os.environ.get('OPENCLAW_BACKTEST_SLIPPAGE', '1') != '0'
    _slippage_bps = _cost_bps if _slippage_on else 0.0

    # Honest cost model (2026-07-27): mirror run_backtest's per-ticker
    # half-spread map + live asset-eligibility gate (equity/etp only, loud
    # fallback to flat/ungated). Tier selection prices every universe on the
    # SAME cost model as canonical runs — without this the ladder optimizes
    # tiers on costs the live book never pays.
    from backtest.unified_backtest import load_ticker_cost_bps, load_bt_asset_gate
    _honest_kwargs = {}
    if _slippage_on and _instrument_class in ('equity', 'etp'):
        _cost_map = load_ticker_cost_bps()
        if _cost_map:
            _honest_kwargs['cost_bps_by_ticker'] = _cost_map
            _log(f'grid spread-cost model: {len(_cost_map)} tickers; '
                 f'fallback flat {_slippage_bps}bps for unmapped')
    if _instrument_class in ('equity', 'etp'):
        _gate_map = load_bt_asset_gate()
        if _gate_map:
            _honest_kwargs['asset_gate'] = _gate_map
            _log(f'grid asset gate: {len(_gate_map)} symbols mapped')

    start_dt = pd.Timestamp(start_date)
    end_dt = pd.Timestamp(end_date)
    oos_dates = close_wide.loc[start_dt:end_dt].index

    sim = _per_bar_simulate(
        instance, close_wide, bars_by_ticker, regimes, start_dt, end_dt,
        strategy_id=strategy_id,
        resolver=resolver,
        slippage_bps=_slippage_bps,
        **_honest_kwargs,
    )
    trades         = sim['trades']
    universe_sizes = sim['universe_sizes']

    _pred = getattr(resolver, '_forced_predicate', type(resolver))
    _pred_name = getattr(_pred, '__name__', repr(_pred))
    _log(f'grid sim: {len(oos_dates)} bars, {len(trades)} trades, '
         f'{len(universe_sizes)} universe samples for {strategy_id}/{_pred_name}')

    # aggregate_per_regime calls aggregate_metrics internally and copies all keys
    # (including sortino/calmar) into out[regime]=agg. Low-sample regimes (<5 trades)
    # have sharpe/sortino/calmar nulled inside aggregate_per_regime itself.
    per_regime = aggregate_per_regime(trades, regimes.loc[start_dt:end_dt])

    mean_universe_size: Optional[float] = None
    if universe_sizes:
        mean_universe_size = round(float(sum(universe_sizes) / len(universe_sizes)), 2)

    return per_regime, mean_universe_size, trade_sha(trades)


def main_with_args(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='SP-2 Phase C grid / SP-7 Phase B tier-ladder cell')
    ap.add_argument('--strategy', required=True)
    ap.add_argument('--start', required=True)
    ap.add_argument('--end', required=True)
    ap.add_argument('--resolver-override',
                    help=f'legacy mode; one of: {sorted(CANDIDATE_PREDICATES)}')
    ap.add_argument('--membership-artifact',
                    help='SP-7 tier mode: path to the frozen membership parquet')
    ap.add_argument('--tier', help='SP-7 tier mode: tier name inside the artifact')
    ap.add_argument('--metrics-json', action='store_true')
    ap.add_argument('--seed', type=int, default=42)
    args = ap.parse_args(argv)

    legacy = args.resolver_override is not None
    tiermode = args.membership_artifact is not None or args.tier is not None
    if legacy == tiermode:  # both or neither
        print('[universe_grid_cli] ERROR: pass EITHER --resolver-override '
              'OR (--membership-artifact AND --tier)', file=sys.stderr)
        return 2
    if tiermode and not (args.membership_artifact and args.tier):
        print('[universe_grid_cli] ERROR: tier mode needs BOTH '
              '--membership-artifact and --tier', file=sys.stderr)
        return 2

    try:
        if legacy:
            candidate = args.resolver_override
            if candidate not in CANDIDATE_PREDICATES:
                print(f'[universe_grid_cli] ERROR: unknown candidate "{candidate}". '
                      f'Valid choices: {sorted(CANDIDATE_PREDICATES)}', file=sys.stderr)
                return 2
            uri = os.environ.get('POSTGRES_URI')
            if not uri:
                raise RuntimeError('POSTGRES_URI not set')
            db = PostgresMetadataDB(uri)
            cov = ParquetCoverage()
            resolver = MockResolver(db=db, coverage=cov,
                                    predicate=CANDIDATE_PREDICATES[candidate],
                                    manifest_loader=_manifest_loader)
            label = candidate
        else:
            from backtest.precomputed_resolver import PrecomputedResolver
            resolver = PrecomputedResolver(args.membership_artifact, args.tier)
            label = args.tier

        per_regime, mus, tsha = _simulate_grid(args.strategy, args.start,
                                               args.end, resolver)
        day_freq = regime_day_frequency(REGIMES_PARQUET)
        metrics = blend_metrics(per_regime, day_freq, mean_universe_size=mus)
        metrics['trade_sha'] = tsha
        metrics['mode'] = 'tier' if tiermode else 'legacy'
        metrics['candidate'] = label
        print(json.dumps(metrics, sort_keys=True))
        return 0
    except FileNotFoundError as e:
        print(f'[universe_grid_cli] FAIL: {e}', file=sys.stderr)
        return 1
    except Exception as e:
        print(f'[universe_grid_cli] FAIL: {type(e).__name__}: {e}', file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return 1


def main() -> int:
    return main_with_args()


if __name__ == '__main__':
    sys.exit(main())
