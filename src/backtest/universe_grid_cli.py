#!/usr/bin/env python3
"""universe_grid_cli.py — SP-2 Phase C Task 0.5.

Runs a resolver→regime-blended 8-metric backtest grid for a given strategy +
candidate universe predicate. Designed to be consumed by the Opus curator.

Usage:
  python3 -m backtest.universe_grid_cli \\
    --strategy momentum_12_1 \\
    --start 2023-01-01 \\
    --end 2023-12-31 \\
    --resolver-override sp500 \\
    --metrics-json \\
    --seed 42

Exit codes:
  0  success (JSON printed to stdout)
  1  backtest error
  2  unknown --resolver-override candidate
"""
from __future__ import annotations

import argparse
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
from backtest.regime_blended_backtest import (
    CANONICAL_REGIMES,
    regime_day_frequency,
)

REGIMES_PARQUET = ROOT / 'data' / 'master' / 'historical_regimes.parquet'
MANIFEST_PATH = ROOT / 'src' / 'strategies' / 'manifest.json'


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
) -> tuple[dict, Optional[float]]:
    """Run the simulation core (no DB writes) and return (per_regime, mean_universe_size).

    NOTE: This intentionally mirrors the per-bar loop in
    ``backtest.unified_backtest.run_backtest``.  If you change signal
    generation, trade simulation, or universe dispatch there, keep this
    loop in sync.  The two paths are separate because run_backtest
    writes to Postgres; grid runs must never pollute the DB.
    """
    import pandas as pd
    import numpy as np
    from backtest.unified_backtest import (
        load_prices_panels, load_regimes, load_strategy_class,
        find_strategy_file, aggregate_per_regime, CANONICAL_REGIMES as CR,
        _signal_to_long_short, simulate_trade, _log, DEFAULT_MAX_HOLD_DAYS,
        aggregate_metrics,
    )
    from strategies.base import CANONICAL_REGIMES

    filepath = find_strategy_file(strategy_id)
    if not filepath:
        raise FileNotFoundError(f'no implementation file for {strategy_id}')
    strategy_cls = load_strategy_class(filepath)
    instance = strategy_cls()
    instance.active_in_regimes = list(CANONICAL_REGIMES)
    min_lookback = getattr(instance, 'min_lookback', 20)

    close_wide, bars_by_ticker = load_prices_panels()
    regimes = load_regimes()

    start_dt = pd.Timestamp(start_date)
    end_dt = pd.Timestamp(end_date)
    oos_dates = close_wide.loc[start_dt:end_dt].index

    try:
        from strategies.aux_data_loader import load_aux_data
    except Exception:
        load_aux_data = None

    trades: list[dict] = []
    universe_sizes: list[int] = []

    for current_date in oos_dates:
        prices_to_date = close_wide.loc[:current_date]
        if len(prices_to_date) < min_lookback + 5:
            continue
        cur_d = current_date.date() if hasattr(current_date, 'date') else current_date
        regime_state = regimes.get(current_date, None)
        if regime_state is None or pd.isna(regime_state):
            continue

        bar_universe = resolver.resolve(strategy_id, as_of=cur_d)
        universe_sizes.append(len(bar_universe))

        regime_payload = {
            'state': str(regime_state),
            'date': cur_d.isoformat(),
            'one_hot': {r: (1.0 if r == regime_state else 0.0) for r in CANONICAL_REGIMES},
            'transition_probs': {
                r1: {r2: (1.0 if r1 == r2 else 0.0) for r2 in CANONICAL_REGIMES}
                for r1 in CANONICAL_REGIMES
            },
        }

        aux = {'options': {}}
        if load_aux_data is not None:
            try:
                aux = load_aux_data(current_date)
            except Exception:
                aux = {'options': {}}

        try:
            signals = instance.generate_signals(prices_to_date, regime_payload, bar_universe, aux_data=aux)
        except TypeError:
            try:
                signals = instance.generate_signals(prices_to_date, regime_payload, bar_universe)
            except Exception:
                continue
        except Exception:
            continue

        if not signals:
            continue

        for sig in signals[:instance.MAX_SIGNALS]:
            direction = _signal_to_long_short(sig.direction)
            if direction == 0:
                continue
            ticker = sig.ticker
            if ticker not in bars_by_ticker:
                continue
            ticker_bars = bars_by_ticker[ticker]
            if current_date not in ticker_bars.index:
                continue
            entry_price = float(sig.entry_price) if (sig.entry_price and sig.entry_price > 0) \
                else float(ticker_bars.loc[current_date, 'close'])
            stop_loss = float(sig.stop_loss) if (sig.stop_loss and sig.stop_loss > 0) \
                else (entry_price * 0.93 if direction > 0 else entry_price * 1.07)
            target_1 = float(sig.target_1) if (sig.target_1 and sig.target_1 > 0) \
                else (entry_price * 1.08 if direction > 0 else entry_price * 0.92)
            if direction > 0 and (stop_loss >= entry_price or target_1 <= entry_price):
                continue
            if direction < 0 and (stop_loss <= entry_price or target_1 >= entry_price):
                continue
            exit_info = simulate_trade(ticker_bars, current_date, direction,
                                       entry_price, stop_loss, target_1, DEFAULT_MAX_HOLD_DAYS)
            trades.append({
                'ticker': ticker,
                'direction': 'long' if direction > 0 else 'short',
                'entry_date': cur_d,
                'entry_price': entry_price,
                'exit_date': (exit_info['exit_date'].date()
                              if hasattr(exit_info['exit_date'], 'date')
                              else exit_info['exit_date']),
                'exit_price': exit_info['exit_price'],
                'exit_reason': exit_info['exit_reason'],
                'holding_days': exit_info['holding_days'],
                'pnl_pct': exit_info['pnl_pct'],
                'entry_regime': str(regime_state),
                'signal_stop': stop_loss,
                'signal_target': target_1,
            })

    _log(f'grid sim: {len(oos_dates)} bars, {len(trades)} trades, '
         f'{len(universe_sizes)} universe samples for {strategy_id}/{resolver._forced_predicate.__name__}')

    per_regime = aggregate_per_regime(trades, regimes.loc[start_dt:end_dt])
    # Inject sortino/calmar into per_regime (aggregate_metrics already computes them;
    # aggregate_per_regime calls aggregate_metrics internally, but we need to add the
    # new keys that aggregate_per_regime doesn't copy out).
    # Re-compute per-regime trades to get sortino/calmar per regime.
    _by_regime: dict[str, list[dict]] = {r: [] for r in CANONICAL_REGIMES}
    for t in trades:
        r = t.get('entry_regime')
        if r in CANONICAL_REGIMES:
            _by_regime[r].append(t)
    for r in CANONICAL_REGIMES:
        agg_full = aggregate_metrics(_by_regime[r])
        per_regime[r]['sortino'] = agg_full.get('sortino')
        per_regime[r]['calmar'] = agg_full.get('calmar')
        # Also zero-out sortino/calmar for low-sample regimes (same threshold as sharpe)
        if per_regime[r].get('trade_count', 0) < 5:
            per_regime[r]['sortino'] = None
            per_regime[r]['calmar'] = None

    mean_universe_size: Optional[float] = None
    if universe_sizes:
        mean_universe_size = round(float(sum(universe_sizes) / len(universe_sizes)), 2)

    return per_regime, mean_universe_size


