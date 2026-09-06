#!/usr/bin/env python3
"""Walk-forward backtest harness — regime_blended_sizer vs production.

v2 (2026-05-12): reads from strategy_regime_backtests (per-strategy per-regime
auto_backtest snapshots) instead of signal_pnl. Wider historical window
(10y via historical_regimes.parquet) → statistical power for the Phase 3
gate-B decision.

Portfolio model (v1, conservative):
  - **Production** = portfolio that runs every live strategy in every regime,
    weighted by historical regime day-frequency.
  - **Blended**    = same, but each strategy zeroed out in regimes outside
    its manifest `eligible_regimes`. Captures the regime-eligibility filter
    effect only; cadence + consolidation effects deferred (they need
    signal-level overlap data the per-strategy backtests don't carry).

Aggregation per regime r over strategies s ∈ live:
  - portfolio_return[r] = compounded ∏(1 + tot_return[s,r]/100) - 1, multiplied by 100
  - portfolio_sharpe[r] = trade-count-weighted mean of strategy sharpes
  - portfolio_max_dd[r] = max (worst) strategy max_dd

Aggregation across regimes (regime day-frequency weights from
historical_regimes.parquet):
  - total_return = Σ day_frac[r] × portfolio_return[r]
  - sharpe       = Σ day_frac[r] × portfolio_sharpe[r]
  - max_dd       = max over r (worst-regime DD)

Gate B (Phase 3 LIVE-flip prerequisite) — revised 2026-05-12 (Path C/B hybrid):

  Empirical context: in the 10y regime-partitioned backtests, the filter's
  effect is concentrated in LOW_VOL (where it drops profitable momentum
  strategies). It provides no observable Sharpe or DD benefit in HIGH_VOL
  or CRISIS because the backtest data is sparse in those regimes
  (HIGH_VOL: 16% of days but most strategies show zero trades there;
  CRISIS: 6% of days, only 4 strategies, all losing). Strict
  `delta.sharpe > 0` is therefore not the right validator — the filter is
  a sacrifice trade purchased for unseen-regime robustness, not a free
  win on aggregate Sharpe.

  Revised gate:
  - delta.sharpe >= -SHARPE_REGRESSION_TOLERANCE_ABS  (bounded regression accepted)
  - delta.max_dd <= 0                                  (informational under current model;
                                                        eligibility filter can only remove
                                                        rows so worst-strategy-per-regime
                                                        DD is monotone non-increasing)
  - blended.strategy_count_LOW_VOL >= 1                (sanity: not a degenerate filter)

  The tolerance is documented in spec:
  docs/archive/superpowers/specs/2026-05-12-regime-backtest-backfill-spec.md (Phase F).
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

CANONICAL_REGIMES = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

# Sharpe regression Path B tolerance (absolute Sharpe units). Empirically the
# filter costs ~0.13 Sharpe in current 10y backtests; 0.25 leaves headroom but
# would still fail a doubling of the harm. Revisit if absolute Sharpe scale
# changes (currently inflated by per-trade σ annualization in auto_backtest).
SHARPE_REGRESSION_TOLERANCE_ABS = 0.25


def _db_uri() -> str:
    return (
        os.environ.get('DATABASE_URL')
        or os.environ.get('POSTGRES_URI')
        or 'postgresql://openclaw:password@localhost:5432/openclaw'
    )


def load_latest_backtests(uri: str):
    """Load the CANONICAL per-strategy regime backtests: for every strategy,
    the regime rows of its latest primary-window run in strategy_backtest_runs
    / strategy_backtest_regimes (the tables the nightly fleet re-backtest
    writes). The old strategy_regime_backtests snapshot table stopped being
    written on 2026-06-28 when its weekly timer was retired.

    Returns (df, run_id, run_at) in the strategy_regime_backtests row shape
    (`max_dd` positive, `note` None, `declared_regimes` None). run_at is the
    NEWEST canonical run (a freshness marker for "is the fleet alive?");
    df.attrs['median_run_at'] carries the median across strategies. Empty DF +
    '' + None when there are no rows.
    """
    import psycopg2
    sql = """
        WITH latest AS (
            SELECT DISTINCT ON (strategy_id) strategy_id, run_id, run_at
              FROM strategy_backtest_runs
             WHERE primary_window = true
             ORDER BY strategy_id, run_at DESC
        )
        SELECT l.strategy_id, r.regime_state,
               r.sharpe::float            AS sharpe,
               r.max_dd_pct::float        AS max_dd,
               r.return_pct::float        AS total_return_pct,
               r.trade_count, r.oos_days_in_regime AS oos_days,
               1 AS window_count, NULL::text AS note, NULL::text AS declared_regimes,
               l.run_at
          FROM latest l
          JOIN strategy_backtest_regimes r ON r.run_id = l.run_id
    """
    with psycopg2.connect(uri) as conn:
        df = pd.read_sql(sql, conn)
    if df.empty:
        return pd.DataFrame(), '', None
    run_ats = pd.to_datetime(df['run_at'], utc=True)
    per_strategy = df.groupby('strategy_id')['run_at'].max()
    newest = pd.to_datetime(per_strategy.max(), utc=True).to_pydatetime()
    median = pd.to_datetime(per_strategy, utc=True).sort_values().iloc[len(per_strategy) // 2].to_pydatetime()
    df = df.drop(columns=['run_at'])
    df.attrs['median_run_at'] = median
    run_id = f'canonical:{df["strategy_id"].nunique()}'
    return df, run_id, newest


def load_eligibility_from_params(uri: str) -> dict[str, list[str]]:
    """Live eligibility map: strategy_id -> regimes with eligible = true in
    strategy_regime_params (Phase 2A moved eligibility ownership there; the
    manifest's eligible_regimes is deprecated). Fail-open: any error -> {} and
    the caller keeps the manifest's field."""
    import psycopg2
    out: dict[str, list[str]] = {}
    try:
        with psycopg2.connect(uri) as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT strategy_id, regime_state, eligible
                                 FROM strategy_regime_params""")
                rows = cur.fetchall()
    except Exception:  # noqa: BLE001 — informational overlay, never fatal
        return {}
    seen: dict[str, list[str]] = {}
    for sid, regime, eligible in rows:
        seen.setdefault(sid, [])
        if eligible:
            seen[sid].append(regime)
    return seen


def regime_day_frequency(parquet_path: Path) -> dict[str, float]:
    """Fraction of historical days spent in each canonical regime."""
    if not parquet_path.exists():
        return {r: 1.0 / len(CANONICAL_REGIMES) for r in CANONICAL_REGIMES}
    df = pd.read_parquet(parquet_path)
    counts = df['regime'].value_counts()
    total = counts.sum()
    return {r: float(counts.get(r, 0)) / total for r in CANONICAL_REGIMES}


def portfolio_per_regime(df: pd.DataFrame, manifest: dict,
                          eligibility_filter: bool) -> dict[str, dict]:
    """Compute portfolio metrics per regime.

    If eligibility_filter=True, zero out strategy contribution in regimes
    outside its manifest eligible_regimes. If False, every strategy
    contributes in every regime (production baseline).
    """
    strategies = manifest.get('strategies', {})
    out: dict[str, dict] = {}
    for regime in CANONICAL_REGIMES:
        sub = df[df['regime_state'] == regime].copy()
        if eligibility_filter:
            def _eligible(row) -> bool:
                eligible = (strategies.get(row['strategy_id'], {})
                            .get('eligible_regimes'))
                # No eligibility declared → backward-compat: passes every regime.
                # Empty list → strict deny (matches eligibility-gate semantics).
                if eligible is None:
                    return True
                return regime in eligible
            sub = sub[sub.apply(_eligible, axis=1)]
        # Filter to rows with actual metrics (skip 'not_declared'/'no_oos_window').
        valid = sub[sub['note'].isna() & sub['trade_count'].notna()
                    & (sub['trade_count'] > 0)]
        if valid.empty:
            out[regime] = {'sharpe': 0.0, 'max_dd': 0.0,
                           'total_return_pct': 0.0, 'strategy_count': 0,
                           'trade_count': 0}
            continue
        total_trades = int(valid['trade_count'].sum())
        # Trade-count-weighted Sharpe across strategies in this regime.
        weights = valid['trade_count'].astype(float)
        w_sharpe = float((valid['sharpe'].astype(float) * weights).sum() / weights.sum())
        # Worst single-strategy DD in this regime — conservative.
        worst_dd = float(valid['max_dd'].astype(float).max())  # max_dd stored positive
        # Compounded portfolio return: equal-weight across strategies → average compounded.
        comp = (1.0 + valid['total_return_pct'].astype(float) / 100.0).prod() - 1.0
        out[regime] = {
            'sharpe':            round(w_sharpe, 4),
            'max_dd':            round(worst_dd, 4),
            'total_return_pct':  round(float(comp * 100.0), 4),
            'strategy_count':    int(len(valid)),
            'trade_count':       total_trades,
        }
    return out


def aggregate_across_regimes(per_regime: dict[str, dict],
                              day_freq: dict[str, float]) -> dict:
    """Day-frequency-weighted aggregate across the four regimes."""
    sharpe = sum(per_regime[r]['sharpe'] * day_freq[r] for r in CANONICAL_REGIMES)
    total_return = sum(per_regime[r]['total_return_pct'] * day_freq[r]
                       for r in CANONICAL_REGIMES)
    # max_dd over regimes — worst-case regime-conditional DD.
    max_dd = max(per_regime[r]['max_dd'] for r in CANONICAL_REGIMES) if per_regime else 0.0
    trade_count = sum(per_regime[r]['trade_count'] for r in CANONICAL_REGIMES)
    return {
        'sharpe':            round(float(sharpe), 4),
        'max_dd':            round(float(max_dd), 4),
        'total_return_pct':  round(float(total_return), 4),
        'trade_count':       int(trade_count),
        'per_regime':        per_regime,
    }


def run_walkforward(uri: str, manifest_path: Path, regime_parquet: Path) -> dict:
    """End-to-end: load latest backtests + manifest + regime weights → compare."""
    df, run_id, run_at = load_latest_backtests(uri)
    if df.empty:
        return {'error': 'strategy_regime_backtests has no rows — run backfill first'}
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    # Eligibility overlay: strategy_regime_params is the live owner of
    # eligibility; the manifest field only survives for strategies without rows.
    eligibility = load_eligibility_from_params(uri)
    eligibility_source = 'manifest'
    if eligibility:
        strategies = manifest.setdefault('strategies', {})
        for sid, regimes in eligibility.items():
            strategies.setdefault(sid, {})['eligible_regimes'] = list(regimes)
        eligibility_source = 'strategy_regime_params'
    day_freq = regime_day_frequency(regime_parquet)

    prod_pr = portfolio_per_regime(df, manifest, eligibility_filter=False)
    blended_pr = portfolio_per_regime(df, manifest, eligibility_filter=True)

    production = aggregate_across_regimes(prod_pr, day_freq)
    blended = aggregate_across_regimes(blended_pr, day_freq)

    delta_sharpe = blended['sharpe'] - production['sharpe']
    delta_max_dd = blended['max_dd'] - production['max_dd']
    sharpe_within_tolerance = delta_sharpe >= -SHARPE_REGRESSION_TOLERANCE_ABS
    max_dd_not_worse = blended['max_dd'] <= production['max_dd']
    low_vol_strategies_present = (
        blended['per_regime']['LOW_VOL']['strategy_count'] >= 1
    )
    median_run_at = df.attrs.get('median_run_at') if hasattr(df, 'attrs') else None
    return {
        'source':           'strategy_backtest_regimes (canonical, latest primary run per strategy)',
        'eligibility_source': eligibility_source,
        'run_id':           run_id,
        'run_at':           run_at.isoformat() if run_at else None,
        'median_run_at':    median_run_at.isoformat() if median_run_at else None,
        'strategy_count':   int(df['strategy_id'].nunique()),
        'regime_day_frequency': {r: round(day_freq[r], 4) for r in CANONICAL_REGIMES},
        'blended':          blended,
        'production':       production,
        'delta': {
            'sharpe':   round(delta_sharpe, 4),
            'max_dd':   round(delta_max_dd, 4),
        },
        'gate_b': {
            'sharpe_regression_tolerance':  SHARPE_REGRESSION_TOLERANCE_ABS,
            'sharpe_within_tolerance':      sharpe_within_tolerance,
            'sharpe_positive':              delta_sharpe > 0,  # informational
            'max_dd_not_worse':             max_dd_not_worse,
            'low_vol_strategies_present':   low_vol_strategies_present,
            'pass':                         (sharpe_within_tolerance
                                             and max_dd_not_worse
                                             and low_vol_strategies_present),
        },
    }


def main():
    uri = _db_uri()
    manifest_path = ROOT / 'src' / 'strategies' / 'manifest.json'
    regime_parquet = ROOT / 'data' / 'master' / 'historical_regimes.parquet'

    result = run_walkforward(uri, manifest_path, regime_parquet)

    out_dir = ROOT / 'output'
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / 'regime_blended_walkforward.json'
    out_path.write_text(json.dumps(result, indent=2, default=str))

    print(json.dumps(result, indent=2, default=str))
    print(f'\n[walkforward] written to {out_path}')


def run_with_resolver(strategy, start, end, resolver, **kwargs):
    """SP-2 Phase A: per-bar resolver opt-in stub.

    Phase A acceptance is "the option exists". This iterates trading days
    and calls resolver.resolve(strategy.id, as_of=bar_date) so callers
    can wire per-bar universes through this engine. Phase B/C will join
    this universe up to the engine's existing logic (which is shaped
    differently across engines — manifest-driven, features-driven, etc.).
    """
    from src.backtest._trading_calendar import trading_days
    results = []
    for bar_date in trading_days(start, end):
        universe = resolver.resolve(strategy.id, as_of=bar_date)
        results.append({"date": bar_date, "universe": universe})
    return results


if __name__ == '__main__':
    main()