def main() -> int:
    ap = argparse.ArgumentParser(
        description='SP-2 Phase C: resolver→regime-blended 8-metric grid'
    )
    ap.add_argument('--strategy', required=True,
                    help='strategy_id to backtest (must exist in manifest)')
    ap.add_argument('--start', required=True, help='start date (YYYY-MM-DD)')
    ap.add_argument('--end', required=True, help='end date (YYYY-MM-DD)')
    ap.add_argument('--resolver-override', required=True,
                    help=f'candidate predicate name; one of: {sorted(CANDIDATE_PREDICATES)}')
    ap.add_argument('--metrics-json', action='store_true',
                    help='print metrics as JSON to stdout (default: on)')
    ap.add_argument('--seed', type=int, default=42,
                    help='RNG seed (accepted for forward-compat; unified_backtest has no RNG)')
    args = ap.parse_args()

    candidate = args.resolver_override
    if candidate not in CANDIDATE_PREDICATES:
        print(
            f'[universe_grid_cli] ERROR: unknown candidate "{candidate}". '
            f'Valid choices: {sorted(CANDIDATE_PREDICATES)}',
            file=sys.stderr,
        )
        return 2

    try:
        predicate = CANDIDATE_PREDICATES[candidate]
        uri = os.environ.get('POSTGRES_URI')
        if not uri:
            raise RuntimeError('POSTGRES_URI not set')

        db = PostgresMetadataDB(uri)
        cov = ParquetCoverage()
        resolver = MockResolver(
            db=db,
            coverage=cov,
            predicate=predicate,
            manifest_loader=_manifest_loader,
        )

        per_regime, mus = _simulate_grid(
            args.strategy,
            args.start,
            args.end,
            resolver,
        )
        day_freq = regime_day_frequency(REGIMES_PARQUET)
        metrics = blend_metrics(per_regime, day_freq, mean_universe_size=mus)

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


if __name__ == '__main__':
    sys.exit(main())
